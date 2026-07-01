"""Shared bulk-reindex driver for the per-app management commands.

Each app's ``reindex_<x>`` command supplies (index name, a queryset/iterable of
records, the app's ``build_doc``) and this driver does the rest: ensure the index
exists (optionally drop+recreate on ``--rebuild``), stream records through
``build_doc``, and bulk-index them. Records that ``build_doc`` skips (return a doc
with no ``iri``) are not indexed — this is how the case command drops
non-published cases.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from jawafdehi_shared.search.indexing import stream_bulk
from jawafdehi_shared.search.opensearch import create_index, make_client


def reindex(
    *,
    index: str,
    records: Iterable[Any],
    build_doc: Callable[[Any], dict[str, Any]],
    rebuild: bool = False,
    client=None,
    batch_size: int = 500,
) -> dict[str, int]:
    """Bulk-(re)index ``records`` into ``index``.

    * ``rebuild``: drop the index first (then recreate with the bilingual config).
    * Records whose ``build_doc`` yields no ``iri`` are skipped (e.g. a
      non-published case).

    Returns ``{"indexed": n, "skipped": m}``.
    """
    client = client or make_client()

    if rebuild and client.indices.exists(index=index):
        client.indices.delete(index=index)
    create_index(client, index)

    indexed = 0
    skipped = 0
    batch: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal indexed
        if batch:
            indexed += stream_bulk(client, index, batch)
            batch.clear()

    for record in records:
        doc = build_doc(record)
        if not doc.get("iri"):
            skipped += 1
            continue
        batch.append(doc)
        if len(batch) >= batch_size:
            flush()
    flush()

    # Make freshly-indexed docs searchable immediately (useful for the caller's
    # post-reindex verification / a follow-on count).
    try:
        client.indices.refresh(index=index)
    except Exception:  # noqa: BLE001 — refresh is a nicety, not a contract.
        pass

    return {"indexed": indexed, "skipped": skipped}
