from __future__ import annotations

from typing import Any


def filter_public_sources(
    sources: list[dict[str, Any]],
    *,
    allowed_source_refs: list[str] | None = None,
    require_source_refs: bool = False,
) -> list[dict[str, Any]]:
    """Keep only retrieved sources that the structured answer cited."""
    cited_refs = {ref for ref in allowed_source_refs or [] if isinstance(ref, str)}
    safe_sources = []
    seen = set()
    for source in sources:
        if require_source_refs:
            source_ref = source.get("source_ref")
            if not source_ref or source_ref not in cited_refs:
                continue
        key = (
            source.get("source_ref"),
            source.get("type"),
            source.get("url"),
            source.get("title"),
            source.get("source_id"),
            source.get("document_id"),
            source.get("chunk_id"),
            source.get("page_start"),
            source.get("page_end"),
            source.get("citation_identifier"),
        )
        if key in seen:
            continue
        seen.add(key)
        safe_sources.append(source)
    return safe_sources
