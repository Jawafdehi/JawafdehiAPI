"""Central job-queue model.

ONE queue table for the whole platform. The queue holds *lifecycle + scheduling*
state (status, priority, attempts, lease, backoff); domain records (``CaseReview``,
``Material``, …) keep their own tables and carry a job's input in ``payload`` /
read its output from ``result``.

The queue engine is Postgres itself: dequeue is an atomic
``SELECT … FOR UPDATE SKIP LOCKED`` (see ``jobs.queue.claim_next``), the same
primitive the review app already used — generalized here over a ``kind`` string so
any number of job types share one claim/lease/retry/dashboard. No broker. See
``docs/jobs-queue-design.md`` for the decision + rationale.
"""

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class Job(models.Model):
    """A single unit of queued work of a given ``kind``."""

    # Lifecycle states. ``dead`` is the dead-letter terminal state a job reaches
    # when it exhausts ``max_attempts`` (distinct from ``failed``, which is a
    # handler-reported terminal failure that will NOT be retried).
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"
    STATUS_CHOICES = [
        (QUEUED, "Queued"),
        (RUNNING, "Running"),
        (DONE, "Done"),
        (FAILED, "Failed"),
        (DEAD, "Dead (retries exhausted)"),
    ]
    #: The non-terminal states a claim/reaper cares about.
    ACTIVE_STATUSES = (QUEUED, RUNNING)
    #: Terminal states (finalized; a stale result submission is rejected).
    TERMINAL_STATUSES = (DONE, FAILED, DEAD)

    #: e.g. "case_review" | "material_convert" | "reindex". Free-form; the set of
    #: valid kinds is defined by the handler registry (jobs.registry), not here.
    kind = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=QUEUED, db_index=True
    )
    #: Lower runs sooner. Default 100 leaves head-room to bump work either way.
    priority = models.IntegerField(default=100)

    #: Handler input (everything a DB-free consumer needs to run the job).
    payload = models.JSONField(default=dict, blank=True)
    #: Handler output (set on success).
    result = models.JSONField(null=True, blank=True)
    #: Human-readable progress ping (best-effort; set via the stage endpoint).
    stage = models.CharField(max_length=64, blank=True, default="")

    #: Natural key of the work unit. UNIQUE (when set) so the same logical work
    #: cannot be enqueued twice concurrently (e.g. one convert per material IRI).
    dedup_key = models.CharField(
        max_length=255, null=True, blank=True, unique=True
    )

    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=3)

    #: Set on claim to now()+lease; cleared on finalize. A RUNNING job whose lease
    #: has lapsed is presumed abandoned (crashed worker) and is reaped.
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    #: A job is only claimable once now() >= available_at. Used for delayed
    #: enqueue and retry backoff.
    available_at = models.DateTimeField(default=timezone.now, db_index=True)

    error = models.TextField(blank=True, default="")
    submitted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="jobs"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    #: Wall-clock seconds the last run took (finalize - start).
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Covers the hot claim query: filter(status, kind, available_at) +
            # order_by(priority, available_at, id).
            models.Index(
                fields=["status", "kind", "priority", "available_at"],
                name="jobs_claim_idx",
            ),
        ]

    def __str__(self):
        return f"{self.kind}#{self.pk} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts
