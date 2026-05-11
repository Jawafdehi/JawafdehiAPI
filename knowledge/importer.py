from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction

from .chunking import KnowledgeChunkingError, chunks_from_manifest
from .models import (
    AccessLevel,
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeEmbedding,
    KnowledgeSource,
    has_public_citation_metadata,
)


class KnowledgeImportError(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgeImportResult:
    collection: KnowledgeCollection
    source: KnowledgeSource
    chunks_imported: int
    embeddings_imported: int = 0


@transaction.atomic
def import_knowledge_manifest(
    manifest: dict[str, Any],
    *,
    base_dir: Path | None = None,
    query_embedder=None,
) -> KnowledgeImportResult:
    if not isinstance(manifest, dict):
        raise KnowledgeImportError("Manifest must be a JSON object.")

    collection = _upsert_collection(manifest)
    source = _upsert_source(manifest, collection)
    chunks = _load_chunks(manifest, base_dir)

    imported_chunks: list[KnowledgeChunk] = []
    imported = 0
    for index, row in enumerate(chunks):
        chunk = _upsert_chunk(source, row, index)
        _upsert_embedding(chunk, row)
        imported_chunks.append(chunk)
        imported += 1

    embeddings_imported = _auto_embed_chunks(
        manifest,
        imported_chunks,
        query_embedder=query_embedder,
    )

    return KnowledgeImportResult(
        collection=collection,
        source=source,
        chunks_imported=imported,
        embeddings_imported=embeddings_imported,
    )


def _upsert_collection(manifest: dict[str, Any]) -> KnowledgeCollection:
    collection_data = manifest.get("collection")
    if isinstance(collection_data, str):
        collection_payload = {"name": collection_data}
    elif isinstance(collection_data, dict):
        collection_payload = collection_data
    else:
        raise KnowledgeImportError(
            "Manifest must include collection as a string or object."
        )

    name = collection_payload.get("name")
    if not name:
        raise KnowledgeImportError("Collection name is required.")

    defaults = {
        "display_name": collection_payload.get("display_name")
        or name.replace("_", " ").title(),
        "description": collection_payload.get("description", ""),
        "access_level": collection_payload.get("access_level", AccessLevel.PRIVATE),
        "is_active": collection_payload.get("is_active", True),
    }
    _validate_access(defaults["access_level"], "collection.access_level")

    collection, _ = KnowledgeCollection.objects.update_or_create(
        name=name,
        defaults=defaults,
    )
    return collection


def _upsert_source(
    manifest: dict[str, Any], collection: KnowledgeCollection
) -> KnowledgeSource:
    source_payload = manifest.get("source")
    if not isinstance(source_payload, dict):
        raise KnowledgeImportError("Manifest must include source as an object.")

    title = source_payload.get("title")
    if not title:
        raise KnowledgeImportError("Source title is required.")

    access_level = source_payload.get("access_level", AccessLevel.PRIVATE)
    _validate_access(access_level, "source.access_level")

    source_url = source_payload.get("source_url") or source_payload.get("url") or ""
    storage_path = source_payload.get("storage_path") or ""
    metadata = source_payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise KnowledgeImportError("source.metadata must be a JSON object.")
    if access_level == AccessLevel.PUBLIC and not (
        source_url or has_public_citation_metadata(metadata)
    ):
        raise KnowledgeImportError(
            "Public knowledge sources require source_url or "
            "metadata.public_citation for citations."
        )

    checksum = source_payload.get("checksum") or ""
    lookup = {"collection": collection, "checksum": checksum} if checksum else None
    if lookup is None:
        lookup = {"collection": collection, "title": title}

    source, _ = KnowledgeSource.objects.update_or_create(
        **lookup,
        defaults={
            "title": title,
            "source_type": source_payload.get("source_type", "document"),
            "source_url": source_url,
            "storage_path": storage_path,
            "checksum": checksum,
            "metadata": metadata,
            "access_level": access_level,
            "is_active": source_payload.get("is_active", True),
        },
    )
    return source


def _load_chunks(
    manifest: dict[str, Any], base_dir: Path | None
) -> list[dict[str, Any]]:
    if isinstance(manifest.get("chunks"), list):
        chunks = manifest["chunks"]
    elif manifest.get("chunks_file"):
        if base_dir is None:
            raise KnowledgeImportError(
                "chunks_file is only supported for file imports."
            )
        chunks_file = (base_dir / manifest["chunks_file"]).resolve()
        if not str(chunks_file).startswith(str(base_dir.resolve())):
            raise KnowledgeImportError(
                "chunks_file must stay inside the manifest directory."
            )
        chunks = _load_json(chunks_file)
    else:
        try:
            chunks = chunks_from_manifest(manifest, base_dir=base_dir)
        except KnowledgeChunkingError as exc:
            raise KnowledgeImportError(str(exc)) from exc

    if not isinstance(chunks, list):
        raise KnowledgeImportError("Knowledge chunks must be a list.")
    if not chunks:
        raise KnowledgeImportError("Knowledge import produced no chunks.")
    return chunks


def _upsert_chunk(
    source: KnowledgeSource, row: dict[str, Any], index: int
) -> KnowledgeChunk:
    if not isinstance(row, dict):
        raise KnowledgeImportError(f"Chunk {index} must be an object.")

    text = str(row.get("text") or row.get("content") or "").strip()
    if not text:
        raise KnowledgeImportError(f"Chunk {index} is missing text.")

    metadata = row.get("metadata", {})
    if not isinstance(metadata, dict):
        raise KnowledgeImportError(f"Chunk {index} metadata must be a JSON object.")

    chunk_index = int(row.get("chunk_index", index))
    content_hash = row.get("content_hash") or hash_text(
        f"{source.id}:{chunk_index}:{text}"
    )
    defaults = {
        "text": text,
        "chunk_index": chunk_index,
        "page_start": int_or_none(row.get("page_start") or row.get("page")),
        "page_end": int_or_none(row.get("page_end") or row.get("page")),
        "section_title": row.get("section_title", ""),
        "table_title": row.get("table_title", ""),
        "metadata": metadata,
    }
    chunk, _ = KnowledgeChunk.objects.update_or_create(
        source=source,
        chunk_index=chunk_index,
        defaults=defaults | {"content_hash": content_hash},
    )
    return chunk


def _upsert_embedding(chunk: KnowledgeChunk, row: dict[str, Any]) -> None:
    embedding = row.get("embedding")
    if not embedding:
        return
    if not isinstance(embedding, list) or not all(
        isinstance(value, (int, float)) for value in embedding
    ):
        raise KnowledgeImportError("Embedding must be a list of numbers.")
    model = row.get("embedding_model") or "unknown"
    KnowledgeEmbedding.objects.update_or_create(
        chunk=chunk,
        embedding_model=model,
        defaults={
            "embedding": embedding,
            "vector": embedding,
            "dimensions": len(embedding),
            "metadata": row.get("embedding_metadata", {}),
        },
    )


def _auto_embed_chunks(
    manifest: dict[str, Any],
    chunks: list[KnowledgeChunk],
    *,
    query_embedder=None,
) -> int:
    embedding_payload = manifest.get("embedding") or {}
    if isinstance(embedding_payload, bool):
        embedding_payload = {"auto": embedding_payload}
    if not isinstance(embedding_payload, dict):
        raise KnowledgeImportError("embedding must be a JSON object or boolean.")
    if not embedding_payload.get("auto"):
        return 0

    model = (
        embedding_payload.get("model")
        or getattr(settings, "KNOWLEDGE_RAG_EMBEDDING_MODEL", "")
        or ""
    )
    if not model:
        raise KnowledgeImportError(
            "embedding.auto requires KNOWLEDGE_RAG_EMBEDDING_MODEL or embedding.model."
        )

    if query_embedder is None:
        from .retrieval import KnowledgeQueryEmbedder

        query_embedder = KnowledgeQueryEmbedder.from_settings()

    batch_size = max(1, int(embedding_payload.get("batch_size") or 32))
    imported = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = query_embedder.embed_documents([chunk.text for chunk in batch])
        if len(vectors) != len(batch):
            raise KnowledgeImportError(
                "Embedding provider returned an unexpected batch size."
            )
        for chunk, vector in zip(batch, vectors, strict=True):
            if not vector:
                raise KnowledgeImportError(
                    f"Embedding provider returned an empty vector for chunk {chunk.id}."
                )
            KnowledgeEmbedding.objects.update_or_create(
                chunk=chunk,
                embedding_model=model,
                defaults={
                    "embedding": vector,
                    "vector": vector,
                    "dimensions": len(vector),
                    "metadata": embedding_payload.get("metadata", {}),
                },
            )
            imported += 1
    return imported


def _load_json(path: Path) -> Any:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KnowledgeImportError(f"Invalid JSON in {path}: {exc}") from exc


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_access(value: str, path: str) -> None:
    if value not in {AccessLevel.PRIVATE, AccessLevel.PUBLIC}:
        raise KnowledgeImportError(f"{path} must be one of: private, public.")
