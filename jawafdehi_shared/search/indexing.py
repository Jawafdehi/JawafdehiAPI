"""Shared helpers for the per-app unified-search indexers.

The four per-app indexers (entities, materials, court cases, cases) each own
their record→doc projection, but they share a few low-level concerns that this
module centralizes so the projections stay consistent:

* extracting a bilingual (Devanagari/Roman) title from a schema.org ``name``
  (which may be a string OR a ``{"ne": ..., "en": ...}`` language map),
* deriving the ``title_translit`` field via the single shared transliteration
  (``jawafdehi_shared.search.transliterate``),
* a tiny ``best_effort`` wrapper so an indexing failure is logged and swallowed
  (the DB is the source of truth; the index is a derived, best-effort projection
  — an OpenSearch hiccup must never break a write),
* an ``upsert_doc`` / ``delete_doc`` pair that talk to the OpenSearch client by
  ``_id == iri``.

Nothing here imports Django, so it stays unit-testable with a mocked client.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from jawafdehi_shared.search.opensearch import (
    get_bulk_chunk_size,
    get_bulk_initial_backoff,
    get_bulk_max_backoff,
    get_bulk_max_chunk_bytes,
    get_bulk_max_retries,
)
from jawafdehi_shared.search.transliterate import (
    to_devanagari,
    to_roman,
    to_roman_colloquial,
)

logger = logging.getLogger("jawafdehi.search.index")


# Devanagari Unicode block (covers the Nepali script range we care about).
_DEVANAGARI_RANGE = range(0x0900, 0x0980)


def has_devanagari(text: str | None) -> bool:
    """True if ``text`` contains at least one Devanagari codepoint."""
    if not text:
        return False
    return any(ord(ch) in _DEVANAGARI_RANGE for ch in text)


def flatten_strings(value: Any) -> list[str]:
    """Collect non-empty stripped strings from a string / language-map / list.

    Used by the per-app indexers to fold a JSON-LD value of any shape into the
    flat string lists the common index doc wants (``body``, ``keywords``,
    ``identifiers``). Recurses through dict values and sequences.
    """
    out: list[str] = []
    if isinstance(value, str):
        if value.strip():
            out.append(value.strip())
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(flatten_strings(v))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(flatten_strings(item))
    return out


def type_token(atype: Any) -> str:
    """Render a JSON-LD ``@type`` as the index's single ``type`` token.

    A list ``@type`` is comma-joined, matching how the promoted
    ``entity_type``/``material_type`` columns store a multi-type document.
    """
    if isinstance(atype, list):
        return ",".join(str(t) for t in atype)
    return str(atype) if atype is not None else ""


def name_to_titles(name: Any) -> tuple[str | None, str | None]:
    """Split a schema.org ``name`` into ``(title_ne, title_en)``.

    ``name`` may be:
    * a language map ``{"ne": "...", "en": "..."}`` — keys are read directly
      (``np`` is accepted as an alias for ``ne``),
    * a plain string — bucketed into ``ne`` or ``en`` by script (Devanagari →
      ``ne``, otherwise ``en``),
    * a list — the first usable string/map wins.

    Either side may be ``None`` when the source only carries one language.
    """
    if name is None:
        return None, None
    if isinstance(name, str):
        s = name.strip()
        if not s:
            return None, None
        if has_devanagari(s):
            return s, None
        return None, s
    if isinstance(name, dict):
        ne = name.get("ne") or name.get("np")
        en = name.get("en")
        ne = ne.strip() if isinstance(ne, str) and ne.strip() else None
        en = en.strip() if isinstance(en, str) and en.strip() else None
        # If only an untagged value is present, fall back to script bucketing.
        if ne is None and en is None:
            for v in name.values():
                if isinstance(v, str) and v.strip():
                    return name_to_titles(v.strip())
        return ne, en
    if isinstance(name, (list, tuple)):
        for item in name:
            ne, en = name_to_titles(item)
            if ne or en:
                return ne, en
    return None, None


def title_translit(title_ne: str | None, title_en: str | None) -> str | None:
    """Build the ingest-side ``title_translit`` recall field.

    Emits the cross-script romanization of the Devanagari title AND the
    Devanagari-ization of the Roman title, joined, so a query in either script
    has an ingest-side bridge in addition to the in-engine ICU one. Returns
    ``None`` when both sides are empty.

    The Devanagari side contributes BOTH the scholarly IAST form ("bharata") and a
    colloquial, schwa-deleted, ASCII form ("bharat") so that both a diacritic-exact
    query and the way people actually type Nepali names in Latin ("Bharat") match.
    """
    parts: list[str] = []
    if title_ne:
        roman = to_roman(title_ne)
        if roman:
            parts.append(roman)
        colloquial = to_roman_colloquial(title_ne)
        if colloquial:
            parts.append(colloquial)
    if title_en:
        deva = to_devanagari(title_en)
        if deva:
            parts.append(deva)
        # Also carry the roman title itself so the translit field is a superset.
        parts.append(title_en)
    if not parts:
        return None
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)


def best_effort(action: str) -> Callable[[Callable], Callable]:
    """Decorator: run an indexer call, log+swallow any error, never raise.

    ``action`` is a short label used in the log line (e.g. ``"index entity"``).
    Returns the wrapped function's result on success, ``None`` on failure.
    """

    def decorator(func: Callable) -> Callable:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception:  # noqa: BLE001 — best-effort by contract.
                logger.warning("unified-search %s failed", action, exc_info=True)
                return None

        wrapper.__name__ = getattr(func, "__name__", "wrapper")
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator


def is_not_found(exc: Exception) -> bool:
    """True for a 404 from the cluster, False for every other failure.

    opensearch-py raises NotFoundError (a TransportError subclass with
    ``status_code`` 404) for an absent index/alias/doc. Callers that mean
    "absent" must test for THAT, not for ``Exception``: a timeout, a 5xx or an
    auth failure is not an empty result, and treating it as one is how a
    rebuild ends up deciding a live alias does not exist.
    """
    status = getattr(exc, "status_code", None)
    return status == 404 or str(status) == "404"


def upsert_doc(client, index: str, doc: dict[str, Any]) -> None:
    """Upsert ``doc`` into ``index`` keyed by its ``iri`` (the document ``_id``).

    Raises on transport error — callers wrap with ``best_effort`` for the live
    (signal) path; the bulk-reindex path lets it surface so a broken cluster
    fails the management command loudly.
    """
    iri = doc["iri"]
    client.index(index=index, id=iri, body=doc)


def delete_doc(client, index: str, iri: str) -> None:
    """Delete the doc keyed by ``iri`` from ``index``.

    A 404 (document not present) is treated as success — deleting an absent doc
    is a no-op, which is what the caller wants (e.g. unpublishing a case that
    was never indexed).
    """
    try:
        client.delete(index=index, id=iri)
    except Exception as exc:  # noqa: BLE001
        # Treat "already gone" as success; re-raise anything else so
        # best_effort / the command can see a real failure.
        if is_not_found(exc):
            return
        raise


#: How many failed items to quote in the error a failed bulk raises. Bounded so
#: a chunk-wide failure cannot turn the exception message into the payload.
_FAILURE_SAMPLE = 3


def _bulk_kwargs() -> dict[str, Any]:
    """The bounded ``streaming_bulk`` configuration both bulk paths share.

    ``raise_on_error=False`` is what lets the caller decide which per-item
    statuses are real failures — a 404 means something different to an upsert
    than to an eviction — and it keeps opensearch-py from attaching the source
    document to each error item (``_process_bulk_chunk_success`` only does that
    on the ``raise_on_error`` branch). ``raise_on_exception=True`` is deliberate
    and opposite: a transport error is a broken cluster, not a bad document, and
    must fail the command loudly rather than be counted as N failed docs.
    """
    return {
        "chunk_size": get_bulk_chunk_size(),
        "max_chunk_bytes": get_bulk_max_chunk_bytes(),
        "max_retries": get_bulk_max_retries(),
        "initial_backoff": get_bulk_initial_backoff(),
        "max_backoff": get_bulk_max_backoff(),
        "raise_on_error": False,
        "raise_on_exception": True,
        "yield_ok": False,
    }


def _describe_bulk_failure(info: dict[str, Any]) -> str:
    """One failed bulk item as a short string, keeping no reference to the doc.

    Projects down to op/id/status/reason and truncates the reason, so neither a
    wide failure nor one pathological error string can make the collected sample
    scale with the data. (Under ``raise_on_error=False`` opensearch-py does not
    attach the source document at all, but this must stay true if that ever
    changes — it is the caller's only guard.)
    """
    action, result = next(iter(info.items())) if info else ("index", {})
    result = result or {}
    error = result.get("error")
    if isinstance(error, dict):
        reason = error.get("reason") or error.get("type") or ""
    else:
        reason = error or ""
    return (
        f"{action} _id={result.get('_id')!r} "
        f"status={result.get('status')} {str(reason)[:200]}"
    )


def _item_status(info: dict[str, Any]) -> str:
    """The HTTP status of a single bulk item result, as a string."""
    _action, result = next(iter(info.items())) if info else ("index", {})
    return str((result or {}).get("status"))


def stream_bulk(client, index: str, docs: Iterable[dict[str, Any]]) -> int:
    """Bulk-index ``docs`` into ``index``. Returns the number of docs submitted.

    Each doc is upserted by its ``iri`` (document ``_id``). Imports
    ``opensearchpy.helpers`` lazily so the module stays importable without the
    optional dependency.

    Uses ``streaming_bulk`` rather than ``helpers.bulk`` for two reasons, neither
    of which is memory — ``bulk`` collects errors per CHUNK and raises at the end
    of each one, so its error list was already bounded by ``chunk_size``:

    * ``bulk`` hard-codes no retry, so a single ``429`` from a busy cluster
      failed an entire reindex. Backpressure is the one failure here that is
      expected to pass, and it is the one the old path could not survive.
    * it gives us a per-item view, so ``stream_bulk_delete`` can treat a 404 as
      success while this path treats it as a failure, without either of them
      re-deriving the other's policy.

    Bounding each request by BYTES as well as count (see
    ``get_bulk_max_chunk_bytes``) is a real improvement for ngm-materials, whose
    docs carry a whole JSON-LD body in ``raw`` — but note the caller controls the
    larger allocation: see ``reindex._stream``.

    Failures are fatal, as they were before. A reindex that silently skipped
    documents would hand ``reindex()`` a short index and, on a rebuild, swap the
    alias onto it.
    """
    from opensearchpy.helpers import streaming_bulk  # lazy: optional dependency

    submitted = 0

    def actions() -> Iterable[dict[str, Any]]:
        nonlocal submitted
        for doc in docs:
            submitted += 1
            yield {"_index": index, "_id": doc["iri"], "_source": doc}

    failed = 0
    sample: list[str] = []
    for ok, info in streaming_bulk(client, actions(), **_bulk_kwargs()):
        # Guarded rather than relying on yield_ok=False: if that flag is ever
        # flipped (progress logging is the obvious reason to) an unguarded
        # counter would score every SUCCESS as a failure and abort a clean run.
        if ok:
            continue
        failed += 1
        if len(sample) < _FAILURE_SAMPLE:
            sample.append(_describe_bulk_failure(info))

    if failed:
        logger.error(
            "bulk index into %s failed for %d/%d docs: %s",
            index,
            failed,
            submitted,
            sample,
        )
        raise RuntimeError(
            f"bulk index into {index} failed for {failed}/{submitted} docs; "
            f"first {len(sample)}: {sample}"
        )
    return submitted


def stream_bulk_delete(client, index: str, iris: Iterable[str]) -> int:
    """Bulk-delete ``iris`` from ``index``. Returns the number submitted.

    Batched rather than per-doc because the caller (the rebuild catch-up) issues
    a tombstone for every changed row its gate rejects, and rejection is the
    COMMON case — only ~1.2% of court cases are public, so a busy window is tens
    of thousands of tombstones for docs that were mostly never indexed.

    Those per-item 404s are the expected result, not a failure. Anything else is
    raised, since it means the eviction genuinely did not happen.

    Retries a 429 with backoff, which the previous ``helpers.bulk`` call could
    not: opensearch-py defaults per-chunk retries to 0, and 429 is not 404, so
    ONE rejected tombstone aborted the whole pass. That matters more here than on
    the indexing path because ``reindex`` runs this AFTER the alias swap — the
    new generation is already live, so an aborted catch-up leaves a row that was
    hidden mid-build still searchable, which for court cases is the sensitive-type
    floor being undone by a reindex.
    """
    from opensearchpy.helpers import streaming_bulk  # lazy: optional dependency

    ids = list(iris)
    if not ids:
        return 0

    def actions() -> Iterable[dict[str, Any]]:
        for iri in ids:
            yield {"_op_type": "delete", "_index": index, "_id": iri}

    failed = 0
    sample: list[str] = []
    for ok, info in streaming_bulk(client, actions(), **_bulk_kwargs()):
        if ok or _item_status(info) == "404":
            continue
        failed += 1
        if len(sample) < _FAILURE_SAMPLE:
            sample.append(_describe_bulk_failure(info))

    if failed:
        raise RuntimeError(
            f"bulk delete from {index} failed for {failed}/{len(ids)} docs; "
            f"first {len(sample)}: {sample}"
        )
    return len(ids)
