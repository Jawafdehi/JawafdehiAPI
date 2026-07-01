"""The review app is now a queue producer + consumer-of-record via `jobs`.

These tests assert the seam works end to end WITHOUT the poller: submitting a
review enqueues a case_review job; claiming it resolves the case dict server-side
(build_payload); finalizing it updates the CaseReview row (on_result); a terminal
failure marks the CaseReview failed (on_failure).
"""

from unittest import mock

import pytest

from jobs import queue
from jobs.models import Job
from review.models import CaseReview


@pytest.mark.django_db
def test_submit_review_enqueues_case_review_job(client_helper=None):
    review = CaseReview.objects.create(slug="some-case")
    # Enqueue via the same helper the view uses.
    from review.views import _enqueue_review_job

    job = _enqueue_review_job(review)
    assert job.kind == "case_review"
    assert job.payload["slug"] == "some-case"
    assert job.payload["review_id"] == review.id
    assert job.dedup_key == f"case_review:{review.id}"


@pytest.mark.django_db
def test_claim_resolves_case_dict_via_build_payload():
    review = CaseReview.objects.create(slug="case-x")
    from review.views import _enqueue_review_job

    _enqueue_review_job(review)

    fake_case = {"title": "Case X", "state": "published", "entities": []}
    with mock.patch("review.case_provider.get_case", return_value=fake_case):
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
    review = CaseReview.objects.create(slug="case-y")
    from review.views import _enqueue_review_job

    _enqueue_review_job(review)
    with mock.patch("review.case_provider.get_case", return_value={"title": "Y"}):
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
    review = CaseReview.objects.create(slug="case-z")
    from review.views import _enqueue_review_job

    _enqueue_review_job(review, submitted_by=None)
    with mock.patch("review.case_provider.get_case", return_value={"title": "Z"}):
        job = queue.claim_next(["case_review"])

    queue.finalize(job, status=Job.FAILED, error="scorer exploded", retryable=False)

    review.refresh_from_db()
    assert review.status == CaseReview.STATUS_FAILED
    assert "scorer exploded" in review.error


@pytest.mark.django_db
def test_regrade_reenqueues_after_prior_job_terminal():
    review = CaseReview.objects.create(slug="case-r")
    from review.views import _enqueue_review_job

    j1 = _enqueue_review_job(review)
    with mock.patch("review.case_provider.get_case", return_value={"title": "R"}):
        claimed = queue.claim_next(["case_review"])
    queue.finalize(claimed, status=Job.DONE, result={"case_title": "R", "result": {}})

    # A regrade enqueues a fresh job now that the prior one is terminal.
    j2 = _enqueue_review_job(review)
    assert j2.id != j1.id
    assert j2.status == Job.QUEUED
