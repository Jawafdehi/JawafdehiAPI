from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CHUNK_SIZE = 1200
DEFAULT_CHUNK_OVERLAP = 180
DEFAULT_MIN_CHUNK_CHARS = 120
DEFAULT_SEPARATORS = ("\n## ", "\n### ", "\n#### ", "\n\n", "\n", ". ", " ", "")


class KnowledgeChunkingError(ValueError):
    pass


@dataclass(frozen=True)
class ChunkingConfig:
    strategy: str = "recursive"
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    min_chunk_chars: int = DEFAULT_MIN_CHUNK_CHARS
    separators: tuple[str, ...] = DEFAULT_SEPARATORS

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> "ChunkingConfig":
        payload = manifest.get("chunking") or {}
        if not isinstance(payload, dict):
            raise KnowledgeChunkingError("chunking must be a JSON object.")

        strategy = str(payload.get("strategy") or "recursive").strip().lower()
        if strategy not in {"recursive", "page_recursive"}:
            raise KnowledgeChunkingError(
                "chunking.strategy must be one of: recursive, page_recursive."
            )

        chunk_size = _positive_int(payload.get("chunk_size"), DEFAULT_CHUNK_SIZE)
        chunk_overlap = _non_negative_int(
            payload.get("chunk_overlap"), DEFAULT_CHUNK_OVERLAP
        )
        min_chunk_chars = _positive_int(
            payload.get("min_chunk_chars"), DEFAULT_MIN_CHUNK_CHARS
        )
        if chunk_overlap >= chunk_size:
            raise KnowledgeChunkingError(
                "chunking.chunk_overlap must be below chunk_size."
            )

        separators = payload.get("separators")
        if separators is None:
            separator_tuple = DEFAULT_SEPARATORS
        elif isinstance(separators, list) and all(
            isinstance(item, str) for item in separators
        ):
            separator_tuple = tuple(separators)
            if "" not in separator_tuple:
                separator_tuple = separator_tuple + ("",)
        else:
            raise KnowledgeChunkingError(
                "chunking.separators must be a list of strings."
            )

        return cls(
            strategy=strategy,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            min_chunk_chars=min_chunk_chars,
            separators=separator_tuple,
        )


@dataclass(frozen=True)
class DocumentUnit:
    text: str
    page_start: int | None = None
    page_end: int | None = None
    section_title: str = ""
    metadata: dict[str, Any] | None = None


