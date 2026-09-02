"""Unit tests for the server-side search-analytics event builder.

Pure functions (no DB, no cluster, no log pipeline): assert the ``search_query``
event contract that a future ranking algorithm will consume — normalized query,
the zero-result gap flag, per-type counts, the top-hit anchor, and that NO user
identity is ever included.
"""

from __future__ import annotations

from unittest.mock import patch

from search.analytics import (
    build_click_event,
    build_search_event,
    emit_search_click_event,
    emit_search_event,
    normalize_query,
)


def test_normalize_query_lowercases_trims_and_collapses_whitespace():
    assert normalize_query("  Sher   Bahadur  Deuba ") == "sher bahadur deuba"


def test_normalize_query_blank_is_empty_string():
    assert normalize_query(None) == ""
    assert normalize_query("") == ""
    assert normalize_query("   ") == ""


def _params(**over):
    base = {
        "q": "akhtiyar",
        "lang": "both",
        "types": None,
        "sort": "relevance",
        "page": 1,
        "page_size": 10,
        "filters": {},
    }
    base.update(over)
    return base


def test_build_event_captures_demand_and_result_shape():
    response = {
        "count": 42,
        "counts": {"case": 2, "entity": 40},
        "results": [{"type": "entity", "score": 12.5}],
    }
    event = build_search_event(
        search_id="abc123", params=_params(), response=response, took_ms=7.04
    )
    assert event["search_id"] == "abc123"
    assert event["q_normalized"] == "akhtiyar"
    assert event["q_len"] == len("akhtiyar")
    assert event["has_query"] is True
    assert event["result_count"] == 42
    assert event["zero_result"] is False
    assert event["counts_by_type"] == {"case": 2, "entity": 40}
    assert event["returned"] == 1
    assert event["took_ms"] == 7.0
    # First-page top hit is recorded as the click-through anchor.
    assert event["top_type"] == "entity"
    assert event["top_score"] == 12.5


def test_build_event_flags_zero_result_only_for_real_queries():
    empty = {"count": 0, "counts": {}, "results": []}
    # A real query that returned nothing is the actionable gap signal.
    hit = build_search_event(
        search_id="x", params=_params(q="obscure name"), response=empty, took_ms=1.0
    )
    assert hit["zero_result"] is True
    # A browse (no query term) that returns nothing is NOT a zero-result miss.
    browse = build_search_event(
        search_id="x", params=_params(q=""), response=empty, took_ms=1.0
    )
    assert browse["has_query"] is False
    assert browse["zero_result"] is False


def test_build_event_flags_whether_a_zero_result_was_recoverable():
    """Design §18 wants the did-you-mean RATE — this flag over ``zero_result``. The
    FLAG only, never the suggested text: it is derived from ``q_normalized``, which
    the event already carries."""
    empty = {"count": 0, "counts": {}, "results": []}
    recoverable = build_search_event(
        search_id="x",
        params=_params(q="coruption"),
        response={**empty, "did_you_mean": "corruption"},
        took_ms=1.0,
    )
    assert recoverable["zero_result"] is True
    assert recoverable["did_you_mean"] is True
    # A miss with nothing to suggest is the residual gap the romanization work owns.
    dead_end = build_search_event(
        search_id="x",
        params=_params(q="melamchee"),
        response={**empty, "did_you_mean": None},
        took_ms=1.0,
    )
    assert dead_end["zero_result"] is True
    assert dead_end["did_you_mean"] is False


def test_build_event_omits_top_hit_beyond_first_page():
    response = {
        "count": 99,
        "counts": {"entity": 99},
        "results": [{"type": "entity", "score": 3.0}],
    }
    event = build_search_event(
        search_id="x", params=_params(page=2), response=response, took_ms=1.0
    )
    # top_* anchors click-through to the best answer SHOWN FIRST; deeper pages
    # would misattribute, so they are omitted there.
    assert "top_type" not in event
    assert "top_score" not in event


