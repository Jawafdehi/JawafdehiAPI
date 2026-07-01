"""Tests for E1 — GET /api/casework/reviews/grouped/.

The grouped endpoint groups CaseReview rows by case slug, paginating BY CASE:
each entry carries {slug, case_title, latest, executions[]} with executions
newest-first and cases ordered by their most-recent execution.
"""

import pytest
from rest_framework.test import APIClient

from review.models import CaseReview
from tests.conftest import create_user_with_role

URL = "/api/casework/reviews/grouped/"


def _reader_client():
    # Caseworker has CanReadReview.
    user = create_user_with_role("rev-reader", "rev-reader@example.com", "Caseworker")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_grouped_requires_read_role():
    """A Public-role user (no casework read access) is denied."""
    user = create_user_with_role("rev-public", "rev-public@example.com", "Public")
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(URL)

    assert response.status_code == 403


@pytest.mark.django_db
def test_grouped_requires_authentication():
    assert APIClient().get(URL).status_code == 401


@pytest.mark.django_db
def test_grouped_groups_executions_by_slug():
    # Two executions for case-a, one for case-b.
    CaseReview.objects.create(slug="case-a", case_title="Case A", status="done")
    CaseReview.objects.create(slug="case-a", case_title="Case A", status="done")
    CaseReview.objects.create(slug="case-b", case_title="Case B", status="pending")

    response = _reader_client().get(URL)

    assert response.status_code == 200
    assert response.data["count"] == 2  # two CASES, not three executions

    by_slug = {g["slug"]: g for g in response.data["results"]}
    assert set(by_slug) == {"case-a", "case-b"}
    assert len(by_slug["case-a"]["executions"]) == 2
    assert len(by_slug["case-b"]["executions"]) == 1
    assert by_slug["case-a"]["case_title"] == "Case A"


@pytest.mark.django_db
def test_grouped_latest_is_newest_execution():
    older = CaseReview.objects.create(slug="case-c", case_title="Old title")
    newer = CaseReview.objects.create(slug="case-c", case_title="New title")

    response = _reader_client().get(URL)

    group = response.data["results"][0]
    # latest == executions[0] == the newest execution.
    assert group["latest"]["id"] == newer.id
    assert group["executions"][0]["id"] == newer.id
    assert group["executions"][1]["id"] == older.id
    # case_title on the group reflects the freshest snapshot.
    assert group["case_title"] == "New title"


@pytest.mark.django_db
def test_grouped_cases_ordered_by_most_recent_execution():
    # case-x reviewed first, then case-y — case-y (newer) should come first.
    CaseReview.objects.create(slug="case-x", case_title="X")
    CaseReview.objects.create(slug="case-y", case_title="Y")

    response = _reader_client().get(URL)

    slugs = [g["slug"] for g in response.data["results"]]
    assert slugs == ["case-y", "case-x"]


@pytest.mark.django_db
def test_grouped_paginates_by_case():
    # 25 distinct cases > default PAGE_SIZE (20) -> paginated by case.
    for i in range(25):
        CaseReview.objects.create(slug=f"case-{i:02d}", case_title=f"Case {i}")

    response = _reader_client().get(URL)

    assert response.data["count"] == 25
    assert len(response.data["results"]) == 20
    assert response.data["next"] is not None


@pytest.mark.django_db
def test_grouped_item_shape_matches_flat_list():
    """Each execution item carries the same fields the flat /reviews/ list emits."""
    CaseReview.objects.create(slug="case-shape", case_title="Shape")

    response = _reader_client().get(URL)

    item = response.data["results"][0]["latest"]
    for field in ("id", "slug", "status", "case_title", "overall_score", "disposition"):
        assert field in item
