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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Wall-clock seconds the pipeline took (fetch + convert + score).
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

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
