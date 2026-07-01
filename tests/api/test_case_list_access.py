"""
Tests for authenticated user visibility in the case list endpoint.

GET /api/cases/ visibility rules:
  - Unauthenticated: PUBLISHED only (IN_REVIEW is NOT shown)
  - Admin / Moderator: all non-CLOSED cases
  - Contributor (or other authenticated):
      PUBLISHED + DRAFT/IN_REVIEW cases where they are in contributors

Feature: accountability-platform-core
Validates: Requirements 1.5, 3.1, 6.1
"""

import pytest
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities, create_user_with_role

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def contributor(db):
    return create_user_with_role("sita_rai", "sita@test.com", "Caseworker")


@pytest.fixture
def other_contributor(db):
    return create_user_with_role("ram_thapa", "ram@test.com", "Caseworker")


@pytest.fixture
def moderator(db):
    return create_user_with_role("nisha_sharma", "nisha@test.com", "Moderator")


@pytest.fixture
def published_case(db):
    return create_case_with_entities(
        title="Bhrastachar Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
    )


@pytest.fixture
def draft_assigned(db, contributor):
    case = create_case_with_entities(
        title="Mero Draft Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
    )
    case.contributors.add(contributor)
    return case


@pytest.fixture
def draft_unassigned(db):
    return create_case_with_entities(
        title="Arkako Draft Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
    )


@pytest.fixture
def in_review_assigned(db, contributor):
    case = create_case_with_entities(
        title="Samiksha Adheen Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.IN_REVIEW,
    )
    case.contributors.add(contributor)
    return case


@pytest.fixture
def closed_case(db):
    return create_case_with_entities(
        title="Banda Garieko Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.CLOSED,
    )


def case_ids_in(response):
    return {c["slug"] for c in response.data.get("results", [])}


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unauthenticated_sees_only_published(
    published_case, draft_unassigned, in_review_assigned, closed_case
):
    """Unauthenticated requests return only PUBLISHED cases."""
    client = APIClient()
    response = client.get("/api/cases/")
    assert response.status_code == 200
    ids = case_ids_in(response)
    assert published_case.slug in ids
    assert draft_unassigned.slug not in ids
    assert in_review_assigned.slug not in ids
    assert closed_case.slug not in ids


# ---------------------------------------------------------------------------
# Contributor
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_contributor_sees_published_and_assigned_draft(
    contributor, published_case, draft_assigned, draft_unassigned
):
    """Contributor sees all non-CLOSED cases (PUBLISHED + DRAFT + IN_REVIEW)."""
    client = APIClient()
    client.force_authenticate(user=contributor)
    response = client.get("/api/cases/")
    assert response.status_code == 200
    ids = case_ids_in(response)
    assert published_case.slug in ids
    assert draft_assigned.slug in ids
    assert draft_unassigned.slug in ids  # global read access


@pytest.mark.django_db
def test_contributor_sees_assigned_in_review(
    contributor, published_case, in_review_assigned
):
    """Contributor sees PUBLISHED + their assigned IN_REVIEW."""
    client = APIClient()
    client.force_authenticate(user=contributor)
    response = client.get("/api/cases/")
    assert response.status_code == 200
    ids = case_ids_in(response)
    assert published_case.slug in ids
    assert in_review_assigned.slug in ids


@pytest.mark.django_db
def test_contributor_does_see_unassigned_draft(
    contributor, draft_unassigned, draft_assigned
):
    """Contributor sees all drafts (global read access), including unassigned."""
    client = APIClient()
    client.force_authenticate(user=contributor)
    response = client.get("/api/cases/")
    assert response.status_code == 200
    ids = case_ids_in(response)
    assert draft_unassigned.slug in ids


@pytest.mark.django_db
def test_contributor_never_sees_closed(contributor, closed_case):
    """Contributor must never see CLOSED cases."""
    client = APIClient()
    client.force_authenticate(user=contributor)
    response = client.get("/api/cases/")
    assert response.status_code == 200
    assert closed_case.slug not in case_ids_in(response)


@pytest.mark.django_db
def test_other_contributor_can_see_another_users_draft(
    other_contributor, draft_assigned
):
    """A contributor unrelated to a draft can now see it (global read access)."""
    client = APIClient()
    client.force_authenticate(user=other_contributor)
    response = client.get("/api/cases/")
    assert response.status_code == 200
    assert draft_assigned.slug in case_ids_in(response)


# ---------------------------------------------------------------------------
# Admin / Moderator
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_moderator_sees_all_non_closed(
    moderator, published_case, draft_unassigned, in_review_assigned, closed_case
):
    """Moderator sees all non-CLOSED cases regardless of assignment."""
    client = APIClient()
    client.force_authenticate(user=moderator)
    response = client.get("/api/cases/")
    assert response.status_code == 200
    ids = case_ids_in(response)
    assert published_case.slug in ids
    assert draft_unassigned.slug in ids
    assert in_review_assigned.slug in ids
    assert closed_case.slug not in ids
