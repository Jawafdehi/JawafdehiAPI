"""Tests for the ?state= filter on GET /api/cases/ (plan §G1 — moderation queue).

The filter runs AFTER get_queryset()'s visibility scoping, so a caller can only
ever see states they are permitted to view.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities, create_user_with_role

URL = "/api/cases/"


def _slugs(response):
    return {c["slug"] for c in response.data.get("results", [])}


@pytest.fixture
def moderator(db):
    return create_user_with_role("mod-filter", "mod-filter@example.com", "Moderator")


@pytest.fixture
def cases(db):
    published = create_case_with_entities(
        title="Published one", case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED
    )
    in_review = create_case_with_entities(
        title="In review one", case_type=CaseType.CORRUPTION, state=CaseState.IN_REVIEW
    )
    draft = create_case_with_entities(
        title="Draft one", case_type=CaseType.CORRUPTION, state=CaseState.DRAFT
    )
    return {"published": published, "in_review": in_review, "draft": draft}


@pytest.mark.django_db
def test_moderator_filters_in_review_queue(moderator, cases):
    """?state=IN_REVIEW returns exactly the moderation queue for a casework role."""
    client = APIClient()
    client.force_authenticate(user=moderator)

    response = client.get(URL, {"state": CaseState.IN_REVIEW})

    assert response.status_code == 200
    assert _slugs(response) == {cases["in_review"].slug}


@pytest.mark.django_db
def test_moderator_filters_published(moderator, cases):
    client = APIClient()
    client.force_authenticate(user=moderator)

    response = client.get(URL, {"state": CaseState.PUBLISHED})

    assert response.status_code == 200
    assert _slugs(response) == {cases["published"].slug}


@pytest.mark.django_db
def test_unauthenticated_state_filter_respects_visibility(cases):
    """A public caller filtering ?state=IN_REVIEW gets nothing — visibility is
    scoped before the filter applies (no casework leak)."""
    response = APIClient().get(URL, {"state": CaseState.IN_REVIEW})

    assert response.status_code == 200
    assert _slugs(response) == set()


@pytest.mark.django_db
def test_unauthenticated_state_filter_published_still_works(cases):
    response = APIClient().get(URL, {"state": CaseState.PUBLISHED})

    assert response.status_code == 200
    assert _slugs(response) == {cases["published"].slug}
