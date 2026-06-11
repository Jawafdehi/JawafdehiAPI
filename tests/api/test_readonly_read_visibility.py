"""Integration tests: the org-wide ReadOnly role gets a true system-wide READ.

These exercise the actual API list endpoints (not bare ORM filters), proving a
ReadOnly user sees materials that the public contract hides:
  - cases: all non-CLOSED cases, including DRAFT (CaseViewSet.get_queryset)
  - sources: every non-deleted source, including draft-only / unreferenced ones
  - entities: every entity, not just those in published cases

The public/anonymous baseline is asserted alongside each, so the tests fail if
ReadOnly visibility regresses to the public surface.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType, DocumentSource, JawafEntity
from tests.conftest import create_case_with_entities, create_user_with_role


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the process-global cache (e.g. public_entities_list) around each
    test so entity-list assertions never read a stale set from another test."""
    cache.clear()
    yield
    cache.clear()


def _ids(response):
    """IDs from a (possibly paginated) DRF list response."""
    data = response.json()
    results = data["results"] if isinstance(data, dict) and "results" in data else data
    return {row["id"] for row in results}


def _ro_client():
    readonly = create_user_with_role("ro_read", "ro_read@example.com", "ReadOnly")
    client = APIClient()
    client.force_authenticate(user=readonly)
    return client


# ---------------------------------------------------------------------------
# Cases — DRAFT visibility through the real list endpoint (CodeRabbit #3)
# ---------------------------------------------------------------------------


def _make_case(title, state):
    case = create_case_with_entities(
        title=title,
        alleged_entities=["entity:person/test-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description=title,
    )
    case.state = state
    case.save()
    return case


@pytest.mark.django_db
def test_readonly_lists_draft_cases_via_endpoint():
    draft = _make_case("RO Draft Case", CaseState.DRAFT)
    published = _make_case("RO Published Case", CaseState.PUBLISHED)
    closed = _make_case("RO Closed Case", CaseState.CLOSED)

    response = _ro_client().get("/api/cases/")
    assert response.status_code == 200
    ids = _ids(response)
    assert draft.id in ids  # the systemwide-read promise: DRAFT is visible
    assert published.id in ids
    assert closed.id not in ids  # CLOSED is never exposed via the public API


@pytest.mark.django_db
def test_anonymous_does_not_list_draft_cases():
    draft = _make_case("Anon Draft", CaseState.DRAFT)
    published = _make_case("Anon Published", CaseState.PUBLISHED)

    response = APIClient().get("/api/cases/")
    assert response.status_code == 200
    ids = _ids(response)
    assert draft.id not in ids
    assert published.id in ids


# ---------------------------------------------------------------------------
# Sources — ReadOnly sees draft-only / unreferenced sources
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_readonly_lists_unreferenced_source():
    """A source not referenced by any published/in-review case is hidden from the
    public list but visible to ReadOnly."""
    orphan = DocumentSource.objects.create(
        title="Draft-only Source", source_type="MISC"
    )

    # Public/anonymous: not visible.
    assert orphan.id not in _ids(APIClient().get("/api/sources/"))

    # ReadOnly: visible (systemwide read).
    response = _ro_client().get("/api/sources/")
    assert response.status_code == 200
    assert orphan.id in _ids(response)


# ---------------------------------------------------------------------------
# Entities — ReadOnly sees entities not attached to any published case
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_readonly_lists_unpublished_entity():
    """An entity not appearing in any published case is hidden from the public
    list but visible to ReadOnly."""
    orphan = JawafEntity.objects.create(display_name="Draft-only Entity")

    # Public/anonymous: not visible.
    assert orphan.id not in _ids(APIClient().get("/api/entities/"))

    # ReadOnly: visible (systemwide read).
    response = _ro_client().get("/api/entities/")
    assert response.status_code == 200
    assert orphan.id in _ids(response)
