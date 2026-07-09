"""API tests for ``GET /api/search/`` — patches the OpenSearch client (no cluster).

Asserts the public endpoint returns the envelope on success, 503 when the
cluster is down (hard dependency), and 400 when ``q`` is missing.
"""

from __future__ import annotations

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
def test_search_api_503_when_cluster_down():
    client = MagicMock()
    client.search.side_effect = ConnectionError("down")
    with patch("search.service.make_client", return_value=client):
        resp = APIClient().get("/api/search/", {"q": "x"})
    assert resp.status_code == 503


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
