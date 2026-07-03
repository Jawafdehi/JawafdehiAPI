"""Supersede stale queued review jobs.

A ``case_review`` job grades the LIVE case (the case dict is resolved
server-side at claim time), so two QUEUED jobs for the same slug would produce
the same grading twice — and each duplicate is a full LLM run. Whenever a newer
review of a slug is enqueued, any older still-queued job for that slug is dead
weight: this module marks it DEAD (dead-letter, never claimed) and finalizes its
CaseReview row as failed/"superseded" so nothing dangles as pending forever.

RUNNING jobs are never touched — their work is already paid for and the newest
result wins by completion order anyway.
"""

from django.db import transaction
from django.utils import timezone

from jobs.models import Job

from .models import CaseReview


def supersede_older_queued_jobs(slug, keep_job_id):
    """Dead-letter every QUEUED ``case_review`` job for ``slug`` except ``keep_job_id``.

    Returns the number of jobs superseded. Row-locks the stale jobs (skipping any
    a consumer is concurrently claiming) so a job cannot be claimed and superseded
    at the same time.
    """
    superseded = 0
    with transaction.atomic():
        stale_jobs = list(
            Job.objects.select_for_update(skip_locked=True)
            .filter(kind="case_review", status=Job.QUEUED, payload__slug=slug)
            .exclude(pk=keep_job_id)
        )
        now = timezone.now()
        for job in stale_jobs:
            job.status = Job.DEAD
            job.error = f"Superseded by a newer queued review of the same case (job {keep_job_id})."
            job.completed_at = now
            job.save(update_fields=["status", "error", "completed_at", "updated_at"])
            superseded += 1

            review_id = (job.payload or {}).get("review_id")
            if review_id:
                CaseReview.objects.filter(
                    pk=review_id,
                    status__in=[CaseReview.STATUS_PENDING, CaseReview.STATUS_RUNNING],
                ).update(
                    status=CaseReview.STATUS_FAILED,
                    stage="superseded",
                    error=(
                        "Superseded by a newer queued review of the same case "
                        "before this one was claimed."
                    ),
                    updated_at=now,
                )
    return superseded
