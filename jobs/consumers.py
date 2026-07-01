"""Kind registrations owned by consumer apps.

The jobs app is domain-agnostic; the concrete job *kinds* and their server-side
hooks live with the apps that own the work. They are collected here (imported by
``jobs.registry`` at import time) so registration happens once, centrally, without
the jobs app importing consumer internals at module load of ``models``/``queue``.

Each ``register`` binds a ``kind`` to:
- ``build_payload`` — resolve the input a DB-free consumer needs, at claim time.
- ``on_result``     — apply a successful result to the owning domain record.
"""

from __future__ import annotations

import logging

from .registry import KindSpec, register

logger = logging.getLogger("jobs.consumers")


# --- case_review: the casework review pipeline (first consumer) --------------
#
# build_payload resolves the case dict server-side (exactly as the old
# review.claim_job did) so the poller needs no case DB access. on_result
# finalizes the linked CaseReview row from the poller's scored result.


def _case_review_build_payload(job) -> dict:
    """Resolve the case dict + review config for a claimed case_review job.

    Imports are local to avoid a jobs→review import at app-load time.
    """
    from review import case_provider
    from review.models import ReviewConfig

    slug = (job.payload or {}).get("slug")
    if not slug:
        raise ValueError("case_review job payload is missing 'slug'.")

    case = case_provider.get_case(slug)
    case.setdefault("slug", slug)
    cfg = ReviewConfig.get_active()
    return {
        "case": case,
        "config": {
            "pass_threshold": cfg.pass_threshold,
            "revise_threshold": cfg.revise_threshold,
            "llm_samples": cfg.llm_samples,
        },
    }


def _case_review_on_result(job, result: dict) -> None:
    """Finalize the CaseReview linked to this job from the scored result."""
    from review.models import CaseReview

    review_id = (job.payload or {}).get("review_id")
    if not review_id:
        return
    try:
        review = CaseReview.objects.get(pk=review_id)
    except CaseReview.DoesNotExist:
        logger.warning("case_review job %s references missing review %s", job.pk, review_id)
        return

    review.status = CaseReview.STATUS_DONE
    review.stage = "complete"
    review.error = ""
    review.case_title = result.get("case_title", "") or ""
    review.case_state = result.get("case_state", "") or ""
    review.case_type = result.get("case_type", "") or ""
    review.source_count = result.get("source_count", 0) or 0
    review.sources_converted = result.get("sources_converted", 0) or 0
    review.result = result.get("result")
    review.completed_at = job.completed_at
    if result.get("duration_seconds") is not None:
        review.duration_seconds = result["duration_seconds"]
    review.save()


def _case_review_on_failure(job) -> None:
    """Mark the linked CaseReview failed when its job terminally fails/dies."""
    from review.models import CaseReview

    review_id = (job.payload or {}).get("review_id")
    if not review_id:
        return
    updated = CaseReview.objects.filter(pk=review_id).exclude(
        status=CaseReview.STATUS_DONE
    ).update(
        status=CaseReview.STATUS_FAILED,
        stage="failed",
        error=job.error or "job failed",
        completed_at=job.completed_at,
    )
    if not updated:
        logger.info("case_review job %s failure: review %s not updated", job.pk, review_id)


register(
    KindSpec(
        kind="case_review",
        lease_seconds=900,  # 10-min reviews with head-room; heartbeat extends it.
        max_attempts=2,  # a review failure is usually deterministic; don't hammer.
        build_payload=_case_review_build_payload,
        on_result=_case_review_on_result,
        on_failure=_case_review_on_failure,
    )
)
