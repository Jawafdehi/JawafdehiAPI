"""Tests for the ``?slug=`` filter on GET /api/casework/reviews/.

The per-case review page fetches one case's whole run history via the flat list
scoped to a slug. Without the filter it would have to page the entire table.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import Case, CaseType
from review.models import CaseReview
from tests.conftest import create_user_with_role

URL = "/api/casework/reviews/"


def _review(slug, **kwargs):
    """Create a CaseReview linked to the Case with ``slug`` (created on demand)."""
    case, _ = Case.objects.get_or_create(
        slug=slug, defaults=dict(title=slug, case_type=CaseType.CORRUPTION)
    )
    return CaseReview.objects.create(case=case, **kwargs)


def _reader_client():
    # Caseworker has CanReadReview.
    user = create_user_with_role("rev-reader", "rev-reader@example.com", "Caseworker")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_slug_filter_returns_only_that_cases_runs():
    _review("case-a", case_title="Case A", status="done")
    _review("case-a", case_title="Case A", status="done")
    _review("case-b", case_title="Case B", status="done")

    response = _reader_client().get(URL, {"slug": "case-a"})

    assert response.status_code == 200
    slugs = {r["slug"] for r in response.data["results"]}
    assert slugs == {"case-a"}
    assert response.data["count"] == 2


@pytest.mark.django_db
def test_slug_filter_orders_newest_first():
    older = _review("case-c", case_title="Case C")
    # Back-date the older row: two back-to-back creates can share a created_at
    # on fast DBs, which would make the -created_at ordering non-deterministic.
    CaseReview.objects.filter(id=older.id).update(
        created_at=timezone.now() - timedelta(seconds=1)
    )
    newer = _review("case-c", case_title="Case C")

    response = _reader_client().get(URL, {"slug": "case-c"})

    ids = [r["id"] for r in response.data["results"]]
    assert ids == [newer.id, older.id]


@pytest.mark.django_db
def test_unfiltered_list_returns_all_runs():
    _review("case-a", case_title="Case A")
    _review("case-b", case_title="Case B")

    response = _reader_client().get(URL)

    assert response.data["count"] == 2


@pytest.mark.django_db
def test_unknown_slug_returns_empty():
    _review("case-a", case_title="Case A")

    response = _reader_client().get(URL, {"slug": "no-such-case"})

    assert response.status_code == 200
    assert response.data["count"] == 0
