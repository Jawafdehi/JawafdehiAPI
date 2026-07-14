"""Perf/caching regression tests for the case list endpoint (GET /api/cases/).

These lock in the fixes for the slow "Recently Documented Cases" home section:
  - anonymous list responses are browser-cacheable (an immediate reload is
    free) but stay OUT of shared/CDN caches: the same URL serves a wider
    role-scoped list to authenticated caseworkers and Cloudflare can't vary
    the cache on auth, so a shared cache must not hold this snapshot;
  - authenticated/casework list responses are NOT cached at all (role-scoped);
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
def test_anonymous_list_is_browser_cacheable_but_not_shared_cacheable():
    """Anon list is role-scoped-by-URL: the browser may hold it briefly, but a
    shared/CDN cache must NOT store it. Cloudflare keys by URL and can't vary
    on auth, so a cached ``public`` snapshot would be served to a signed-in
    caseworker and hide their DRAFT/IN_REVIEW cases. So: ``private`` +
    ``max-age``, and explicitly NOT ``public``/``s-maxage`` — this guards
    against a future re-introduction of edge caching on this endpoint."""
    _make_published_case(1)
    resp = APIClient().get("/api/cases/")
    assert resp.status_code == 200
    cc = resp["Cache-Control"]
    assert cc == CaseViewSet.LIST_CACHE_CONTROL
    # Browser may cache briefly, but the response must stay out of shared
    # caches. Assert the directives literally (not just == the constant) so a
    # regression in the constant itself is caught here too.
    assert "max-age=60" in cc
    assert "private" in cc
    assert "public" not in cc
    assert "s-maxage" not in cc
    # Vary on the auth-bearing headers so the browser's own cache can't reuse
    # this anon entry for a post-login (cookie- or bearer-token) request that
    # is entitled to the wider, role-scoped list.
    vary = resp.get("Vary", "")
    assert "Cookie" in vary
    assert "Authorization" in vary


@pytest.mark.django_db
def test_authenticated_list_is_not_publicly_cached():
    _make_published_case(1)
    moderator = create_user_with_role("nisha_sharma", "nisha@test.com", "Moderator")
    client = APIClient()
    client.force_authenticate(user=moderator)
    resp = client.get("/api/cases/")
    assert resp.status_code == 200
    # Assert the safe value directly, not merely "!= the cacheable value":
    # ``resp.get("Cache-Control")`` is ``None`` when the header is absent, and
    # ``None != LIST_CACHE_CONTROL`` is True — so the weaker check would pass
    # even in the unsafe "no header at all" state this test guards against.
    assert "no-store" in resp["Cache-Control"]


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