def chunks_from_manifest(
    manifest: dict[str, Any],
    *,
    base_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Create chunk rows from document text/pages using recursive splitting."""

    config = ChunkingConfig.from_manifest(manifest)
    units = document_units_from_manifest(manifest, base_dir=base_dir)
    rows: list[dict[str, Any]] = []
    for unit in units:
        for chunk_text in split_text_recursive(
            unit.text,
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
            separators=config.separators,
        ):
            cleaned = chunk_text.strip()
            if not cleaned:
                continue
            if len(cleaned) < config.min_chunk_chars and rows:
                rows[-1]["text"] = f"{rows[-1]['text'].rstrip()}\n\n{cleaned}"
                continue
            rows.append(
                {
                    "text": cleaned,
                    "page_start": unit.page_start,
                    "page_end": unit.page_end,
                    "section_title": unit.section_title or _heading_from_text(cleaned),
                    "metadata": {
                        **(unit.metadata or {}),
                        "chunking_strategy": config.strategy,
                        "chunk_size": config.chunk_size,
                        "chunk_overlap": config.chunk_overlap,
                    },
                }
            )

    for index, row in enumerate(rows):
        row["chunk_index"] = index
    return rows


def document_units_from_manifest(
    manifest: dict[str, Any],
    *,
    base_dir: Path | None = None,
) -> list[DocumentUnit]:
    document = manifest.get("document") or {}
    if not isinstance(document, dict):
        raise KnowledgeChunkingError("document must be a JSON object.")

    pages = document.get("pages")
    if isinstance(pages, list):
        units = []
        for index, page in enumerate(pages):
            if not isinstance(page, dict):
                raise KnowledgeChunkingError(
                    f"document.pages[{index}] must be an object."
                )
            text = str(page.get("text") or page.get("content") or "").strip()
            if not text:
                continue
            page_number = _optional_int(page.get("page") or page.get("page_start"))
            page_end = _optional_int(page.get("page_end")) or page_number
            units.append(
                DocumentUnit(
                    text=text,
                    page_start=page_number,
                    page_end=page_end,
                    section_title=str(page.get("section_title") or ""),
                    metadata=_metadata(page),
                )
            )
        if units:
            return units

    text = _document_text(document, base_dir=base_dir)
    if text:
        return [
            DocumentUnit(
                text=text,
                section_title=str(document.get("section_title") or ""),
                metadata=_metadata(document),
            )
        ]

    raise KnowledgeChunkingError(
        "Manifest without chunks must include document.text, document.markdown, "
        "document.content, document.pages, or document.content_file."
    )


def split_text_recursive(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    separators: tuple[str, ...] = DEFAULT_SEPARATORS,
) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    pieces = _recursive_pieces(text, chunk_size=chunk_size, separators=separators)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        candidate = f"{current}\n\n{piece}".strip() if current else piece
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            overlap = _tail_overlap(current, chunk_overlap)
            current = f"{overlap}\n\n{piece}".strip() if overlap else piece
        else:
            chunks.append(piece[:chunk_size])
            current = piece[max(0, chunk_size - chunk_overlap) :]

    if current:
        chunks.append(current)
    return chunks


def _recursive_pieces(
    text: str,
    *,
    chunk_size: int,
    separators: tuple[str, ...],
) -> list[str]:
    text = text.strip()
    if len(text) <= chunk_size:
        return [text]

    separator = next((item for item in separators if item and item in text), "")
    if separator == "":
        return [
            text[index : index + chunk_size]
            for index in range(0, len(text), chunk_size)
        ]

    parts = _split_keep_separator(text, separator)
    pieces: list[str] = []
    for part in parts:
        if len(part) <= chunk_size:
            pieces.append(part)
        else:
            next_separators = separators[separators.index(separator) + 1 :]
            pieces.extend(
                _recursive_pieces(
                    part,
                    chunk_size=chunk_size,
                    separators=next_separators or ("",),
                )
            )
    return pieces


def _split_keep_separator(text: str, separator: str) -> list[str]:
    if separator.startswith("\n"):
        normalized = text.replace(
            separator, f"\n__JDS_CHUNK_SPLIT__{separator.lstrip()}"
        )
        return [
            item.replace("__JDS_CHUNK_SPLIT__", "").strip()
            for item in normalized.split("\n__JDS_CHUNK_SPLIT__")
            if item.strip()
        ]
    return [item.strip() for item in text.split(separator) if item.strip()]


def _tail_overlap(text: str, chunk_overlap: int) -> str:
    if chunk_overlap <= 0:
        return ""
    tail = text[-chunk_overlap:].strip()
    first_space = tail.find(" ")
    return tail[first_space + 1 :].strip() if first_space > 0 else tail


def _document_text(document: dict[str, Any], *, base_dir: Path | None) -> str:
    for key in ("text", "markdown", "content"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    file_name = (
        document.get("content_file")
        or document.get("markdown_file")
        or document.get("text_file")
    )
    if not file_name:
        return ""
    if base_dir is None:
        raise KnowledgeChunkingError("document content files require a base directory.")

    path = (base_dir / str(file_name)).resolve()
    if not str(path).startswith(str(base_dir.resolve())):
        raise KnowledgeChunkingError(
            "document content files must stay inside manifest directory."
        )
    return path.read_text(encoding="utf-8").strip()


def _heading_from_text(text: str) -> str:
    for line in text.splitlines()[:5]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:300]
    return ""


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def _positive_int(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise KnowledgeChunkingError("chunking values must be integers.") from exc
    if parsed < 1:
        raise KnowledgeChunkingError("chunking values must be positive integers.")
    return parsed


def _non_negative_int(value: Any, default: int) -> int:
    parsed = _positive_int(value, default) if value not in (0, "0") else 0
    if parsed < 0:
        raise KnowledgeChunkingError("chunking values cannot be negative.")
    return parsed


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
