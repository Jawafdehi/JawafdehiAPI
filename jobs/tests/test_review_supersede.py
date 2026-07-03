"""Superseding duplicate queued case_review jobs.

A case_review job grades the LIVE case (resolved at claim time), so two QUEUED
jobs for the same slug are pure duplicate LLM spend. Enqueuing a newer review of
a slug dead-letters the older queued one; regrade_all regrades one review per
slug (the latest); the supersede_queued_reviews command collapses an existing
backlog.
"""

from io import StringIO

from django.core.management import call_command

import pytest

from jobs.models import Job
from review.models import CaseReview
from review.views import _enqueue_review_job


def _enqueue_pair(slug):
    """Two reviews of the same slug, enqueued oldest-first. Returns (old, new) jobs."""
    r1 = CaseReview.objects.create(slug=slug)
    j1 = _enqueue_review_job(r1)
    r2 = CaseReview.objects.create(slug=slug)
    j2 = _enqueue_review_job(r2)
    j1.refresh_from_db()
    return j1, j2


@pytest.mark.django_db
def test_enqueue_supersedes_older_queued_job_for_same_slug():
    j1, j2 = _enqueue_pair("case-dup")

    assert j2.status == Job.QUEUED
    assert j1.status == Job.DEAD
    assert "Superseded" in j1.error
    # The stale job's review is finalized, not left dangling as pending.
    stale_review = CaseReview.objects.get(pk=j1.payload["review_id"])
    assert stale_review.status == CaseReview.STATUS_FAILED
    assert stale_review.stage == "superseded"


@pytest.mark.django_db
def test_enqueue_does_not_touch_other_slugs_or_running_jobs():
    other = CaseReview.objects.create(slug="case-other")
    other_job = _enqueue_review_job(other)

    running = CaseReview.objects.create(slug="case-dup")
    running_job = _enqueue_review_job(running)
    Job.objects.filter(pk=running_job.pk).update(status=Job.RUNNING)

    newer = CaseReview.objects.create(slug="case-dup")
    _enqueue_review_job(newer)

    other_job.refresh_from_db()
    running_job.refresh_from_db()
    assert other_job.status == Job.QUEUED  # different slug untouched
    assert running_job.status == Job.RUNNING  # in-flight work never killed


@pytest.mark.django_db
def test_supersede_command_dry_run_then_apply():
    j1, j2 = _enqueue_pair("case-swept")
    # Undo the enqueue-time supersede to simulate the pre-fix backlog.
    Job.objects.filter(pk=j1.pk).update(status=Job.QUEUED, error="", completed_at=None)
    CaseReview.objects.filter(pk=j1.payload["review_id"]).update(
        status=CaseReview.STATUS_PENDING, stage="", error=""
    )

    out = StringIO()
    call_command("supersede_queued_reviews", stdout=out)
    j1.refresh_from_db()
    assert j1.status == Job.QUEUED  # dry-run mutates nothing
    assert "would keep" in out.getvalue()

    out = StringIO()
    call_command("supersede_queued_reviews", "--apply", stdout=out)
    j1.refresh_from_db()
    j2.refresh_from_db()
    assert j1.status == Job.DEAD
    assert j2.status == Job.QUEUED
    assert "superseded 1" in out.getvalue()


@pytest.mark.django_db
def test_regrade_all_targets_only_the_latest_review_per_slug():
    import unittest.mock as mock

    from django.contrib.auth import get_user_model
    from rest_framework.test import APIRequestFactory, force_authenticate

    old = CaseReview.objects.create(
        slug="case-hist", status=CaseReview.STATUS_DONE, stage="complete"
    )
    latest = CaseReview.objects.create(
        slug="case-hist", status=CaseReview.STATUS_DONE, stage="complete"
    )

    from review import views

    user = get_user_model().objects.create_user(username="regrader")
    request = APIRequestFactory().post("/api/casework/reviews/regrade_all/")
    force_authenticate(request, user=user)
    with mock.patch.object(
        views.HasContributorRole, "has_permission", return_value=True
    ):
        response = views.regrade_all(request)

    assert response.data["review_ids"] == [latest.pk]
    old.refresh_from_db()
    latest.refresh_from_db()
    # Historical row untouched; only the latest is reset + re-enqueued.
    assert old.status == CaseReview.STATUS_DONE
    assert latest.status == CaseReview.STATUS_PENDING
    assert latest.stage == "queued_for_regrade"
    assert Job.objects.filter(kind="case_review", status=Job.QUEUED).count() == 1