def test_build_event_omits_top_hit_on_cursor_pages():
    """A cursor-paginated deep page keeps page==1 (the service ignores page under
    a cursor), so it must NOT be treated as the first page for the click anchor."""
    response = {
        "count": 99,
        "counts": {"entity": 99},
        "results": [{"type": "entity", "score": 3.0}],
    }
    event = build_search_event(
        search_id="x",
        params=_params(page=1, cursor="opaque-token"),
        response=response,
        took_ms=1.0,
    )
    assert "top_type" not in event
    assert "top_score" not in event


def test_build_event_records_active_facets_and_sorted_types():
    event = build_search_event(
        search_id="x",
        params=_params(
            types=["entity", "case"],
            filters={"case_type": ["CORRUPTION"], "tags": []},
        ),
        response={"count": 1, "counts": {}, "results": []},
        took_ms=1.0,
    )
    # Types are sorted for stable aggregation; empty facet lists are dropped.
    assert event["types"] == ["case", "entity"]
    assert event["filters"] == {"case_type": ["CORRUPTION"]}


def test_build_event_records_active_range_bounds():
    """Which refine controls readers actually reach for is the point of this event,
    so the बिगो bounds are recorded — in their own key, since they are scalars
    rather than the term lists ``filters`` holds."""
    event = build_search_event(
        search_id="x",
        params=_params(ranges={"bigo_min": 10_000_000, "bigo_max": None}),
        response={"count": 1, "counts": {}, "results": []},
        took_ms=1.0,
    )
    assert event["ranges"] == {"bigo_min": 10_000_000}


def test_build_event_ranges_none_when_unbounded_and_keeps_a_zero_bound():
    """No bound → ``None`` (consistent with ``filters``). But ``0`` is a real
    bound: dropping it on falsiness would misreport the query that was run."""
    unbounded = build_search_event(
        search_id="x",
        params=_params(),
        response={"count": 0, "counts": {}, "results": []},
        took_ms=1.0,
    )
    assert unbounded["ranges"] is None
    zero = build_search_event(
        search_id="x",
        params=_params(ranges={"bigo_min": 0}),
        response={"count": 0, "counts": {}, "results": []},
        took_ms=1.0,
    )
    assert zero["ranges"] == {"bigo_min": 0}


def test_build_event_carries_no_user_identity():
    """The event is aggregate product telemetry — it must never carry identity."""
    event = build_search_event(
        search_id="x",
        params=_params(),
        response={"count": 1, "counts": {}, "results": []},
        took_ms=1.0,
    )
    forbidden = {"user", "user_id", "ip", "ip_address", "session", "user_agent", "referer"}
    assert forbidden.isdisjoint(event.keys())


def test_emit_search_event_never_raises():
    """The never-raise contract the view depends on: a builder failure is
    swallowed (logged), so telemetry can never turn a good search into a 500."""
    with patch(
        "search.analytics.build_search_event", side_effect=RuntimeError("boom")
    ):
        # Must NOT propagate.
        emit_search_event(
            search_id="x",
            params=_params(),
            response={"count": 0, "counts": {}, "results": []},
            took_ms=1.0,
        )


# ── click event (the other half of the loop) ────────────────────────────────────


def test_build_click_event_join_keys_and_carries_no_identity():
    event = build_click_event(
        search_id="abc123",
        rank=3,
        result_type="entity",
        result_id="https://jawafdehi.org/entity/person/x",
        result_score=9.5,
    )
    # Joins back to the query by search_id; carries the clicked result + its rank.
    assert event["search_id"] == "abc123"
    assert event["rank"] == 3
    assert event["result_type"] == "entity"
    assert event["result_id"] == "https://jawafdehi.org/entity/person/x"
    assert event["result_score"] == 9.5
    # No identity, ever.
    forbidden = {"user", "user_id", "ip", "session", "user_agent", "referer"}
    assert forbidden.isdisjoint(event.keys())


def test_build_click_event_omits_absent_score():
    event = build_click_event(
        search_id="x", rank=1, result_type="case", result_id="case:1"
    )
    assert "result_score" not in event


def test_emit_search_click_event_never_raises():
    with patch(
        "search.analytics.build_click_event", side_effect=RuntimeError("boom")
    ):
        emit_search_click_event(
            search_id="x", rank=1, result_type="case", result_id="case:1"
        )

