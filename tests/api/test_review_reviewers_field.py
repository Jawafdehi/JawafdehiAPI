"""Tests for the ``reviewers`` field on the casework review serializers (ADMIN-7).

The frontend renders reviewer attribution ("Graded by …") from a ``reviewers``
list of ``{tier, provider, model, calls}`` on each review, but the serializers
never emitted the field, so the chips silently never appeared. The runner
already records this per-tier LLM usage in
``result["token_usage"]["by_provider"]``; the serializers now project it.
"""

import pytest
from rest_framework.test import APIClient

from review.models import CaseReview
from tests.conftest import create_user_with_role

FLAT_URL = "/api/casework/reviews/"
GROUPED_URL = "/api/casework/reviews/grouped/"

# A result payload shaped like the runner's output: two tiers (premium gate rules
# + cheap routine rules), each a distinct (provider, tier, model) bucket.
RESULT_WITH_USAGE = {
    "overall_score": 82,
    "disposition": "PASS",
    "token_usage": {
        "model_id": "anthropic.claude-opus",
        "calls": 7,
        "by_provider": [
            {"provider": "bedrock", "tier": "premium", "model": "opus", "calls": 3},
            {"provider": "bedrock", "tier": "cheap", "model": "haiku", "calls": 4},
        ],
    },
}


def _reader_client():
    user = create_user_with_role("rev-r", "rev-r@example.com", "Caseworker")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _detail_url(pk):
    return f"/api/casework/reviews/{pk}/"


@pytest.mark.django_db
def test_flat_list_projects_reviewers_from_usage():
    CaseReview.objects.create(
        slug="case-a", case_title="Case A", status="done", result=RESULT_WITH_USAGE
    )

    row = _reader_client().get(FLAT_URL).data["results"][0]

    assert "reviewers" in row
    reviewers = row["reviewers"]
    assert reviewers is not None
    assert {r["tier"] for r in reviewers} == {"premium", "cheap"}
    premium = next(r for r in reviewers if r["tier"] == "premium")
    assert premium == {"tier": "premium", "provider": "bedrock", "model": "opus", "calls": 3}


@pytest.mark.django_db
def test_detail_projects_reviewers_from_usage():
    review = CaseReview.objects.create(
        slug="case-d", case_title="Case D", status="done", result=RESULT_WITH_USAGE
    )

    data = _reader_client().get(_detail_url(review.pk)).data

    assert data["reviewers"] is not None
    assert len(data["reviewers"]) == 2


@pytest.mark.django_db
def test_grouped_latest_carries_reviewers():
    CaseReview.objects.create(
        slug="case-g", case_title="Case G", status="done", result=RESULT_WITH_USAGE
    )

    group = _reader_client().get(GROUPED_URL).data["results"][0]

    assert group["latest"]["reviewers"] is not None
    assert len(group["latest"]["reviewers"]) == 2


@pytest.mark.django_db
def test_reviewers_null_when_no_usage_yet():
    """Pending/failed runs (no result, or a result without token_usage) → null,
    not a crash and not an empty-list masquerading as 'graded'."""
    CaseReview.objects.create(slug="pending-1", case_title="P1", status="pending")
    CaseReview.objects.create(
        slug="no-usage", case_title="NU", status="done", result={"overall_score": 10}
    )

    rows = {r["slug"]: r for r in _reader_client().get(FLAT_URL).data["results"]}

    assert rows["pending-1"]["reviewers"] is None
    assert rows["no-usage"]["reviewers"] is None


@pytest.mark.django_db
def test_reviewers_null_on_malformed_result_shapes():
    """`result` is a JSONField — malformed/legacy stored shapes must yield null,
    never crash the serializer (non-dict result, scalar token_usage, non-list
    by_provider, empty buckets)."""
    CaseReview.objects.create(slug="m-list", case_title="M", status="done", result=[1, 2])
    CaseReview.objects.create(
        slug="m-scalar-usage", case_title="M", status="done", result={"token_usage": 7}
    )
    CaseReview.objects.create(
        slug="m-scalar-bp",
        case_title="M",
        status="done",
        result={"token_usage": {"by_provider": "nope"}},
    )
    CaseReview.objects.create(
        slug="m-empty-bp",
        case_title="M",
        status="done",
        result={"token_usage": {"by_provider": []}},
    )

    rows = {r["slug"]: r for r in _reader_client().get(FLAT_URL).data["results"]}

    assert rows["m-list"]["reviewers"] is None
    assert rows["m-scalar-usage"]["reviewers"] is None
    assert rows["m-scalar-bp"]["reviewers"] is None
    assert rows["m-empty-bp"]["reviewers"] is None
