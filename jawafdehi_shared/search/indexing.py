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


def stream_bulk(client, index: str, docs: Iterable[dict[str, Any]]) -> int:
    """Bulk-index ``docs`` into ``index`` via the streaming bulk helper.

    Each doc is upserted by its ``iri`` (document ``_id``). Returns the number of
    docs submitted. Uses ``opensearchpy.helpers.bulk`` lazily so the module
    stays importable without the optional dependency.
    """
    from opensearchpy.helpers import bulk  # lazy: optional dependency

    def actions() -> Iterable[dict[str, Any]]:
        for doc in docs:
            yield {"_index": index, "_id": doc["iri"], "_source": doc}

    count = 0

    def counting() -> Iterable[dict[str, Any]]:
        nonlocal count
        for action in actions():
            count += 1
            yield action

    bulk(client, counting())
    return count


def stream_bulk_delete(client, index: str, iris: Iterable[str]) -> int:
    """Bulk-delete ``iris`` from ``index``. Returns the number submitted.

    Batched rather than per-doc because the caller (the rebuild catch-up) issues
    a tombstone for every changed row its gate rejects, and rejection is the
    COMMON case — only ~1.2% of court cases are public, so a busy window is tens
    of thousands of tombstones for docs that were mostly never indexed.

    Those per-item 404s are the expected result, not a failure, so errors are
    collected instead of raised; anything that is NOT a 404 is re-raised, since
    that means the eviction genuinely did not happen.
    """
    from opensearchpy.helpers import bulk  # lazy: optional dependency

    ids = list(iris)
    if not ids:
        return 0
    actions = [{"_op_type": "delete", "_index": index, "_id": iri} for iri in ids]
    _, errors = bulk(client, actions, raise_on_error=False, stats_only=False)
    real = [
        err
        for err in errors
        if str((err.get("delete") or {}).get("status")) not in ("404", "200")
    ]
    if real:
        raise RuntimeError(f"bulk delete from {index} failed: {real[:3]}")
    return len(ids)
