"""Tests for the ``?slug=`` filter on GET /api/casework/reviews/.

The per-case review page fetches one case's whole run history via the flat list
scoped to a slug. Without the filter it would have to page the entire table.
"""

import pytest
from rest_framework.test import APIClient

from review.models import CaseReview
from tests.conftest import create_user_with_role

URL = "/api/casework/reviews/"


def _reader_client():
    # Caseworker has CanReadReview.
    user = create_user_with_role("rev-reader", "rev-reader@example.com", "Caseworker")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_slug_filter_returns_only_that_cases_runs():
    CaseReview.objects.create(slug="case-a", case_title="Case A", status="done")
    CaseReview.objects.create(slug="case-a", case_title="Case A", status="done")
    CaseReview.objects.create(slug="case-b", case_title="Case B", status="done")

    response = _reader_client().get(URL, {"slug": "case-a"})

    assert response.status_code == 200
    slugs = {r["slug"] for r in response.data["results"]}
    assert slugs == {"case-a"}
    assert response.data["count"] == 2


@pytest.mark.django_db
def test_slug_filter_orders_newest_first():
    older = CaseReview.objects.create(slug="case-c", case_title="Case C")
    newer = CaseReview.objects.create(slug="case-c", case_title="Case C")

    response = _reader_client().get(URL, {"slug": "case-c"})

    ids = [r["id"] for r in response.data["results"]]
    assert ids == [newer.id, older.id]


@pytest.mark.django_db
def test_unfiltered_list_returns_all_runs():
    CaseReview.objects.create(slug="case-a", case_title="Case A")
    CaseReview.objects.create(slug="case-b", case_title="Case B")

    response = _reader_client().get(URL)

    assert response.data["count"] == 2


@pytest.mark.django_db
def test_unknown_slug_returns_empty():
    CaseReview.objects.create(slug="case-a", case_title="Case A")

    response = _reader_client().get(URL, {"slug": "no-such-case"})

    assert response.status_code == 200
    assert response.data["count"] == 0
