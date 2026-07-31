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

    case_id = (job.payload or {}).get("case_id")
    if not case_id:
        raise ValueError("case_review job payload is missing 'case_id'.")

    # Resolve by the stable case PK: the serialized dict already carries the
    # case's current slug, so a re-slug between submit and claim never orphans it.
    case = case_provider.get_case_by_id(case_id)

    # Stamp the review's "picked up" time from the job's claim time so the UI can
    # show it (and compute elapsed = finished - picked up) while the review is
    # still running, not only once it finishes. Best-effort: a failure here must
    # never fail the claim, and we never overwrite an existing value.
    review_id = (job.payload or {}).get("review_id")
    if review_id and job.started_at is not None:
        try:
            from review.models import CaseReview

            CaseReview.objects.filter(pk=review_id, started_at__isnull=True).update(
                started_at=job.started_at
            )
        except Exception as exc:  # noqa: BLE001 - pickup stamp is best-effort
            logger.warning("could not stamp started_at for review %s: %s", review_id, exc)

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
    # "Picked up" time = the job's claim time; keep any earlier stamp from claim.
    review.started_at = review.started_at or job.started_at
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
        started_at=job.started_at,
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


# --- material_convert: OCR a Material's source document → data["text"] -------
#
# The data-plane FTS feed (docs/data-plane-design.md §5). build_payload resolves
# the source document URL(s) from the Material's associatedMedia (server-side, so
# the consumer stays DB-free); on_result stores the OCR markdown as a MARKDOWN
# MediaObject and sets data["text"] (the field the search indexer reads). The
# worker-side OCR handler lives in materials.job_handlers (heavy deps off the API).


def _material_convert_build_payload(job) -> dict:
    from materials.conversion import build_convert_payload

    return build_convert_payload(job)


def _material_convert_on_result(job, result: dict) -> None:
    from materials.conversion import apply_convert_result

    apply_convert_result(job, result)


register(
    KindSpec(
        kind="material_convert",
        lease_seconds=1800,  # 300-page OCR is minutes; heartbeat extends per page.
        max_attempts=2,  # network/OCR flakes retry once; then dead-letter.
        build_payload=_material_convert_build_payload,
        on_result=_material_convert_on_result,
        # No on_failure: a failed convert just leaves data["text"] unset; the
        # Material is still served (metadata-searchable), and a re-upload re-runs.
    )
)


# --- court_scrape: crawl a court's recent cause-lists into the ngm lake -------
#
# Unlike the DB-free review/material kinds, court_scrape WRITES via the courts
# ORM, so it is run by an in-process consumer with ngm DB access
# (courts/management/commands/scrape_worker.py), not the external HTTP poller.
# The queue still gives it lease/retry/backoff/dedup + the /api/jobs dashboard.
# No build_payload (the payload is self-contained: court + lookback) and no
# on_result (the crawl writes the lake itself; job.result just records stats).


register(
    KindSpec(
        kind="court_scrape",
        lease_seconds=1800,  # an incremental per-court crawl is minutes.
        max_attempts=3,  # portal/network flakes retry with backoff, then dead-letter.
    )
)


# --- case_proposal_intent: draft a change intent from an observed signal ------
#
# The bus's proposal-builder consumer enqueues one of these per matched signal
# and acks immediately, so the model call happens out here rather than inside a
# JetStream ack window. build_payload resolves the case snapshot server-side (so
# the worker stays DB-free, exactly like case_review); on_result stages a PENDING
# CaseUpdateProposal through the same serializer the HTTP create path uses.
#
# Unlike the kinds above, the whole spec is defined by the owning app rather than
# assembled here: its hooks are tightly coupled to the proposal serializer and to
# the question of what a MODEL may draft as opposed to what the system accepts,
# and that reasoning belongs beside the model it guards.


def _register_case_proposal_intent() -> None:
    """Register the intent kind, tolerating an import failure.

    Wrapped because this module is imported by ``jobs.registry`` at app load: an
    ImportError raised here would take the whole queue down, and the queue must
    keep running the kinds it already knows even if one app's spec is broken.
    Mirrors the guard ``jobs.registry`` puts around importing this module.
    """
    try:
        from case_proposals.job_kind import SPEC

        register(SPEC)
    except Exception:  # noqa: BLE001 - one bad spec must not break the queue
        logger.exception("could not register the case_proposal_intent kind")


_register_case_proposal_intent()
