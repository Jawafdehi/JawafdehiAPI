from django.contrib.auth.models import User
from django.db import models


class CaseReview(models.Model):
    """A single review run for one Jawafdehi case."""

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

    # Identity is the stable Case PK, not the mutable slug: a case can be
    # re-slugged, which used to orphan its reviews (grade-time resolution was by
    # slug). The FK keys each review to its case row; the display slug is derived
    # from it (see the ``slug`` property). CASCADE so deleting a case takes its
    # reviews with it. null=True is a DB-level safety valve only (kept nullable
    # so the backfill migration can add the column before it is populated); the
    # code path always sets it. The FK column is indexed by Django, covering the
    # flat list (?slug=), the grouped view, and regrade-all, which key on it.
    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="reviews",
        null=True,
    )
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Wall-clock seconds the pipeline took (fetch + convert + score).
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def slug(self):
        """The linked case's current slug, derived from the FK (else empty).

        The slug is exposed for the frontend URL/link but is NO LONGER stored on
        the review — it is read live off the case, so a re-slug is reflected
        automatically and can never orphan the review from its case.
        """
        return self.case.slug if self.case_id else ""

    def __str__(self):
        return f"{self.slug or self.case_id} [{self.status}]"


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
