"""Regression tests for the regrade-all role gate + "latest per slug" targeting.

``POST /api/casework/reviews/regrade-all/`` (view ``regrade_all`` in
review/views.py) is gated by ``HasContributorRole`` — the *write* floor of the
review system. It must:

  * reject anonymous callers (401),
  * reject read-only roles that can *observe* but not *drive* (Public / ReadOnly
    -> 403),
  * admit the contributor roles (Caseworker / Moderator / Admin -> 2xx).

And it must re-queue only ONE review per case: the LATEST CaseReview row of each
slug is reset to pending, while older rows of the same slug stay untouched
history (see the view docstring — regrading every historical row would grade the
same live case N times at full LLM cost).
"""

import pytest
from rest_framework.test import APIClient

from review.models import CaseReview
from tests.conftest import create_user_with_role

URL = "/api/casework/reviews/regrade-all/"


def _client_for_role(role):
    user = create_user_with_role(
        f"regrade-{role.lower()}", f"regrade-{role.lower()}@example.com", role
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_regrade_all_requires_authentication():
    """Anonymous callers get 401 (no credentials)."""
    assert APIClient().post(URL).status_code == 401


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Public", "ReadOnly"])
def test_regrade_all_denies_read_only_roles(role):
    """Public and the org-wide ReadOnly role may observe but not drive: 403.

    HasContributorRole deliberately excludes both — they lack any content role
    (has_role) and are not ReviewAssistant service accounts.
    """
    assert _client_for_role(role).post(URL).status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Caseworker", "Moderator", "Admin"])
def test_regrade_all_allows_contributor_roles(role):
    """Caseworker / Moderator / Admin all clear the write floor (2xx)."""
    resp = _client_for_role(role).post(URL)
    assert 200 <= resp.status_code < 300


@pytest.mark.django_db
def test_regrade_all_targets_only_the_latest_review_per_slug():
    """Only the highest-id CaseReview per slug is reset; older rows are history.

    Setup: two executions for ``case-a`` (older + newer) and one for ``case-b``.
    After regrade-all: the newer ``case-a`` row and the sole ``case-b`` row are
    reset to pending/``queued_for_regrade``; the older ``case-a`` row is left
    exactly as it was (still ``done`` / its old stage / its old error).
    """
    older_a = CaseReview.objects.create(
        slug="case-a",
        case_title="Case A",
        status=CaseReview.STATUS_DONE,
        stage="complete",
        error="prior-error-should-survive",
    )
    newer_a = CaseReview.objects.create(
        slug="case-a",
        case_title="Case A",
        status=CaseReview.STATUS_DONE,
        stage="complete",
    )
    only_b = CaseReview.objects.create(
        slug="case-b",
        case_title="Case B",
        status=CaseReview.STATUS_DONE,
        stage="complete",
    )

    resp = _client_for_role("Caseworker").post(URL)

    assert resp.status_code == 200
    # One regrade per CASE, not per execution.
    assert resp.data["regrading"] == 2
    assert set(resp.data["review_ids"]) == {newer_a.id, only_b.id}

    # The latest row of each slug is reset to pending / queued_for_regrade.
    newer_a.refresh_from_db()
    only_b.refresh_from_db()
    for reset in (newer_a, only_b):
        assert reset.status == CaseReview.STATUS_PENDING
        assert reset.stage == "queued_for_regrade"
        assert reset.error == ""

    # The older execution of case-a is untouched history.
    older_a.refresh_from_db()
    assert older_a.status == CaseReview.STATUS_DONE
    assert older_a.stage == "complete"
    assert older_a.error == "prior-error-should-survive"


@pytest.mark.django_db
def test_regrade_all_with_no_reviews_is_a_noop():
    """No reviews -> nothing to regrade, still a clean 2xx."""
    resp = _client_for_role("Admin").post(URL)
    assert resp.status_code == 200
    assert resp.data["regrading"] == 0
    assert resp.data["review_ids"] == []
