from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from django.conf import settings

from .importer import (
    KnowledgeImportError,
    KnowledgeImportResult,
    import_knowledge_manifest,
)
from .models import AccessLevel, KnowledgeCollection, KnowledgeSource

NEPALI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
YEAR_RE = re.compile(r"(?<!\d)(20[4-9]\d|21[0-1]\d)(?:\s*[/\-.]\s*(\d{2,4}))?")
DEFAULT_CHUNKING = {
    "strategy": "recursive",
    "chunk_size": 1200,
    "chunk_overlap": 180,
    "min_chunk_chars": 120,
}


@dataclass(frozen=True)
class SourceImportRequest:
    collection_name: str = "public_docs"
    collection_display_name: str = "Public Docs"
    source_title: str = ""
    source_type: str = "document"
    access_level: str = AccessLevel.PUBLIC
    embed: bool = False
    source_url: str = ""
    text: str = ""
    markdown: str = ""
    manifest: dict[str, Any] | None = None
    file_name: str = ""
    file_bytes: bytes | None = None
    content_type: str = ""
    pages: str = ""
    page_start: int | None = None
    page_end: int | None = None
    expand_catalog: bool = True
    convert_linked_documents: bool = False


@dataclass
class SourceImportSummary:
    collection: KnowledgeCollection
    source: KnowledgeSource | None = None
    sources_imported: int = 0
    chunks_imported: int = 0
    embeddings_imported: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


def import_source(payload: SourceImportRequest) -> SourceImportSummary:
    collection_payload = _collection_payload(payload)

    if payload.manifest is not None:
        manifest = _with_embedding(payload.manifest, payload.embed)
        result = import_knowledge_manifest(manifest)
        return SourceImportSummary(
            collection=result.collection,
            source=result.source,
            sources_imported=1,
            chunks_imported=result.chunks_imported,
            embeddings_imported=result.embeddings_imported,
        )

    if payload.source_url:
        fetched = _fetch_url(payload.source_url)
        if _is_json_content(payload.source_url, fetched.content_type):
            return _import_json_url(payload, fetched)
        return _import_single_document(
            payload,
            collection_payload=collection_payload,
            source_url=payload.source_url,
            title=payload.source_title or _title_from_url(payload.source_url),
            content=fetched.body,
            content_type=fetched.content_type,
            original_file_name=_file_name_from_url(payload.source_url),
        )

    if payload.file_bytes is not None:
        return _import_single_document(
            payload,
            collection_payload=collection_payload,
            source_url="",
            title=payload.source_title or _title_from_file(payload.file_name),
            content=payload.file_bytes,
            content_type=payload.content_type or _guess_content_type(payload.file_name),
            original_file_name=payload.file_name,
        )

    document_text = payload.markdown or payload.text
    if document_text.strip():
        title = payload.source_title or "Pasted knowledge source"
        manifest = _manifest_for_document(
            payload,
            collection_payload=collection_payload,
            title=title,
            source_url="",
            source_type=payload.source_type,
            document_markdown=document_text,
            metadata=_metadata(
                title=title,
                source_url="",
                source_type=payload.source_type,
                original_file_name="",
                content_type="text/markdown" if payload.markdown else "text/plain",
                extra={},
            ),
        )
        result = import_knowledge_manifest(manifest)
        return _summary_from_result(result)

    raise KnowledgeImportError(
        "Provide source_url, file, text, markdown, or manifest JSON."
    )


@dataclass(frozen=True)
class FetchedURL:
    body: bytes
    content_type: str


