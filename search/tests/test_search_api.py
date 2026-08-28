"""API tests for ``GET /api/search/`` — patches the OpenSearch client (no cluster).

Asserts the public endpoint returns the envelope on success, 503 when the
cluster is down (hard dependency), and 400 when ``q`` is missing.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient


def _canned():
    return {
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_index": "nes-entities",
                    "_id": "https://jawafdehi.org/entity/person/x",
                    "_score": 1.0,
                    "_source": {
                        "iri": "https://jawafdehi.org/entity/person/x",
                        "source_app": "nes",
                        "title_en": "X",
                        "type": "Person",
                        "raw": {},
                    },
                }
            ],
        },
        "aggregations": {
            "by_index": {"buckets": [{"key": "nes-entities", "doc_count": 1}]},
            "entity_type": {"buckets": [{"key": "Person", "doc_count": 1}]},
            "case_type": {"buckets": []},
            "tags": {"buckets": []},
        },
    }


@pytest.mark.django_db
def test_search_api_returns_envelope():
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["type"] == "entity"
    assert body["counts"] == {"entity": 1}
    # Refine facets are exposed alongside the per-type counts.
    assert body["facets"]["entity_type"] == [{"name": "Person", "count": 1}]
    assert body["sort"] == "relevance"


@pytest.mark.django_db
def test_search_api_echoes_search_id_and_emits_analytics():
    """Every search echoes an ephemeral ``search_id`` (the click-loop join seam)
    and emits ONE server-side analytics event carrying that same id + the query."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client), patch(
        "search.views.emit_search_event"
    ) as emit:
        resp = APIClient().get("/api/search/", {"q": "Akhtiyar"})
    assert resp.status_code == 200
    search_id = resp.json()["search_id"]
    assert search_id
    emit.assert_called_once()
    kwargs = emit.call_args.kwargs
    assert kwargs["search_id"] == search_id
    assert kwargs["params"]["q"] == "Akhtiyar"
    assert kwargs["response"]["count"] == 1
    assert kwargs["took_ms"] >= 0


@pytest.mark.django_db
def test_search_api_503_when_cluster_down():
    client = MagicMock()
    client.search.side_effect = ConnectionError("down")
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "x"})
    assert resp.status_code == 503


@pytest.mark.django_db
def test_search_api_503_reports_to_sentry():
    """A search-backend outage returns a *handled* 503, so it must be reported to
    Sentry explicitly (the Django integration only sees unhandled exceptions, and
    the service logs the transport error at warning level, below Sentry's ERROR
    event threshold). Without this, search outages are invisible in Sentry."""
    client = MagicMock()
    client.search.side_effect = ConnectionError("down")
    with patch("search.service.make_client", return_value=client), patch(
        "search.views.sentry_sdk.capture_exception"
    ) as capture:
        resp = APIClient().get("/api/search/", {"q": "x"})
    assert resp.status_code == 503
    capture.assert_called_once()
    # The captured exception chains back to the underlying transport error.
    captured = capture.call_args.args[0]
    assert isinstance(captured.__cause__, ConnectionError)


@pytest.mark.django_db
def test_search_api_allows_empty_q_as_browse():
    """``q`` is optional: no query term is a browse (match-all), not a 400."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        # No q at all → 200 browse.
        resp = APIClient().get("/api/search/")
        assert resp.status_code == 200
        # Blank q → also 200.
        resp_blank = APIClient().get("/api/search/", {"q": ""})
        assert resp_blank.status_code == 200
    # The browse query is a match_all (not an empty multi_match that matches nothing).
    body = client.search.call_args.kwargs["body"]
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]


@pytest.mark.django_db
def test_search_api_envelope_carries_next_cursor_key():
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "x"})
    # The deep-paging cursor key is always present (null when no further page).
    assert "next_cursor" in resp.json()


@pytest.mark.django_db
def test_search_api_400_on_bad_cursor():
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "x", "cursor": "!!!garbage!!!"})
    # A malformed cursor is a client error (400), never a 503.
    assert resp.status_code == 400
    client.search.assert_not_called()


@pytest.mark.django_db
def test_search_api_passes_sort_and_filters_through():
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/",
            {"q": "x", "sort": "newest", "case_type": "CORRUPTION", "tags": ["a", "b"]},
        )
    assert resp.status_code == 200
    body = client.search.call_args.kwargs["body"]
    assert body["sort"][0] == {"date": {"order": "desc", "missing": "_last"}}
    filters = body["query"]["bool"]["filter"]
    assert {"terms": {"case_type": ["CORRUPTION"]}} in filters
    assert {"terms": {"keywords": ["a", "b"]}} in filters


@pytest.mark.django_db
def test_search_api_case_type_filter_normalized_to_upper():
    """A lowercase ``case_type`` filter must upper-case to match the indexed token
    (court-case case_type is normalized to upper at index time)."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "x", "case_type": "corruption"})
    assert resp.status_code == 200
    filters = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert {"terms": {"case_type": ["CORRUPTION"]}} in filters


@pytest.mark.django_db
def test_search_api_threads_status_facet_through():
    """The case-list ?type=case&status=ongoing filter reaches the OpenSearch DSL."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/", {"q": "", "type": "case", "status": "ongoing"}
        )
    assert resp.status_code == 200
    body = client.search.call_args.kwargs["body"]
    assert {"terms": {"case_status": ["ongoing"]}} in body["query"]["bool"]["filter"]


@pytest.mark.django_db
def test_search_api_threads_bigo_range_through():
    """?type=case&bigo_min=…&bigo_max=… reaches the DSL as ONE range clause."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/",
            {"q": "", "type": "case", "bigo_min": "10000000", "bigo_max": "100000000"},
        )
    assert resp.status_code == 200
    body = client.search.call_args.kwargs["body"]
    assert {
        "range": {"bigo": {"gte": 10_000_000, "lte": 100_000_000}}
    } in body["query"]["bool"]["filter"]


@pytest.mark.django_db
def test_search_api_bigo_min_alone_is_an_open_ended_lower_bound():
    """The common case — "cases over रु १ करोड" — needs no upper bound."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "", "bigo_min": "10000000"})
    assert resp.status_code == 200
    clauses = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert clauses == [{"range": {"bigo": {"gte": 10_000_000}}}]


@pytest.mark.django_db
def test_search_api_no_range_clause_when_no_bound_given():
    """An absent bound must not become an implicit ``bigo >= 0``, which would drop
    every non-case result from an ordinary search."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "x"})
    assert resp.status_code == 200
    assert client.search.call_args.kwargs["body"]["query"]["bool"]["filter"] == []


@pytest.mark.django_db
def test_search_api_400_on_inverted_bigo_range():
    """An inverted interval matches nothing — a 400 beats a confident empty page
    the reader would read as "no such cases"."""
    resp = APIClient().get(
        "/api/search/", {"q": "x", "bigo_min": "100", "bigo_max": "10"}
    )
    assert resp.status_code == 400
    assert "bigo_min" in json.dumps(resp.json())


@pytest.mark.django_db
def test_search_api_400_on_malformed_bigo_bounds():
    """Bad input is a client error, never a query sent on to OpenSearch.

    ``2**63`` overflows the ``long`` mapping: unbounded, it would come back from
    the cluster as a number_format_exception and surface as a 503.
    """
    for params in (
        {"bigo_min": "abc"},
        {"bigo_min": "-1"},
        {"bigo_max": "-5"},
        {"bigo_min": str(2**63)},
        {"bigo_max": "1e9"},
    ):
        resp = APIClient().get("/api/search/", {"q": "x", **params})
        assert resp.status_code == 400, params


@pytest.mark.django_db
def test_search_api_equal_bigo_bounds_are_allowed():
    """min == max is an exact-amount lookup, not an inverted range."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/", {"q": "x", "bigo_min": "500", "bigo_max": "500"}
        )
    assert resp.status_code == 200
    clauses = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert clauses == [{"range": {"bigo": {"gte": 500, "lte": 500}}}]


@pytest.mark.django_db
def test_search_api_passes_date_bounds_as_iso_strings():
    """?date_from/?date_to reach the DSL as ONE range clause of ISO STRINGS.

    Strings, not ``datetime.date`` objects: the serializer re-serializes after
    validating, so the OpenSearch body (and the analytics event) stay pure JSON
    regardless of any one consumer's encoder.
    """
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/",
            {"q": "", "date_from": "2020-01-01", "date_to": "2021-12-31"},
        )
    assert resp.status_code == 200
    clauses = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert clauses == [
        {"range": {"date": {"gte": "2020-01-01", "lte": "2021-12-31"}}}
    ]


@pytest.mark.django_db
def test_search_api_400_on_malformed_dates():
    """Bad input is a client error, never a query sent on to OpenSearch."""
    for params in (
        {"date_from": "abc"},
        {"date_to": "2024-13-01"},
        {"date_from": "2024-02-30"},
        {"date_from": "01/02/2024"},
    ):
        resp = APIClient().get("/api/search/", {"q": "x", **params})
        assert resp.status_code == 400, params


@pytest.mark.django_db
def test_search_api_400_on_inverted_date_interval():
    """from > to matches nothing — a 400 beats a confident empty page."""
    resp = APIClient().get(
        "/api/search/", {"q": "x", "date_from": "2022-01-01", "date_to": "2020-01-01"}
    )
    assert resp.status_code == 400
    assert "date_from" in json.dumps(resp.json())


@pytest.mark.django_db
def test_search_api_equal_date_bounds_are_allowed():
    """from == to is a single-day range, not an inverted interval."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/",
            {"q": "x", "date_from": "2020-06-15", "date_to": "2020-06-15"},
        )
    assert resp.status_code == 200
    clauses = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert clauses == [
        {"range": {"date": {"gte": "2020-06-15", "lte": "2020-06-15"}}}
    ]


@pytest.mark.django_db
def test_search_api_threads_court_type_through():
    """?court_type reaches the DSL as a terms filter on the promoted field."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/", {"q": "", "type": "courtcase", "court_type": "supreme"}
        )
    assert resp.status_code == 200
    body = client.search.call_args.kwargs["body"]
    assert {"terms": {"court_type": ["supreme"]}} in body["query"]["bool"]["filter"]


@pytest.mark.django_db
def test_search_api_400_on_unknown_court_type():
    """The vocabulary is CLOSED (district/high/supreme/special): a typo is a 400,
    not a confident empty page."""
    resp = APIClient().get("/api/search/", {"q": "x", "court_type": "municipal"})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_search_api_threads_a_multi_court_selection_through():
    """?court is repeatable, so an arbitrary set of courts ACROSS tiers lands in
    one terms clause — the selection court_type+district cannot express."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/",
            {"q": "", "type": "courtcase", "court": ["kathmandudc", "patanhc"]},
        )
    assert resp.status_code == 200
    body = client.search.call_args.kwargs["body"]
    assert {"terms": {"court": ["kathmandudc", "patanhc"]}} in body["query"]["bool"][
        "filter"
    ]


@pytest.mark.django_db
def test_search_api_400_on_unknown_court_identifier():
    """?court is CLOSED against the 97 real courts. Safe to be strict: a court
    absent from the scraper registry is a court with no cases to filter for."""
    resp = APIClient().get("/api/search/", {"q": "x", "court": "atlantisdc"})
    assert resp.status_code == 400
    # And the tier vocabulary is NOT accepted here — ?court takes identifiers.
    assert APIClient().get(
        "/api/search/", {"q": "x", "court": "district"}
    ).status_code == 400


@pytest.mark.django_db
def test_search_api_threads_district_and_province_through():
    """?district/?province reach the DSL as terms filters on the court_* fields."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/",
            {
                "q": "",
                "type": "courtcase",
                "district": "Kathmandu",
                "province": "Bagmati",
            },
        )
    assert resp.status_code == 200
    clauses = client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
    assert {"terms": {"court_district": ["Kathmandu"]}} in clauses
    assert {"terms": {"court_province": ["Bagmati"]}} in clauses


@pytest.mark.django_db
def test_search_api_threads_facet_q_through():
    """?facet_q=<facet>:<text> adds an include regex to that facet's agg only,
    leaving the query itself untouched."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/", {"q": "x", "facet_q": "tags:घुस"}
        )
    assert resp.status_code == 200
    body = client.search.call_args.kwargs["body"]
    assert body["aggs"]["tags"]["terms"]["include"] == ".*घुस.*"
    assert "include" not in body["aggs"]["case_type"]["terms"]
    assert body["query"]["bool"]["filter"] == []


@pytest.mark.django_db
def test_search_api_facet_q_text_may_contain_colons():
    """Only the FIRST colon separates facet from text."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/", {"q": "x", "facet_q": "tags:a:b"}
        )
    assert resp.status_code == 200
    body = client.search.call_args.kwargs["body"]
    assert body["aggs"]["tags"]["terms"]["include"] == ".*[aA]:[bB].*"


@pytest.mark.django_db
def test_search_api_400_on_malformed_facet_q():
    """Bad input is a client error, never a query sent on to OpenSearch."""
    for value in ("tagsx", "tags:", ":घुस", "bogus:x"):
        resp = APIClient().get("/api/search/", {"q": "x", "facet_q": value})
        assert resp.status_code == 400, value


@pytest.mark.django_db
def test_search_api_400_on_duplicate_facet_q_facet():
    """Two facet_q for one facet is ambiguous — refuse rather than pick one."""
    resp = APIClient().get(
        "/api/search/",
        [("q", "x"), ("facet_q", "tags:a"), ("facet_q", "tags:b")],
    )
    assert resp.status_code == 400


def test_every_facet_field_has_an_agg_and_a_serializer_field():
    """``FACET_FIELDS`` and the serializer have to grow together — the view's
    ``active_filters`` comprehension reads ``validated_data``, and DRF discards
    any query param the serializer does not declare. The mirror of
    ``test_every_range_field_is_declared_on_the_query_serializer`` below.
    """
    from search.service import FACET_FIELDS
    from search.views import SearchQuerySerializer

    undeclared = set(FACET_FIELDS) - set(SearchQuerySerializer().get_fields())
    assert not undeclared, (
        "these FACET_FIELDS params reach no serializer field, so the API will "
        f"accept and silently ignore them: {sorted(undeclared)}"
    )


def test_every_range_field_is_declared_on_the_query_serializer():
    """``RANGE_FIELDS`` and the serializer have to grow together.

    ``RANGE_FIELDS``' own comment promises that adding ``date_from``/``date_to``
    is "two entries here … and nothing else", and the view's says its
    ``active_ranges`` comprehension is driven off ``RANGE_FIELDS`` "so adding
    them there does not silently fail to reach the service". Both overstate it:
    the comprehension reads ``serializer.validated_data``, and DRF discards any
    query param the serializer does not declare. An entry with no matching field
    is therefore ``None`` on every request — no clause, no 400, no log, just a
    bound that looks accepted and does nothing.

    This is the assertion that turns that silent no-op into a red test.
    """
    from search.service import RANGE_FIELDS
    from search.views import SearchQuerySerializer

    undeclared = set(RANGE_FIELDS) - set(SearchQuerySerializer().get_fields())
    assert not undeclared, (
        "these RANGE_FIELDS params reach no serializer field, so the API will "
        f"accept and silently ignore them: {sorted(undeclared)}"
    )


@pytest.mark.django_db
def test_search_api_400_on_invalid_sort():
    resp = APIClient().get("/api/search/", {"q": "x", "sort": "bogus"})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_search_api_type_all_searches_every_type():
    """``?type=all`` is the SPA's default/reset sentinel — it must search every
    type (like omitting ``type``), NOT 200-with-an-error-body. Regression: the
    ChoiceField rejected ``all`` as invalid, so the response carried a validation
    error while still returning 200, silently yielding zero results.
    """
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "roads", "type": "all"})
    assert resp.status_code == 200
    body = resp.json()
    assert "count" in body and body.get("count") is not None
    # ``all`` normalizes to no type filter → the query hits the multi-index search.
    index = client.search.call_args.kwargs.get("index")
    assert index is None or "," in str(index) or isinstance(index, (list, tuple))


@pytest.mark.django_db
def test_search_api_400_on_invalid_type():
    resp = APIClient().get("/api/search/", {"q": "x", "type": "bogus"})
    assert resp.status_code == 400


# ── POST /api/search/click (the result-click beacon) ────────────────────────────


@pytest.mark.django_db
def test_search_click_beacon_emits_event():
    """A valid click beacon is accepted (204) and emits one ``search_click`` event
    carrying the join key + clicked result."""
    payload = {
        "search_id": "sid-1",
        "rank": 4,
        "result_type": "case",
        "result_id": "/case/some-slug",
        "result_score": 8.1,
    }
    with patch("search.views.emit_search_click_event") as emit:
        resp = APIClient().post("/api/search/click", payload, format="json")
    assert resp.status_code == 204
    emit.assert_called_once_with(
        search_id="sid-1",
        rank=4,
        result_type="case",
        result_id="/case/some-slug",
        result_score=8.1,
    )


@pytest.mark.django_db
def test_search_click_beacon_accepts_text_plain_body():
    """navigator.sendBeacon posts text/plain (CORS-safelisted, no preflight); the
    view parses the raw body rather than 415-ing on the media type."""
    body = json.dumps(
        {"search_id": "sid-2", "rank": 1, "result_type": "entity", "result_id": "e:1"}
    )
    with patch("search.views.emit_search_click_event") as emit:
        resp = APIClient().post(
            "/api/search/click", data=body, content_type="text/plain"
        )
    assert resp.status_code == 204
    emit.assert_called_once()
    assert emit.call_args.kwargs["search_id"] == "sid-2"


@pytest.mark.django_db
def test_search_click_beacon_swallows_invalid_payload():
    """Best-effort: an invalid or garbage beacon still returns 204 and emits
    nothing (a beacon cannot read the response, so a 400 would be pointless)."""
    with patch("search.views.emit_search_click_event") as emit:
        # Missing required fields.
        r1 = APIClient().post(
            "/api/search/click", {"rank": 1}, format="json"
        )
        # Not even JSON.
        r2 = APIClient().post(
            "/api/search/click", data="not json", content_type="text/plain"
        )
    assert r1.status_code == 204
    assert r2.status_code == 204
    emit.assert_not_called()
