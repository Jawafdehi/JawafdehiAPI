"""API tests for the casework review submit endpoint.

Covers the three behaviors added on top of the original slug-only submit:
  1. A case may be submitted by its court case number, not just its slug.
  2. The case is verified up front: an unknown slug / court case number fails
     fast (404) and a found case's title is pulled onto the review.
  3. Only one review may be active per case (max one pending/running job).
"""

import pytest
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from review.models import CaseReview
from tests.conftest import create_user_with_role

SUBMIT_URL = "/api/casework/reviews/submit/"
REGRADE_URL = "/api/casework/reviews/regrade-all/"


@pytest.fixture
def contributor(db):
    return create_user_with_role(
        "rev_contrib", "rev_contrib@example.com", "Contributor"
    )


@pytest.fixture
def client(contributor):
    c = APIClient()
    c.force_authenticate(user=contributor)
    return c


def _make_case(slug, title, court_cases=None):
    return Case.objects.create(
        title=title,
        slug=slug,
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        court_cases=court_cases,
    )


def test_submit_by_slug_pulls_title(client):
    _make_case("alpha-case", "Alpha Corruption Case")
    resp = client.post(SUBMIT_URL, {"slug": "alpha-case"}, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.data["slug"] == "alpha-case"
    assert resp.data["case_title"] == "Alpha Corruption Case"
    assert resp.data["status"] == "pending"


def test_submit_by_court_case_number_resolves_case(client):
    _make_case("beta-case", "Beta Corruption Case", ["special:081-CR-0079"])
    resp = client.post(
        SUBMIT_URL, {"court_case_number": "special:081-CR-0079"}, format="json"
    )
    assert resp.status_code == 201, resp.content
    # Resolves to the owning case's slug + title for display.
    assert resp.data["slug"] == "beta-case"
    assert resp.data["case_title"] == "Beta Corruption Case"
    review = CaseReview.objects.get(id=resp.data["id"])
    assert review.slug == "beta-case"


@pytest.mark.parametrize(
    "value",
    [
        "https://jawafdehi.org/case/alpha-case",
        "https://jawafdehi.org/case/alpha-case/",
        "https://jawafdehi.org/case/alpha-case?ref=share",
        "jawafdehi.org/case/alpha-case",
    ],
)
def test_submit_by_full_case_url_extracts_slug(client, value):
    _make_case("alpha-case", "Alpha Corruption Case")
    resp = client.post(SUBMIT_URL, {"slug": value}, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.data["slug"] == "alpha-case"
    assert resp.data["case_title"] == "Alpha Corruption Case"


def test_submit_unknown_slug_is_404(client):
    resp = client.post(SUBMIT_URL, {"slug": "does-not-exist"}, format="json")
    assert resp.status_code == 404
    assert not CaseReview.objects.exists()


def test_submit_unknown_court_case_number_is_404(client):
    resp = client.post(
        SUBMIT_URL, {"court_case_number": "special:999-CR-9999"}, format="json"
    )
    assert resp.status_code == 404
    assert not CaseReview.objects.exists()


def test_submit_malformed_court_case_number_is_404(client):
    resp = client.post(SUBMIT_URL, {"court_case_number": "not-a-ref"}, format="json")
    assert resp.status_code == 404
    assert not CaseReview.objects.exists()


def test_submit_requires_exactly_one_identifier(client):
    _make_case("gamma-case", "Gamma Case", ["special:081-CR-0080"])
    # Neither provided.
    assert client.post(SUBMIT_URL, {}, format="json").status_code == 400
    # Both provided.
    both = client.post(
        SUBMIT_URL,
        {"slug": "gamma-case", "court_case_number": "special:081-CR-0080"},
        format="json",
    )
    assert both.status_code == 400


def test_only_one_active_review_per_case(client):
    _make_case("delta-case", "Delta Case", ["special:081-CR-0081"])

    first = client.post(SUBMIT_URL, {"slug": "delta-case"}, format="json")
    assert first.status_code == 201

    # Re-submitting by slug while the first is still pending is rejected.
    dup = client.post(SUBMIT_URL, {"slug": "delta-case"}, format="json")
    assert dup.status_code == 409
    assert dup.data["review_id"] == first.data["id"]

    # The same case reached by its court case number is also blocked.
    dup_cc = client.post(
        SUBMIT_URL, {"court_case_number": "special:081-CR-0081"}, format="json"
    )
    assert dup_cc.status_code == 409

    assert CaseReview.objects.filter(slug="delta-case").count() == 1


def test_resubmit_allowed_after_previous_review_finished(client):
    _make_case("epsilon-case", "Epsilon Case")

    first = client.post(SUBMIT_URL, {"slug": "epsilon-case"}, format="json")
    assert first.status_code == 201

    review = CaseReview.objects.get(id=first.data["id"])
    review.status = CaseReview.STATUS_DONE
    review.save(update_fields=["status"])

    again = client.post(SUBMIT_URL, {"slug": "epsilon-case"}, format="json")
    assert again.status_code == 201
    assert CaseReview.objects.filter(slug="epsilon-case").count() == 2


def test_ambiguous_court_case_number_is_404(client):
    # Two cases sharing a court ref must not silently resolve to one of them.
    _make_case("zeta-one", "Zeta One", ["special:081-CR-0090"])
    _make_case("zeta-two", "Zeta Two", ["special:081-CR-0090"])
    resp = client.post(
        SUBMIT_URL, {"court_case_number": "special:081-CR-0090"}, format="json"
    )
    assert resp.status_code == 404
    assert "ambiguous" in resp.data["detail"].lower()
    assert not CaseReview.objects.exists()


def test_db_constraint_blocks_second_active_review(db):
    # The partial unique constraint is the source of truth for the race-prone
    # check-then-create path: a second active row for the same case_id is rejected.
    CaseReview.objects.create(
        case_id="case-eta", slug="eta-case", status=CaseReview.STATUS_PENDING
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CaseReview.objects.create(
                case_id="case-eta", slug="eta-case", status=CaseReview.STATUS_RUNNING
            )


def test_regrade_all_requeues_latest_per_case_and_skips_active(client):
    # theta: two finished reviews (same case_id) -> only the latest should re-queue.
    CaseReview.objects.create(
        case_id="case-theta", slug="theta-case", status=CaseReview.STATUS_DONE
    )
    theta_latest = CaseReview.objects.create(
        case_id="case-theta", slug="theta-case", status=CaseReview.STATUS_FAILED
    )
    # iota: already has an active review -> must be left untouched.
    iota_active = CaseReview.objects.create(
        case_id="case-iota", slug="iota-case", status=CaseReview.STATUS_RUNNING
    )

    resp = client.post(REGRADE_URL, {}, format="json")
    assert resp.status_code == 200

    # Exactly one active review for the theta case (the latest), none for iota.
    theta_pending = CaseReview.objects.filter(
        case_id="case-theta", status=CaseReview.STATUS_PENDING
    )
    assert list(theta_pending.values_list("id", flat=True)) == [theta_latest.id]

    iota_active.refresh_from_db()
    assert iota_active.status == CaseReview.STATUS_RUNNING
    assert (
        CaseReview.objects.filter(
            case_id="case-iota", status=CaseReview.STATUS_PENDING
        ).count()
        == 0
    )
