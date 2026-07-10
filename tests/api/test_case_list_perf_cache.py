"""Perf/caching regression tests for the case list endpoint (GET /api/cases/).

These lock in the fixes for the slow "Recently Documented Cases" home section:
  - anonymous (public) list responses are publicly cacheable so an immediate
    reload is served from cache instead of re-paying the query cost;
  - authenticated/casework list responses are NOT publicly cached (role-scoped);
  - the per-card entity + material resolution no longer scales the SQL query
    count with the number of cards (N+1 removed via batched resolution +
    material_references prefetch).
"""

import pytest
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from cases.api_views import CaseViewSet
from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities, create_user_with_role


def _make_published_case(i):
    return create_case_with_entities(
        title=f"Bhrastachar Case {i}",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        alleged_entities=[f"https://jawafdehi.org/entity/person/accused-{i}"],
        locations=[f"https://jawafdehi.org/entity/location/place-{i}"],
    )


@pytest.mark.django_db
def test_anonymous_list_is_publicly_cacheable():
    _make_published_case(1)
    resp = APIClient().get("/api/cases/")
    assert resp.status_code == 200
    assert resp["Cache-Control"] == CaseViewSet.LIST_CACHE_CONTROL


@pytest.mark.django_db
def test_authenticated_list_is_not_publicly_cached():
    _make_published_case(1)
    moderator = create_user_with_role("nisha_sharma", "nisha@test.com", "Moderator")
    client = APIClient()
    client.force_authenticate(user=moderator)
    resp = client.get("/api/cases/")
    assert resp.status_code == 200
    assert resp.get("Cache-Control") != CaseViewSet.LIST_CACHE_CONTROL


@pytest.mark.django_db
def test_list_query_count_is_constant_across_cards():
    """Query count must not grow per card (no N+1 on entity/material resolution)."""
    client = APIClient()

    _make_published_case(1)
    with CaptureQueriesContext(connection) as one_card:
        assert client.get("/api/cases/").status_code == 200

    for i in range(2, 6):  # now 5 cards on the page
        _make_published_case(i)
    with CaptureQueriesContext(connection) as five_cards:
        assert client.get("/api/cases/").status_code == 200

    # A per-card N+1 would make the 5-card page cost ~4×(per-card queries) more.
    # With batching + prefetch the count is flat (allow a tiny constant slack).
    assert len(five_cards) <= len(one_card) + 1, (
        f"query count scaled with cards: 1 card={len(one_card)}, "
        f"5 cards={len(five_cards)}"
    )
