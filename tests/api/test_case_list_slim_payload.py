"""
Regression tests for the slim case LIST payload.

The list endpoint (GET /api/cases/) uses CaseListSerializer, which drops
detail-only body fields to keep the payload (and the MCP search tool's LLM
context) small. The detail endpoint (GET /api/cases/{slug}/) must still expose
the full field set.

Also guards the entity-hydration query count: the viewset prefetches
``entity_relationships__entity`` and ``get_entities`` must reuse that cache, so
the number of queries does not grow with the number of cases on the page.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities

# Fields intentionally removed from the list response (present only on detail).
LIST_OMITTED_FIELDS = {
    "description",
    "timeline",
    "evidence",
    "notes",
    "missing_details",
    "versionInfo",
}

# A representative subset that must remain on the list response.
LIST_REQUIRED_FIELDS = {
    "id",
    "case_id",
    "slug",
    "case_type",
    "state",
    "title",
    "short_description",
    "thumbnail_url",
    "banner_url",
    "case_start_date",
    "case_end_date",
    "entities",
    "tags",
    "key_allegations",
    "court_cases",
    "bigo",
    "created_at",
    "updated_at",
}


def _make_published_case(**overrides):
    """Create a fully-populated PUBLISHED case so every field has content."""
    data = {
        "title": "Slim payload case",
        "case_type": CaseType.CORRUPTION,
        "state": CaseState.PUBLISHED,
        "short_description": "A short summary.",
        "description": "# Full markdown body that should not ship in the list.",
        "key_allegations": ["Allegation one", "Allegation two"],
        "timeline": [{"date": "2024-01-01", "title": "Event", "description": "x"}],
        "evidence": [{"source_id": "DOC-1", "description": "evidence"}],
        "notes": "Internal notes that must not leak into the list.",
        "missing_details": "Some missing details.",
        "alleged_entities": ["entity:person/test-a", "entity:person/test-b"],
    }
    data.update(overrides)
    return create_case_with_entities(**data)


@pytest.mark.django_db
def test_list_response_omits_detail_only_fields():
    """The list endpoint must not include the heavy detail-only body fields."""
    _make_published_case()

    response = APIClient().get("/api/cases/")
    assert response.status_code == 200

    results = response.data["results"]
    assert results, "expected at least one case in the list response"
    item = results[0]

    leaked = LIST_OMITTED_FIELDS & set(item.keys())
    assert not leaked, f"list response should not contain detail-only fields: {leaked}"

    missing = LIST_REQUIRED_FIELDS - set(item.keys())
    assert not missing, f"list response is missing expected fields: {missing}"


@pytest.mark.django_db
def test_list_returns_short_description_verbatim_when_blank():
    """Blank short_description is returned as-is (empty), with no fallback."""
    _make_published_case(short_description="")

    response = APIClient().get("/api/cases/")
    assert response.status_code == 200

    item = response.data["results"][0]
    assert item["short_description"] == ""
    # No full description leaks in as a fallback.
    assert "description" not in item


@pytest.mark.django_db
def test_detail_response_still_includes_full_fields():
    """The detail endpoint must still expose every field the list drops."""
    case = _make_published_case()

    response = APIClient().get(f"/api/cases/{case.slug}/")
    assert response.status_code == 200

    missing = LIST_OMITTED_FIELDS - set(response.data.keys())
    assert not missing, f"detail response is missing fields: {missing}"


@pytest.mark.django_db
def test_entity_hydration_is_constant_query_count():
    """
    Entity hydration must not be N+1: serializing 1 case and serializing 5
    cases should issue the same number of queries (the prefetch cache is
    reused by get_entities). Guards against reintroducing
    ``.select_related()`` on the entity_relationships manager.
    """
    client = APIClient()

    _make_published_case(title="Only case")
    with CaptureQueriesContext(connection) as ctx_one:
        assert client.get("/api/cases/").status_code == 200
    one_case_queries = len(ctx_one.captured_queries)

    for i in range(4):
        _make_published_case(title=f"Extra case {i}")
    with CaptureQueriesContext(connection) as ctx_many:
        assert client.get("/api/cases/").status_code == 200
    many_case_queries = len(ctx_many.captured_queries)

    assert many_case_queries == one_case_queries, (
        "query count grew with the number of cases — entity hydration is N+1: "
        f"{one_case_queries} query/queries for 1 case vs "
        f"{many_case_queries} for 5 cases"
    )
