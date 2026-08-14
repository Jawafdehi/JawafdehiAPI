"""Shared bulk-reindex driver for the per-app management commands.

Each app's ``reindex_<x>`` command supplies (index name, a queryset/iterable of
records, the app's ``build_doc``) and this driver does the rest: ensure the index
exists, stream records through ``build_doc``, and bulk-index them. Records that
``build_doc`` skips (return a doc with no ``iri``) are not indexed — this is how
the case command drops non-published cases.

``rebuild=True`` no longer drops the live index. It builds a NEW generation
beside it and moves the alias when the generation is complete, so a rebuild is
invisible to readers and writers; see ``jawafdehi_shared.search.aliases`` for the
naming and the atomic swap. Two consequences worth knowing before touching this:

* A rebuild is now safe to schedule. It is the ONLY mode that evicts a doc whose
  row has since become hidden — an incremental pass can only ever add — so the
  drift that used to need a manual rebuild is now something a cron can fix.
* Writes that land DURING the build go to the old generation and would die at
  the swap. That would be a regression: the old in-place rebuild refilled the
  very index the live path was writing to, so those writes survived. ``catchup``
  closes it (see ``reindex``), and a command that passes ``records`` without a
  matching ``catchup`` is accepting that loss.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from jawafdehi_shared.search.aliases import (
    next_generation,
    prune_generations,
    resolve_alias,
    swap_alias,
)
from jawafdehi_shared.search.indexing import (
    is_not_found,
    stream_bulk,
    stream_bulk_delete,
)
from jawafdehi_shared.search.opensearch import create_index, make_client


class RebuildAborted(RuntimeError):
    """A rebuild produced no documents while the live index still had some.

    Raised INSTEAD of swapping, so the old generation keeps serving. This is the
    guard against a bug in a visibility gate silently emptying public search: the
    swap is atomic and the bootstrap branch deletes the old index, so without it
    one inverted predicate would wipe an index with no warning and no undo.
    """


def _doc_count(client, index: str) -> int:
    """Docs currently in ``index``; 0 if it is MISSING, raise if it is unreadable.

    The distinction is the whole point. This number decides whether a
    destructive swap is safe, so a timeout or a 5xx must not be allowed to read
    as "the old index was empty anyway".
    """
    try:
        return int(client.count(index=index)["count"])
    except Exception as exc:  # noqa: BLE001
        if is_not_found(exc):
            return 0
        raise


def _refresh(client, index: str) -> None:
    """Make freshly-indexed docs searchable now (for the caller's verification)."""
    try:
        client.indices.refresh(index=index)
    except Exception:  # noqa: BLE001 — refresh is a nicety, not a contract.
        pass


def _stream(
    client,
    index: str,
    records: Iterable[Any],
    build_doc: Callable[[Any], dict[str, Any]],
    batch_size: int,
) -> tuple[int, int]:
    """Bulk-index ``records`` into ``index``. Returns ``(indexed, skipped)``."""
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
    return indexed, skipped


def _catch_up(
    client,
    index: str,
    pairs: Iterable[tuple[str, Any]],
    build_doc: Callable[[Any], dict[str, Any]],
    batch_size: int,
) -> tuple[int, int]:
    """Apply post-build changes to ``index``. Returns ``(upserted, evicted)``.

    ``pairs`` is ``(iri, record_or_None)``: the command has already applied its
    own visibility gate, and a ``None`` record means "this one no longer belongs
    in the index". Evicting on None is not optional. Without it a row that became
    hidden DURING the build stays in the new generation — and since the live
    signal's delete went to the OLD generation, the swap would RESURRECT a doc
    the platform had already evicted. For court cases that is the sensitive-type
    floor being undone by a reindex.
    """
    upserted = 0
    evicted = 0
    batch: list[dict[str, Any]] = []
    tombstones: list[str] = []

    def flush() -> None:
        nonlocal upserted, evicted
        if batch:
            upserted += stream_bulk(client, index, batch)
            batch.clear()
        if tombstones:
            evicted += stream_bulk_delete(client, index, tombstones)
            tombstones.clear()

    for iri, record in pairs:
        # A tombstone for a doc the scan never indexed is a no-op, not an error —
        # and it is the common case, so both sides are batched.
        doc = {} if record is None else build_doc(record)
        if not doc.get("iri"):
            tombstones.append(iri)
        else:
            batch.append(doc)
        if len(batch) >= batch_size or len(tombstones) >= batch_size:
            flush()
    flush()
    return upserted, evicted


def reindex(
    *,
    index: str,
    records: Iterable[Any],
    build_doc: Callable[[Any], dict[str, Any]],
    rebuild: bool = False,
    client=None,
    batch_size: int = 500,
    catchup: Callable[[datetime], Iterable[tuple[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Bulk-(re)index ``records`` into ``index``.

    * ``rebuild``: build a new generation and swap the alias onto it when it is
      complete (no downtime, and the only mode that evicts).
    * ``catchup``: called with the UTC time the build started, and must yield
      ``(iri, record_or_None)`` for everything written since — ``None`` meaning
      the row no longer belongs in the index, which is EVICTED rather than
      skipped. Only meaningful with ``rebuild``. The alias flips BEFORE this
      runs, so anything written after the swap already lands on the new
      generation and this only has to cover the build window itself.
    * Records whose ``build_doc`` yields no ``iri`` are skipped (e.g. a
      non-published case).

    Returns ``{"indexed", "skipped", "index", "swapped", "caught_up", "evicted",
    "displaced", "pruned"}`` — ``index`` being the generation actually written,
    which is not ``index`` when a swap happened.
    """
    client = client or make_client()

    if not rebuild:
        create_index(client, index)
        indexed, skipped = _stream(client, index, records, build_doc, batch_size)
        _refresh(client, index)
        return {
            "indexed": indexed,
            "skipped": skipped,
            "index": index,
            "swapped": False,
            "caught_up": 0,
            "evicted": 0,
            "displaced": [],
            "pruned": [],
        }

    # Stamped BEFORE the scan so the catch-up window cannot miss a row written
    # while the first batch was still being built.
    started_at = datetime.now(timezone.utc)
    target = next_generation(client, index)
    # A fresh generation is created from the CURRENT mappings — this is what makes
    # a mapping migration land, since create_index no-ops on an existing index.
    create_index(client, target)

    indexed, skipped = _stream(client, target, records, build_doc, batch_size)
    _refresh(client, target)

    # Count what actually LANDED, not what was submitted: `indexed` is the number
    # of docs handed to the bulk helper, which is an upper bound on the number
    # the cluster stored. The swap is destructive, so it is gated on the real one.
    live = resolve_alias(client, index)
    live_count = _doc_count(client, live or index)
    if _doc_count(client, target) == 0 and live_count > 0:
        client.indices.delete(index=target, ignore_unavailable=True)
        raise RebuildAborted(
            f"{index}: rebuild produced 0 documents while the live index holds "
            f"{live_count}; refusing to swap. The old index is untouched and "
            f"still serving."
        )

    displaced = swap_alias(client, index, target)

    caught_up = 0
    evicted = 0
    if catchup is not None:
        caught_up, evicted = _catch_up(
            client, target, catchup(started_at), build_doc, batch_size
        )
        _refresh(client, target)

    pruned = prune_generations(client, index, keep=target)
    return {
        "indexed": indexed,
        "skipped": skipped,
        "index": target,
        "swapped": True,
        "caught_up": caught_up,
        "evicted": evicted,
        "displaced": displaced,
        "pruned": pruned,
    }


def summary(label: str, result: dict[str, Any]) -> str:
    """The one-line command output, identical in shape across the four commands."""
    line = f"{label}: indexed={result['indexed']} skipped={result['skipped']}"
    if result.get("swapped"):
        line += (
            f" swapped->{result['index']} caught_up={result['caught_up']}"
            f" evicted={result.get('evicted', 0)}"
            f" pruned={len(result['pruned'])}"
        )
    return line
