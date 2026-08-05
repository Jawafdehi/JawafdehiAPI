"""Tests for E1 — GET /api/casework/reviews/grouped/.

The grouped endpoint groups CaseReview rows by case, paginating BY CASE: each
entry carries {slug, case_title, latest, executions[]} with executions
newest-first and cases ordered by their most-recent execution. The group ``slug``
is derived from the (shared) linked case, not stored on the review.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import Case, CaseType
from review.models import CaseReview
from tests.conftest import create_user_with_role

URL = "/api/casework/reviews/grouped/"


def _review(slug, **kwargs):
    """Create a CaseReview linked to the Case with ``slug`` (created on demand).

    Reviews key on the case FK now; the ``slug`` they expose is derived from it.
    """
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
def test_grouped_requires_read_role():
    """An authenticated user with no role (no casework read access) is denied."""
    user = create_user_with_role("rev-norole", "rev-norole@example.com", "Public")
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
    _review("case-a", case_title="Case A", status="done")
    _review("case-a", case_title="Case A", status="done")
    _review("case-b", case_title="Case B", status="pending")

    response = _reader_client().get(URL)

    assert response.status_code == 200
    assert response.data["count"] == 2  # two CASES, not three executions

    by_slug = {g["slug"]: g for g in response.data["results"]}
    assert set(by_slug) == {"case-a", "case-b"}
    assert len(by_slug["case-a"]["executions"]) == 2
    assert len(by_slug["case-b"]["executions"]) == 1
    assert by_slug["case-a"]["case_title"] == "Case A"


@pytest.mark.django_db
def test_grouped_slug_filter_returns_only_that_case():
    _review("case-a", case_title="Case A", status="done")
    _review("case-a", case_title="Case A", status="done")
    _review("case-b", case_title="Case B", status="done")

    response = _reader_client().get(URL, {"slug": " /case-a/ "})

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert [group["slug"] for group in response.data["results"]] == ["case-a"]
    assert len(response.data["results"][0]["executions"]) == 2


@pytest.mark.django_db
def test_grouped_latest_is_newest_execution():
    older = _review("case-c", case_title="Old title")
    newer = _review("case-c", case_title="New title")

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
    _review("case-x", case_title="X")
    _review("case-y", case_title="Y")

    response = _reader_client().get(URL)

    slugs = [g["slug"] for g in response.data["results"]]
    assert slugs == ["case-y", "case-x"]


@pytest.mark.django_db
def test_grouped_paginates_by_case():
    # 25 distinct cases > default PAGE_SIZE (20) -> paginated by case.
    for i in range(25):
        _review(f"case-{i:02d}", case_title=f"Case {i}")

    response = _reader_client().get(URL)

    assert response.data["count"] == 25
    assert len(response.data["results"]) == 20
    assert response.data["next"] is not None


@pytest.mark.django_db
def test_grouped_pagination_ranks_cases_across_pages():
    """DB-level slug pagination keeps the newest-case-first ranking across pages.

    Cases are created oldest-first (case-00 … case-24), so the newest execution
    belongs to case-24. Page 1 must start at case-24 and page 2 must continue the
    descending ranking without gaps or repeats.
    """
    for i in range(25):
        _review(f"case-{i:02d}", case_title=f"Case {i}")

    client = _reader_client()
    page1 = client.get(URL)
    page2 = client.get(URL, {"page": 2})

    slugs1 = [g["slug"] for g in page1.data["results"]]
    slugs2 = [g["slug"] for g in page2.data["results"]]

    assert slugs1[0] == "case-24"  # newest execution first
    assert len(slugs1) == 20 and len(slugs2) == 5
    # Full descending sequence, no overlap between pages.
    assert slugs1 + slugs2 == [f"case-{i:02d}" for i in range(24, -1, -1)]
    assert set(slugs1).isdisjoint(slugs2)


@pytest.mark.django_db
def test_grouped_item_shape_matches_flat_list():
    """Each execution item carries the same fields the flat /reviews/ list emits."""
    _review("case-shape", case_title="Shape")

    response = _reader_client().get(URL)

    item = response.data["results"][0]["latest"]
    for field in ("id", "slug", "status", "case_title", "overall_score", "disposition"):
        assert field in item


@pytest.mark.django_db
def test_grouped_excludes_unlinked_reviews():
    """A review with no linked case (case_id NULL) must not form a bogus group.

    The FK is nullable only as a backfill safety valve; an unlinked row would
    otherwise surface as a None-keyed group with an empty slug in the UI.
    """
    _review("case-linked", case_title="Linked")
    CaseReview.objects.create(case=None, case_title="Orphan")

    response = _reader_client().get(URL)

    assert response.data["count"] == 1
    assert [g["slug"] for g in response.data["results"]] == ["case-linked"]
