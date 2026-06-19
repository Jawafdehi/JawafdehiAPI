from django.contrib.auth.models import User
from django.db import models


class CaseReview(models.Model):
    """A single review run for one Jawafdehi case slug."""

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "In progress"),
        (STATUS_DONE, "Done"),
        (STATUS_FAILED, "Failed"),
    ]

    # Stable internal case identifier (cases.Case.case_id, e.g. "case-a1b2c3d4").
    # This — not the slug — is the review's primary case reference: it is resolved
    # once at submit time and is what executions are grouped/deduped by. Kept as a
    # plain indexed string (not a FK) so the review system stays decoupled from the
    # case table and the reviewer can run fully over HTTP. Blank only for legacy
    # rows whose slug could not be resolved by the backfill migration.
    case_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    slug = models.CharField(max_length=255)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    submitted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="reviews"
    )

    # Progress / pipeline transparency
    stage = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")

    # Snapshot of the case we pulled (title/state etc. for display)
    case_title = models.CharField(max_length=512, blank=True, default="")
    case_state = models.CharField(max_length=64, blank=True, default="")
    case_type = models.CharField(max_length=32, blank=True, default="")
    source_count = models.IntegerField(default=0)
    sources_converted = models.IntegerField(default=0)

    # Full structured review result (per-rule score+confidence, overall, gates)
    result = models.JSONField(null=True, blank=True)

    # Which reviewer(s) produced this grade: a compact summary derived from the
    # result's per-provider token usage, e.g.
    #   [{"tier": "premium", "provider": "claude_cli", "model": "opus", "calls": 7},
    #    {"tier": "cheap",   "provider": "codex_cli",  "model": "gpt-5-codex", "calls": 41}]
    # Grading is multi-provider per review (gate rules -> premium, routine ->
    # cheap), so this is a list, not a single value. Populated on result submit.
    reviewers = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Wall-clock seconds the pipeline took (fetch + convert + score).
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one active (pending/running) review per case. Enforces the
            # "no duplicate re-runs" rule at the DB level so concurrent submits
            # can't both slip past an application-level pre-check. Keyed on the
            # stable case_id (the primary case reference), not the slug. Literal
            # status values mirror STATUS_PENDING / STATUS_RUNNING (Meta cannot
            # see the class attributes by name).
            models.UniqueConstraint(
                fields=["case_id"],
                condition=models.Q(status__in=["pending", "running"]),
                name="uniq_active_review_per_case",
            )
        ]

    def __str__(self):
        return f"{self.slug} [{self.status}]"


class ReviewConfig(models.Model):
    """Singleton (id=1) holding global disposition thresholds + LLM sampling."""

    pass_threshold = models.IntegerField(default=80)
    revise_threshold = models.IntegerField(default=60)
    # How many times each LLM rule is sampled to compute mean + variance.
    llm_samples = models.IntegerField(default=3)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_active(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
