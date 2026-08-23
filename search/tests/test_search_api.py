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
            # tags is a filter agg (curated case tags only), so its buckets are
            # nested — and an entity-only hit puts no case docs in its scope.
            "tags": {"doc_count": 0, "values": {"buckets": []}},
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


# ── tags_limit: the client-requested tags-facet width ──────────────────────────
#
# design.md §12 asks for BOTH halves of the tag-filter rule — cap the initial list
# AND let the client request more. Capping alone just moves the unreachable tail
# from 50 values to 10.


def _tags_facet_size(client):
    """The tags facet's requested bucket count, out of the DSL the view sent."""
    body = client.search.call_args.kwargs["body"]
    return body["aggs"]["tags"]["aggs"]["values"]["terms"]["size"]


@pytest.mark.django_db
def test_search_api_tags_limit_defaults_to_ten():
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "x", "type": "case"})
    assert resp.status_code == 200
    assert _tags_facet_size(client) == 10


@pytest.mark.django_db
def test_search_api_tags_limit_widens_the_tags_facet():
    """The whole chain: query param → serializer → service → aggregation size."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/", {"q": "x", "type": "case", "tags_limit": "30"}
        )
    assert resp.status_code == 200
    assert _tags_facet_size(client) == 30


@pytest.mark.django_db
def test_search_api_400_on_out_of_range_tags_limit():
    """Bounded at both ends: 0 buckets is a pointless query and an unbounded width
    turns a filter panel into a full vocabulary dump."""
    for bad in ("0", "51", "-1"):
        resp = APIClient().get("/api/search/", {"q": "x", "tags_limit": bad})
        assert resp.status_code == 400, bad


@pytest.mark.django_db
def test_search_api_tags_limit_does_not_touch_the_result_page():
    """It widens the FACET only — page_size still governs the result list, so a
    client asking for more chips does not silently get more results."""
    client = MagicMock()
    client.search.return_value = _canned()
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get(
            "/api/search/", {"q": "x", "tags_limit": "40", "page_size": "5"}
        )
    assert resp.status_code == 200
    body = client.search.call_args.kwargs["body"]
    assert body["size"] == 5
    assert _tags_facet_size(client) == 40


@pytest.mark.django_db
def test_every_documented_query_param_is_declared_on_the_query_serializer():
    """The ``@extend_schema`` parameter list and ``SearchQuerySerializer`` have to
    grow together.

    DRF discards any query param the serializer does not declare, so a param
    documented in OpenAPI but never declared is accepted, advertised, and then
    silently ignored — no effect, no 400, no log. A caller reading the schema has
    no way to tell. This turns that silent no-op into a red test.

    Read off the GENERATED schema rather than the decorator, which needs a bound
    view instance to resolve its parameters.
    """
    import yaml
    from django.test import Client
    from django.urls import reverse

    from search.views import SearchQuerySerializer

    from search.service import MAX_TAGS_FACET_SIZE

    schema = yaml.safe_load(Client().get(reverse("schema")).content)
    params = schema["paths"]["/api/search/"]["get"]["parameters"]
    documented = {p["name"] for p in params if p.get("in") == "query"}
    # Discoverable, not just live — and with bounds a generated client can enforce
    # locally rather than discovering them from a 400.
    assert "tags_limit" in documented
    tags_limit = next(p for p in params if p["name"] == "tags_limit")
    assert tags_limit["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_TAGS_FACET_SIZE,
    }
    undeclared = documented - set(SearchQuerySerializer().get_fields())
    assert not undeclared, (
        "these documented query params reach no serializer field, so the API will "
        f"accept and silently ignore them: {sorted(undeclared)}"
    )


def test_tags_limit_is_bounded_by_the_service_constants():
    """The serializer's bounds are the service's, not a second copy of 1/10/50 that
    can drift from the aggregation it controls."""
    from search.service import MAX_TAGS_FACET_SIZE, TAGS_FACET_SIZE
    from search.views import SearchQuerySerializer

    field = SearchQuerySerializer().get_fields()["tags_limit"]
    assert field.required is False
    assert field.default == TAGS_FACET_SIZE
    assert field.max_value == MAX_TAGS_FACET_SIZE
    assert field.min_value == 1


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
