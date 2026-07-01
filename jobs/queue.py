"""Queue engine: enqueue, atomic claim, finalize, and reap.

This is the generalized form of the review app's original claim primitive
(``review/views.py`` ``claim_job`` — ``select_for_update(skip_locked=True)``),
lifted into one place and given leases, retries-with-backoff, and dedup so every
job ``kind`` shares one correct queue instead of copy-pasting the pattern.

All state lives in Postgres; ``SELECT … FOR UPDATE SKIP LOCKED`` is the dequeue.
No broker.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import registry
from .models import Job

logger = logging.getLogger("jobs.queue")

#: Exponential backoff (seconds) applied to ``available_at`` when a retryable
#: job is re-queued: 1 min, 5 min, 15 min, then capped. Indexed by attempt count.
_BACKOFF_SECONDS = [60, 300, 900]
_BACKOFF_CAP = 1800


def _backoff(attempts: int) -> timedelta:
    idx = min(max(attempts - 1, 0), len(_BACKOFF_SECONDS) - 1)
    secs = _BACKOFF_SECONDS[idx] if attempts <= len(_BACKOFF_SECONDS) else _BACKOFF_CAP
    return timedelta(seconds=secs)


def enqueue(
    kind: str,
    *,
    payload: Optional[dict] = None,
    dedup_key: Optional[str] = None,
    priority: int = 100,
    available_at=None,
    submitted_by=None,
    max_attempts: Optional[int] = None,
) -> Job:
    """Create a queued job. Deduplicated on ``dedup_key`` when supplied.

    If a NON-terminal job with the same ``dedup_key`` already exists, that job is
    returned unchanged (the enqueue is a no-op) — so re-submitting the same work
    while it is queued/running can't double it up. If the prior same-key job is
    terminal (done/failed/dead), the key is freed and a fresh job is created.
    """
    spec = registry.get(kind)
    if max_attempts is None:
        max_attempts = spec.max_attempts

    if dedup_key:
        existing = Job.objects.filter(dedup_key=dedup_key).first()
        if existing is not None and not existing.is_terminal:
            return existing
        if existing is not None:
            # Prior run finished; free the unique key so a fresh job can take it.
            existing.dedup_key = None
            existing.save(update_fields=["dedup_key", "updated_at"])

    try:
        with transaction.atomic():
            return Job.objects.create(
                kind=kind,
                payload=payload or {},
                dedup_key=dedup_key,
                priority=priority,
                available_at=available_at or timezone.now(),
                submitted_by=submitted_by,
                max_attempts=max_attempts,
            )
    except IntegrityError:
        # Lost a dedup race: another enqueue created the same key first. Return
        # whatever now holds it (there is exactly one, by the unique constraint).
        existing = Job.objects.filter(dedup_key=dedup_key).first()
        if existing is not None:
            return existing
        raise


def claim_next(kinds: list[str]) -> Optional[Job]:
    """Atomically claim the highest-priority available job among ``kinds``.

    Returns the claimed :class:`Job` (now RUNNING, lease stamped, payload
    enriched by the kind's ``build_payload`` hook) or ``None`` when nothing is
    claimable. Safe to run from many concurrent consumers — ``SKIP LOCKED``
    guarantees no two claim the same row.
    """
    # Opportunistic lazy sweep: a claim also reclaims lapsed leases, so no
    # separate reaper process is strictly required (a slow safety cron is still
    # recommended for idle periods). Kept OUTSIDE the claim transaction so a
    # reap contention never blocks the claim.
    reap_expired()

    now = timezone.now()
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(status=Job.QUEUED, kind__in=kinds, available_at__lte=now)
            .order_by("priority", "available_at", "id")
            .first()
        )
        if job is None:
            return None
        spec = registry.get(job.kind)
        job.status = Job.RUNNING
        job.attempts += 1
        job.started_at = now
        job.lease_expires_at = now + spec.lease
        job.stage = "claimed"
        job.error = ""
        job.save(
            update_fields=[
                "status",
                "attempts",
                "started_at",
                "lease_expires_at",
                "stage",
                "error",
                "updated_at",
            ]
        )

    # Resolve server-side payload (e.g. the case dict) AFTER the row is safely
    # RUNNING, mirroring the old claim endpoint. A build_payload failure fails
    # the job (it cannot run without its input).
    if spec.build_payload is not None:
        try:
            extra = spec.build_payload(job)
            if extra:
                job.payload = {**(job.payload or {}), **extra}
                job.save(update_fields=["payload", "updated_at"])
        except Exception as exc:  # noqa: BLE001 - payload build failure -> fail job
            logger.warning("build_payload failed for %s: %s", job, exc)
            finalize(job, status=Job.FAILED, error=f"payload build failed: {exc}")
            return None
    return job


def touch(job: Job, *, stage: Optional[str] = None) -> Job:
    """Extend a RUNNING job's lease (heartbeat) and optionally update ``stage``.

    Long-legitimate jobs (e.g. a 300-page OCR run) call this via the stage
    endpoint so the reaper doesn't mistake slow progress for a crash.
    """
    if job.status != Job.RUNNING:
        return job
    spec = registry.get(job.kind)
    job.lease_expires_at = timezone.now() + spec.lease
    fields = ["lease_expires_at", "updated_at"]
    if stage is not None:
        job.stage = stage[:64]
        fields.append("stage")
    job.save(update_fields=fields)
    return job


def finalize(
    job: Job,
    *,
    status: str,
    result: Optional[dict] = None,
    error: str = "",
    retryable: bool = False,
    duration_seconds: Optional[float] = None,
) -> Job:
    """Finalize a claimed job: DONE, FAILED, re-queued (retry), or DEAD.

    - ``status=done`` → DONE, ``result`` stored.
    - ``status=failed`` + ``retryable`` + attempts remaining → back to QUEUED with
      backoff on ``available_at``.
    - ``status=failed`` + (not retryable OR attempts exhausted) → FAILED, or DEAD
      when retries are exhausted on a retryable failure.
    """
    now = timezone.now()
    if status == Job.DONE:
        job.status = Job.DONE
        job.result = result
        job.stage = "complete"
        job.error = ""
        job.completed_at = now
        job.lease_expires_at = None
    else:
        if retryable and job.can_retry:
            job.status = Job.QUEUED
            job.stage = "retry_scheduled"
            job.error = error
            job.available_at = now + _backoff(job.attempts)
            job.lease_expires_at = None
            job.started_at = None
        else:
            # Retryable-but-exhausted → DEAD (dead-letter); non-retryable → FAILED.
            job.status = Job.DEAD if retryable else Job.FAILED
            job.stage = "failed"
            job.error = error
            job.completed_at = now
            job.lease_expires_at = None

    if duration_seconds is not None:
        job.duration_seconds = duration_seconds
    job.save()

    # Let the kind apply the outcome to its own domain record.
    spec = registry.get(job.kind)
    if job.status == Job.DONE and result is not None and spec.on_result is not None:
        try:
            spec.on_result(job, result)
        except Exception as exc:  # noqa: BLE001 - domain apply is best-effort
            logger.warning("on_result failed for %s: %s", job, exc)
    elif job.status in (Job.FAILED, Job.DEAD):
        _apply_failure(job)
    return job


def _apply_failure(job: Job) -> None:
    """Notify the kind's domain record of a TERMINAL failure (best-effort)."""
    spec = registry.get(job.kind)
    if spec.on_failure is None:
        return
    try:
        spec.on_failure(job)
    except Exception as exc:  # noqa: BLE001 - domain apply is best-effort
        logger.warning("on_failure failed for %s: %s", job, exc)


def reap_expired(*, limit: int = 50) -> int:
    """Re-queue or dead-letter RUNNING jobs whose lease has lapsed.

    A crashed worker leaves a RUNNING row with a past ``lease_expires_at``. Such
    a job is re-queued (with backoff) if it has attempts left, else marked DEAD.
    Returns the number of jobs reaped. Bounded by ``limit`` per call so a lazy
    sweep inside claim stays cheap.
    """
    now = timezone.now()
    reaped = 0
    with transaction.atomic():
        stale = list(
            Job.objects.select_for_update(skip_locked=True)
            .filter(status=Job.RUNNING, lease_expires_at__lt=now)
            .order_by("lease_expires_at")[:limit]
        )
        for job in stale:
            if job.can_retry:
                job.status = Job.QUEUED
                job.stage = "reclaimed_after_lease_expiry"
                job.available_at = now + _backoff(job.attempts)
                job.lease_expires_at = None
                job.started_at = None
                dead = None
            else:
                job.status = Job.DEAD
                job.stage = "dead_lease_expired"
                job.error = (
                    job.error or "lease expired and max_attempts exhausted"
                )
                job.completed_at = now
                job.lease_expires_at = None
                dead = job
            job.save()
            if dead is not None:
                _apply_failure(dead)
            reaped += 1
    if reaped:
        logger.info("reaped %d expired job(s)", reaped)
    return reaped
