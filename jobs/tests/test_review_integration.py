"""The review app is now a queue producer + consumer-of-record via `jobs`.

These tests assert the seam works end to end WITHOUT the poller: submitting a
review enqueues a case_review job; claiming it resolves the case dict server-side
(build_payload); finalizing it updates the CaseReview row (on_result); a terminal
failure marks the CaseReview failed (on_failure).
"""

from unittest import mock

import pytest

from cases.models import Case, CaseType
from jobs import queue
from jobs.models import Job
from review.models import CaseReview


def _review(slug, **kwargs):
    """Create a CaseReview linked to the Case with ``slug`` (created on demand)."""
    case, _ = Case.objects.get_or_create(
        slug=slug, defaults=dict(title=slug, case_type=CaseType.CORRUPTION)
    )
    return CaseReview.objects.create(case=case, **kwargs)


@pytest.mark.django_db
def test_submit_review_enqueues_case_review_job(client_helper=None):
    review = _review("some-case")
    # Enqueue via the same helper the view uses.
    from review.views import _enqueue_review_job

    job = _enqueue_review_job(review)
    assert job.kind == "case_review"
    assert job.payload["case_id"] == review.case_id
    assert job.payload["review_id"] == review.id
    assert job.dedup_key == f"case_review:{review.id}"


@pytest.mark.django_db
def test_claim_resolves_case_dict_via_build_payload():
    review = _review("case-x")
    from review.views import _enqueue_review_job

    _enqueue_review_job(review)

    fake_case = {"title": "Case X", "state": "published", "entities": []}
    with mock.patch("review.case_provider.get_case_by_id", return_value=fake_case):
        job = queue.claim_next(["case_review"])

    assert job is not None
    # build_payload resolved the case dict + config server-side.
    assert job.payload["case"]["title"] == "Case X"
    assert "config" in job.payload
    assert set(job.payload["config"]) == {
        "pass_threshold",
        "revise_threshold",
        "llm_samples",
    }


@pytest.mark.django_db
def test_on_result_finalizes_the_review_row():
    review = _review("case-y")
    from review.views import _enqueue_review_job

    _enqueue_review_job(review)
    with mock.patch("review.case_provider.get_case_by_id", return_value={"title": "Y"}):
        job = queue.claim_next(["case_review"])

    result = {
        "case_title": "Y",
        "case_state": "published",
        "case_type": "CORRUPTION",
        "source_count": 3,
        "sources_converted": 2,
        "result": {"overall_score": 88, "disposition": "pass"},
        "duration_seconds": 12.5,
    }
    queue.finalize(job, status=Job.DONE, result=result, duration_seconds=12.5)

    review.refresh_from_db()
    assert review.status == CaseReview.STATUS_DONE
    assert review.case_type == "CORRUPTION"
    assert review.source_count == 3
    assert review.result["overall_score"] == 88
    assert review.duration_seconds == 12.5


@pytest.mark.django_db
def test_on_failure_marks_the_review_failed():
    review = _review("case-z")
    from review.views import _enqueue_review_job

    _enqueue_review_job(review, submitted_by=None)
    with mock.patch("review.case_provider.get_case_by_id", return_value={"title": "Z"}):
        job = queue.claim_next(["case_review"])

    queue.finalize(job, status=Job.FAILED, error="scorer exploded", retryable=False)

    review.refresh_from_db()
    assert review.status == CaseReview.STATUS_FAILED
    assert "scorer exploded" in review.error


@pytest.mark.django_db
def test_regrade_reenqueues_after_prior_job_terminal():
    review = _review("case-r")
    from review.views import _enqueue_review_job

    j1 = _enqueue_review_job(review)
    with mock.patch("review.case_provider.get_case_by_id", return_value={"title": "R"}):
        claimed = queue.claim_next(["case_review"])
    queue.finalize(claimed, status=Job.DONE, result={"case_title": "R", "result": {}})

    # A regrade enqueues a fresh job now that the prior one is terminal.
    j2 = _enqueue_review_job(review)
    assert j2.id != j1.id
    assert j2.status == Job.QUEUED


@pytest.mark.django_db
def test_review_survives_a_case_reslug():
    """The whole point of keying on the case FK: a re-slug must not orphan the
    review. The enqueued payload carries the stable ``case_id`` (never ``slug``),
    the derived ``slug`` follows the case, and build_payload still resolves the
    case by id after the slug changes.
    """
    from jobs.consumers import _case_review_build_payload
    from review.views import _enqueue_review_job

    # DRAFT so the slug is mutable via save() (immutable outside DRAFT).
    review = _review("orig-slug")
    case = review.case

    job = _enqueue_review_job(review)
    assert job.payload["case_id"] == case.id
    assert "slug" not in job.payload

    # Re-slug the case; the review's FK is untouched.
    case.slug = "new-slug"
    case.save()

    # Fetch the review fresh so the derived slug is read off the persisted case.
    fresh = CaseReview.objects.select_related("case").get(pk=review.pk)
    assert fresh.slug == "new-slug"

    # build_payload resolves by case_id — no raise — and the serialized case dict
    # carries the CURRENT slug.
    payload = _case_review_build_payload(job)
    assert payload["case"]["slug"] == "new-slug"


@pytest.mark.django_db
def test_consumer_result_body_through_the_view_finalizes_the_review():
    """The seam that broke in prod: the consumer's HTTP body -> view ->
    JobResultSerializer -> finalize -> on_result. The handler's whole return
    value must arrive nested under "result" (the serializer stores that field
    verbatim as job.result); submitted flat, the hook read wrapper fields off
    the inner scored dict and died on case_type (a dict) overflowing varchar.
    """
    from django.contrib.auth.models import Group, User
    from rest_framework.test import APIClient

    from review.views import _enqueue_review_job

    Group.objects.get_or_create(name="Caseworker")
    user = User.objects.create_user("seamworker", password="x")
    user.groups.add(Group.objects.get(name="Caseworker"))
    api = APIClient()
    api.force_authenticate(user=user)

    review = _review("case-seam")
    _enqueue_review_job(review)
    with mock.patch(
        "review.case_provider.get_case_by_id", return_value={"title": "Seam"}
    ):
        job = queue.claim_next(["case_review"])

    # Exactly what review_poller._process_job submits: the handler wrapper
    # nested under "result".
    wrapper = {
        "case_title": "Seam",
        "case_state": "IN_REVIEW",
        "case_type": "CIAA_BASIC",
        "source_count": 3,
        "sources_converted": 2,
        "result": {"disposition": "PASS", "case_type": {"type": "CIAA_BASIC"}},
        "duration_seconds": 12.5,
    }
    r = api.post(
        f"/api/jobs/{job.pk}/result/",
        {"status": "done", "result": wrapper, "duration_seconds": 12.5},
        format="json",
    )
    assert r.status_code == 200

    review.refresh_from_db()
    assert review.status == CaseReview.STATUS_DONE
    assert review.stage == "complete"
    assert review.case_type == "CIAA_BASIC"  # the STRING, not the scored dict
    assert review.case_title == "Seam"
    assert review.source_count == 3
    assert review.result == {"disposition": "PASS", "case_type": {"type": "CIAA_BASIC"}}
    assert review.duration_seconds == 12.5
