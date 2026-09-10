"""Server-side search analytics: one structured event per ``/api/search`` call.

This is INSTRUMENTATION ONLY — it does not change ranking. Its purpose is to
accumulate the demand + result-quality signals we need to build a data-driven
ranking algorithm LATER, once these events have adequate volume (the plan's
"relevance-weight tuning DEFERRED to future"). Tuning the query blind — with no
record of what people actually search or which queries return nothing — would be
guesswork; this closes that gap first.

What each event captures:

* **demand** — the normalized query text and its length, the requested type
  filter / language / sort / paging, and the active refine facets. This is the
  UNBIASED denominator: it is emitted for every request server-side, unlike GA4
  (consent-gated to ~a quarter of humans) which only sees a fraction of traffic.
* **result quality** — the total hit count, a ``zero_result`` flag (the single
  most actionable gap signal — a real query the corpus/analyzers could not
  answer), a ``did_you_mean`` flag saying whether a spelling correction was
  offered (design §18's did-you-mean RATE — see the field below for the
  denominator, which is NOT ``zero_result``), the
  per-type counts (which index satisfied the demand), and the top hit's type/score
  on the first page (a coarse "was the best answer strong" signal, and the join
  target for click-through analysis).
* **latency** — wall-clock time of the OpenSearch call, so slow queries surface.

Privacy: NO user identity is recorded — no id, IP, user-agent, session, or
referer. The query text is normalized (case/whitespace) but not hashed, because
the query text IS the signal being collected; it is never attached to a person.
Each event carries an ephemeral ``search_id`` so a future client-side
result-click beacon can be correlated into (query -> shown -> clicked)
learning-to-rank judgments WITHOUT ever identifying who searched. The events are
meant for a SHORT-retention stream (route by the ``jawafdehi.search.analytics``
logger name); they are aggregate product telemetry, not an audit trail.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

# A dedicated logger (distinct from ``jawafdehi.search``, which carries the 503
# transport warnings) so the log pipeline can route these product-telemetry events
# to their own short-retention stream by logger name.
logger = logging.getLogger("jawafdehi.search.analytics")

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_query(q: str | None) -> str:
    """Normalize a raw query for aggregation: NFC + trim + lowercase + collapse
    internal whitespace. Returns ``""`` for a blank/browse query.

    NFC so a Devanagari term typed composed vs decomposed aggregates as one; lower
    + whitespace-collapse so ``"  Sher   Deuba "`` and ``"sher deuba"`` are the
    same demand bucket. Not hashed — the query text is the signal we collect.
    """
    if not q:
        return ""
    normalized = unicodedata.normalize("NFC", q).strip().lower()
    return _WHITESPACE_RE.sub(" ", normalized)


def build_search_event(
    *,
    search_id: str,
    params: dict[str, Any],
    response: dict[str, Any],
    took_ms: float,
) -> dict[str, Any]:
    """Build the ``search_query`` event payload (a flat, JSON-serializable dict).

    Pure/inspectable so the field contract is unit-tested without touching the log
    pipeline. ``params`` carries the validated request inputs (``q``, ``lang``,
    ``types``, ``sort``, ``page``, ``page_size``, ``filters``, ``ranges``);
    ``response`` is the :class:`SearchService` envelope (``count``, ``counts``,
    ``results``).

    ``types`` is emitted as a sorted list; an empty list means "all types" (no
    filter). ``top_type``/``top_score`` are recorded only for the TRUE first page
    with at least one hit (offset ``page==1`` AND no ``cursor``) — they anchor
    click-through analysis to the best answer shown, and a cursor-paginated deep
    page keeps ``page==1`` (the service ignores ``page`` under a cursor), so it
    must be excluded or the anchor would misattribute to a deep page.
    """
    q_normalized = normalize_query(params.get("q"))
    has_query = bool(q_normalized)
    count = int(response.get("count") or 0)
    results = response.get("results") or []
    page = params.get("page", 1)
    active_filters = {
        facet: values for facet, values in (params.get("filters") or {}).items() if values
    }
    # Range bounds (bigo_min/bigo_max) ride in their own key, not folded into
    # ``filters``: they are scalars, not term lists, and the emptiness test above
    # is truthiness — which would quietly discard a real ``bigo_min=0``.
    active_ranges = {
        param: bound
        for param, bound in (params.get("ranges") or {}).items()
        if bound is not None
    }

    event: dict[str, Any] = {
        "search_id": search_id,
        "q_normalized": q_normalized,
        "q_len": len(q_normalized),
        "has_query": has_query,
        "lang": params.get("lang"),
        "types": sorted(params.get("types") or []),
        "sort": params.get("sort"),
        "page": page,
        "page_size": params.get("page_size"),
        "filters": active_filters or None,
        "ranges": active_ranges or None,
        "result_count": count,
        # The key gap signal: a real query the corpus/analyzers could not answer.
        # A browse (no query term) that returns nothing is NOT a zero-result miss.
        "zero_result": has_query and count == 0,
        # Whether a spelling correction was offered. A flag, not the text — the
        # suggestion is derived from ``q_normalized``, which is already captured
        # above.
        #
        # NOT a subset of ``zero_result``, so do not divide the two. This fires on
        # either of design §11's triggers, and the second one — a result set with
        # no exactly-matching anchor — is by definition a search that RETURNED
        # something. Dividing by ``zero_result`` would mix an empty-state recovery
        # rate with a spelling-hint rate and can exceed 1. For §18's rate, use
        # queries carrying at least one fuzzy-eligible token as the denominator;
        # ``q_normalized`` is recorded, so it is recoverable from the stream
        # without a new field.
        "did_you_mean": bool(response.get("did_you_mean")),
        "counts_by_type": response.get("counts") or {},
        "returned": len(results),
        "took_ms": round(took_ms, 1),
    }

    # Only the TRUE first page anchors the click-through analysis. A cursor page
    # keeps page==1 (the service ignores page under a cursor), so exclude it.
    if page == 1 and not params.get("cursor") and results:
        top = results[0]
        event["top_type"] = top.get("type")
        event["top_score"] = top.get("score")

    return event


def emit_search_event(
    *,
    search_id: str,
    params: dict[str, Any],
    response: dict[str, Any],
    took_ms: float,
) -> None:
    """Emit one ``search_query`` analytics event. Never raises.

    Analytics is best-effort: a bug here must NEVER turn a good search response
    into an error, so payload construction and logging are wrapped — a failure is
    logged and swallowed. The event fields ride in ``extra`` so the structlog JSON
    formatter renders them as top-level keys (the log message ``"search_query"``
    becomes the ``event`` field).
    """
    try:
        event = build_search_event(
            search_id=search_id,
            params=params,
            response=response,
            took_ms=took_ms,
        )
        logger.info("search_query", extra=event)
    except Exception:  # noqa: BLE001 — telemetry must never break the response.
        logger.warning("search analytics emit failed", exc_info=True)


def build_click_event(
    *,
    search_id: str,
    rank: int,
    result_type: str,
    result_id: str,
    result_score: float | None = None,
) -> dict[str, Any]:
    """Build the ``search_click`` event payload (a flat, JSON-serializable dict).

    The other half of the click loop: it join-keys back to a ``search_query`` event
    by ``search_id``, so ``(query -> shown -> clicked)`` learning-to-rank judgments
    can be reconstructed WITHOUT any user identity. ``rank`` is the clicked result's
    1-based position in the full result order (page offset applied), ``result_type``
    the index it came from, ``result_id`` its public IRI, and ``result_score`` the
    relevance score it was shown with (the label side of the training signal).
    """
    event: dict[str, Any] = {
        "search_id": search_id,
        "rank": rank,
        "result_type": result_type,
        "result_id": result_id,
    }
    if result_score is not None:
        event["result_score"] = result_score
    return event


def emit_search_click_event(
    *,
    search_id: str,
    rank: int,
    result_type: str,
    result_id: str,
    result_score: float | None = None,
) -> None:
    """Emit one ``search_click`` analytics event. Never raises (best-effort)."""
    try:
        event = build_click_event(
            search_id=search_id,
            rank=rank,
            result_type=result_type,
            result_id=result_id,
            result_score=result_score,
        )
        logger.info("search_click", extra=event)
    except Exception:  # noqa: BLE001 — telemetry must never break the response.
        logger.warning("search click emit failed", exc_info=True)