def _fetch_url(url: str) -> FetchedURL:
    try:
        response = httpx.get(url, follow_redirects=True, timeout=60.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise KnowledgeImportError(f"Could not fetch source URL: {exc}") from exc
    return FetchedURL(
        body=response.content,
        content_type=response.headers.get("content-type", "").split(";")[0].strip(),
    )


def _import_json_url(
    payload: SourceImportRequest,
    fetched: FetchedURL,
) -> SourceImportSummary:
    try:
        parsed = json.loads(fetched.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KnowledgeImportError(f"Source URL returned invalid JSON: {exc}") from exc

    if _looks_like_knowledge_manifest(parsed):
        manifest = _with_embedding(parsed, payload.embed)
        result = import_knowledge_manifest(manifest)
        return _summary_from_result(result)

    if payload.expand_catalog and _looks_like_catalog(parsed):
        return _import_catalog(payload, parsed)

    document_markdown = json.dumps(parsed, ensure_ascii=False, indent=2)
    title = payload.source_title or _title_from_url(payload.source_url)
    manifest = _manifest_for_document(
        payload,
        collection_payload=_collection_payload(payload),
        title=title,
        source_url=payload.source_url,
        source_type=payload.source_type or "json",
        document_markdown=document_markdown,
        metadata=_metadata(
            title=title,
            source_url=payload.source_url,
            source_type=payload.source_type or "json",
            original_file_name=_file_name_from_url(payload.source_url),
            content_type=fetched.content_type or "application/json",
            extra={"json_import_mode": "document"},
        ),
    )
    result = import_knowledge_manifest(manifest)
    return _summary_from_result(result)


def _import_catalog(
    payload: SourceImportRequest,
    catalog: dict[str, Any],
) -> SourceImportSummary:
    manuscripts = catalog.get("manuscripts")
    if not isinstance(manuscripts, list) or not manuscripts:
        raise KnowledgeImportError(
            "Catalog JSON must include a non-empty manuscripts list."
        )

    collection = _ensure_collection(_collection_payload(payload))
    source: KnowledgeSource | None = None
    chunks_imported = 0
    embeddings_imported = 0
    sources_imported = 0
    failures: list[dict[str, str]] = []

    for index, item in enumerate(manuscripts):
        if not isinstance(item, dict):
            failures.append(
                {"item": str(index), "error": "Catalog item is not an object."}
            )
            continue
        item_url = str(item.get("url") or "").strip()
        if not item_url:
            failures.append(
                {"item": str(index), "error": "Catalog item is missing url."}
            )
            continue
        item_metadata = (
            item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        )
        item_title = (
            str(item_metadata.get("title") or "").strip()
            or str(item.get("title") or "").strip()
            or str(item.get("file_name") or "").strip()
            or _title_from_url(item_url)
        )
        item_file_name = str(
            item.get("file_name") or item_metadata.get("file_name") or ""
        )
        source_type = (payload.source_type or "document").strip() or "document"
        metadata = _metadata(
            title=item_title,
            source_url=item_url,
            source_type=source_type,
            original_file_name=item_file_name,
            content_type=_guess_content_type(item_url),
            extra={
                "catalog_url": payload.source_url,
                "catalog_name": catalog.get("name", ""),
                "catalog_path": catalog.get("path", ""),
                "catalog_metadata": item_metadata,
                "year_tokens": _year_tokens(
                    " ".join([item_title, str(item.get("file_name") or "")])
                ),
                "is_summary": _looks_like_summary(
                    item_title, str(item.get("file_name") or "")
                ),
            },
        )

        try:
            if payload.convert_linked_documents:
                linked = _fetch_url(item_url)
                result = _import_single_document_result(
                    payload,
                    collection_payload=_collection_payload(payload),
                    source_url=item_url,
                    title=item_title,
                    content=linked.body,
                    content_type=linked.content_type,
                    original_file_name=str(
                        item.get("file_name") or _file_name_from_url(item_url)
                    ),
                    metadata=metadata,
                )
            else:
                result = import_knowledge_manifest(
                    _with_embedding(
                        {
                            "collection": _collection_payload(payload),
                            "source": {
                                "title": item_title,
                                "source_type": source_type,
                                "source_url": item_url,
                                "checksum": _source_checksum(item_url),
                                "access_level": payload.access_level,
                                "metadata": metadata,
                                "is_active": True,
                            },
                            "chunks": [
                                {
                                    "text": _locator_text(
                                        title=item_title,
                                        source_url=item_url,
                                        source_type=source_type,
                                        metadata=metadata,
                                    ),
                                    "chunk_index": 0,
                                    "section_title": "Source locator",
                                    "metadata": {
                                        "locator_chunk": True,
                                        "catalog_url": payload.source_url,
                                    },
                                }
                            ],
                        },
                        payload.embed,
                    )
                )
            source = result.source
            sources_imported += 1
            chunks_imported += result.chunks_imported
            embeddings_imported += result.embeddings_imported
        except KnowledgeImportError as exc:
            failures.append({"item": item_title, "error": str(exc)})

    if sources_imported == 0:
        first_error = failures[0]["error"] if failures else "No catalog items imported."
        raise KnowledgeImportError(f"Catalog import produced no sources: {first_error}")

    return SourceImportSummary(
        collection=collection,
        source=source,
        sources_imported=sources_imported,
        chunks_imported=chunks_imported,
        embeddings_imported=embeddings_imported,
        failures=failures,
    )


def _import_single_document(
    payload: SourceImportRequest,
    *,
    collection_payload: dict[str, Any],
    source_url: str,
    title: str,
    content: bytes,
    content_type: str,
    original_file_name: str,
) -> SourceImportSummary:
    result = _import_single_document_result(
        payload,
        collection_payload=collection_payload,
        source_url=source_url,
        title=title,
        content=content,
        content_type=content_type,
        original_file_name=original_file_name,
        metadata=None,
    )
    return _summary_from_result(result)


def _import_single_document_result(
    payload: SourceImportRequest,
    *,
    collection_payload: dict[str, Any],
    source_url: str,
    title: str,
    content: bytes,
    content_type: str,
    original_file_name: str,
    metadata: dict[str, Any] | None,
) -> KnowledgeImportResult:
    markdown = _content_to_markdown(
        content,
        source_url=source_url,
        content_type=content_type,
        original_file_name=original_file_name,
        pages=_page_range(payload),
    )
    source_type = payload.source_type or _source_type_from_content(
        content_type, original_file_name
    )
    metadata_payload = metadata or _metadata(
        title=title,
        source_url=source_url,
        source_type=source_type,
        original_file_name=original_file_name,
        content_type=content_type,
        extra={},
    )
    manifest = _manifest_for_document(
        payload,
        collection_payload=collection_payload,
        title=title,
        source_url=source_url,
        source_type=source_type,
        document_markdown=markdown,
        metadata=metadata_payload,
    )
    return import_knowledge_manifest(manifest)


def _content_to_markdown(
    content: bytes,
    *,
    source_url: str,
    content_type: str,
    original_file_name: str,
    pages: str,
) -> str:
    if _is_text_content(content_type, original_file_name):
        text = _decode_text(content)
        if _is_html_content(content_type, original_file_name):
            return _html_to_text(text)
        return text

    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise KnowledgeImportError(
            "Document conversion requires the markitdown package for this source type."
        ) from exc

    suffix = Path(original_file_name or urlparse(source_url).path).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix) as temporary:
        temporary.write(content)
        temporary.flush()
        converter = MarkItDown(enable_plugins=True)
        kwargs = {"pages": pages} if pages else {}
        try:
            result = converter.convert_uri(
                Path(temporary.name).resolve().as_uri(), **kwargs
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 - conversion errors should be user-facing.
            raise KnowledgeImportError(
                f"Could not convert document to Markdown: {exc}"
            ) from exc
    markdown = str(getattr(result, "markdown", "") or "").strip()
    if not markdown:
        raise KnowledgeImportError("Document conversion produced no Markdown.")
    return markdown


def _manifest_for_document(
    payload: SourceImportRequest,
    *,
    collection_payload: dict[str, Any],
    title: str,
    source_url: str,
    source_type: str,
    document_markdown: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "collection": collection_payload,
        "source": {
            "title": title,
            "source_type": source_type,
            "source_url": source_url,
            "checksum": _source_checksum(source_url or title),
            "access_level": payload.access_level,
            "metadata": metadata,
            "is_active": True,
        },
        "document": {"markdown": document_markdown},
        "chunking": DEFAULT_CHUNKING,
    }
    return _with_embedding(manifest, payload.embed)


def _with_embedding(manifest: dict[str, Any], embed: bool) -> dict[str, Any]:
    if not embed:
        return manifest
    if not getattr(settings, "KNOWLEDGE_RAG_EMBEDDING_MODEL", ""):
        return manifest
    embedding = manifest.get("embedding")
    if isinstance(embedding, dict):
        manifest["embedding"] = embedding | {"auto": True}
    else:
        manifest["embedding"] = {"auto": True}
    return manifest


def _collection_payload(payload: SourceImportRequest) -> dict[str, Any]:
    if payload.access_level not in {AccessLevel.PRIVATE, AccessLevel.PUBLIC}:
        raise KnowledgeImportError("access_level must be one of: private, public.")
    name = _slug_or_default(payload.collection_name, "public_docs")
    return {
        "name": name,
        "display_name": payload.collection_display_name
        or name.replace("_", " ").title(),
        "access_level": payload.access_level,
        "is_active": True,
    }


def _ensure_collection(collection_payload: dict[str, Any]) -> KnowledgeCollection:
    collection, _ = KnowledgeCollection.objects.update_or_create(
        name=collection_payload["name"],
        defaults={
            "display_name": collection_payload.get("display_name")
            or collection_payload["name"],
            "description": collection_payload.get("description", ""),
            "access_level": collection_payload.get("access_level", AccessLevel.PRIVATE),
            "is_active": collection_payload.get("is_active", True),
        },
    )
    return collection


def _metadata(
    *,
    title: str,
    source_url: str,
    source_type: str,
    original_file_name: str,
    content_type: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    metadata = {
        "public_citation": {
            "title": title,
            "url": source_url,
            "identifier": source_url or title,
            "publisher": "Jawafdehi knowledgebase",
            "publication_date": "",
        },
        "imported_at": now,
        "original_file_name": original_file_name,
        "content_type": content_type,
        "source_type": source_type,
        "year_tokens": _year_tokens(" ".join([title, original_file_name])),
    }
    metadata.update(
        {key: value for key, value in extra.items() if value not in (None, "")}
    )
    return metadata


def _locator_text(
    *,
    title: str,
    source_url: str,
    source_type: str,
    metadata: dict[str, Any],
) -> str:
    year_tokens = " ".join(str(item) for item in metadata.get("year_tokens", []))
    catalog_metadata = metadata.get("catalog_metadata")
    catalog_text = ""
    if isinstance(catalog_metadata, dict):
        catalog_text = " ".join(str(value) for value in catalog_metadata.values())
    return "\n".join(
        part
        for part in [
            f"Title: {title}",
            f"Source type: {source_type}",
            f"Source URL: {source_url}",
            f"Years: {year_tokens}",
            f"Catalog metadata: {catalog_text}",
        ]
        if part.strip()
    )


def _looks_like_knowledge_manifest(value: Any) -> bool:
    return isinstance(value, dict) and "collection" in value and "source" in value


def _looks_like_catalog(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("manuscripts"), list)


def _is_json_content(url: str, content_type: str) -> bool:
    return "json" in content_type.lower() or urlparse(url).path.lower().endswith(
        ".json"
    )


def _is_text_content(content_type: str, file_name: str) -> bool:
    lowered = (content_type or "").lower()
    suffix = Path(file_name).suffix.lower()
    return (
        lowered.startswith("text/")
        or lowered in {"application/json", "application/xml"}
        or suffix
        in {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".html", ".htm"}
    )


def _is_html_content(content_type: str, file_name: str) -> bool:
    suffix = Path(file_name).suffix.lower()
    return content_type == "text/html" or suffix in {".html", ".htm"}


def _source_type_from_content(content_type: str, file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".pdf" or content_type == "application/pdf":
        return "pdf"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".html", ".htm"} or content_type == "text/html":
        return "webpage"
    return "document"


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return content.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    raise KnowledgeImportError("Could not decode text source.")


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if tag in {
            "p",
            "br",
            "div",
            "section",
            "article",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        cleaned = " ".join(data.split())
        if cleaned:
            self.parts.append(cleaned)


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in text.splitlines() if line)


def _page_range(payload: SourceImportRequest) -> str:
    if payload.pages:
        return payload.pages.strip()
    if payload.page_start is None:
        return ""
    end = payload.page_end or payload.page_start
    if end < payload.page_start:
        raise KnowledgeImportError("page_end cannot be before page_start.")
    if end == payload.page_start:
        return str(payload.page_start)
    return f"{payload.page_start}-{end}"


def _year_tokens(text: str) -> list[str]:
    normalized = text.translate(NEPALI_DIGITS)
    tokens: set[str] = set()
    for start, end in YEAR_RE.findall(normalized):
        tokens.add(start)
        if end:
            if len(end) == 2:
                end = start[:2] + end
            tokens.add(end)
            tokens.add(f"{start}/{end[-2:]}")
            tokens.add(f"{start}/{end}")
    for token in list(tokens):
        tokens.add(token.translate(str.maketrans("0123456789", "०१२३४५६७८९")))
    return sorted(tokens)


def _looks_like_summary(title: str, file_name: str) -> bool:
    lowered = f"{title} {file_name}".lower()
    return "summary" in lowered or "executive" in lowered


def _slug_or_default(value: str, default: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")
    return slug or default


def _title_from_url(url: str) -> str:
    file_name = _file_name_from_url(url)
    return _title_from_file(file_name) if file_name else url


def _title_from_file(file_name: str) -> str:
    return Path(file_name).stem.replace("_", " ").replace("-", " ").strip() or file_name


def _file_name_from_url(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)


def _guess_content_type(file_name_or_url: str) -> str:
    return mimetypes.guess_type(file_name_or_url)[0] or ""


def _source_checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _summary_from_result(result: KnowledgeImportResult) -> SourceImportSummary:
    return SourceImportSummary(
        collection=result.collection,
        source=result.source,
        sources_imported=1,
        chunks_imported=result.chunks_imported,
        embeddings_imported=result.embeddings_imported,
    )
