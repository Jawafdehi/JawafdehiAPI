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

from jawafdehi_shared.search.transliterate import to_devanagari, to_roman

logger = logging.getLogger("jawafdehi.search.index")


# Devanagari Unicode block (covers the Nepali script range we care about).
_DEVANAGARI_RANGE = range(0x0900, 0x0980)


def has_devanagari(text: str | None) -> bool:
    """True if ``text`` contains at least one Devanagari codepoint."""
    if not text:
        return False
    return any(ord(ch) in _DEVANAGARI_RANGE for ch in text)


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
    """
    parts: list[str] = []
    if title_ne:
        roman = to_roman(title_ne)
        if roman:
            parts.append(roman)
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
        # opensearch-py raises NotFoundError (a subclass of TransportError with
        # status_code 404). Treat "already gone" as success; re-raise anything
        # else so best_effort / the command can see a real failure.
        status = getattr(exc, "status_code", None)
        if status == 404 or str(status) == "404":
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
