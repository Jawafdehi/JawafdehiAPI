from django.db import models
from django.utils import timezone


class ProposalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    SUPERSEDED = "superseded", "Superseded"


class SignalSource(models.TextChoices):
    NGM_DOCKET = "ngm_docket", "Court docket"
    COURT_ORDER = "court_order", "Court order"
    CIAA_PRESS = "ciaa_press", "CIAA press release"
    NEWS = "news", "News"
    CASEWORKER = "caseworker", "Caseworker"


# Intent types the approve action can APPLY today. ``set_status`` is deliberately
# absent: a Case has no legal-status field (only the editorial ``state``
# workflow), so status facts live as timeline entries. The ``raw_patch`` escape
# hatch covers anything the typed intents don't. See case_proposals.apply.
SUPPORTED_INTENT_TYPES = ("append_timeline_entry", "link_material", "raw_patch")

# The full vocabulary the model/UI accept on create (superset of the applyable
# set) so a producer can stage a forward-looking intent; apply-time validation
# is the authority on what can actually be committed.
KNOWN_INTENT_TYPES = ("append_timeline_entry", "set_status", "link_material", "raw_patch")


class CaseUpdateProposal(models.Model):
    """A proposed enrichment to a Case, awaiting caseworker review.

    Nothing here writes a Case until a caseworker approves it, at which point the
    ``intent`` is applied via the sanctioned write path (``case_proposals.apply``).
    The Case (and its ``timeline``) remains the durable source of truth; this
    model is the transient/operational review-staging layer.
    """

    # The Case is addressed by its slug (the canonical lookup key). We keep a
    # slug (not an FK) so a producer can stage a proposal referencing a case by
    # its stable public handle without a DB join; the apply step resolves it.
    case_slug = models.SlugField(max_length=50, db_index=True)
    # Denormalised for the queue UI; the Case remains the source of truth.
    case_title = models.CharField(max_length=200, blank=True, default="")
    source_kind = models.CharField(max_length=20, choices=SignalSource.choices)

    # The tagged-union change intent (see KNOWN_INTENT_TYPES). Shape-validated by
    # the serializer on create; deeply validated + applied on approve.
    intent = models.JSONField()

    # REQUIRED on every proposal — the autonomy-dial input. Range [0, 1].
    confidence = models.FloatField()

    status = models.CharField(
        max_length=12,
        choices=ProposalStatus.choices,
        default=ProposalStatus.PENDING,
        db_index=True,
    )

    # ── provenance ───────────────────────────────────────────────────────────
    source = models.CharField(max_length=500, blank=True, default="")
    # "consumer:<name>" for automation, "caseworker:<id>" for a hand-filed one.
    detected_by = models.CharField(max_length=100)
    # Idempotency spine: the same fact never re-proposes, and a rejection stays
    # sticky. Producers construct this deterministically (e.g.
    # "docket:<iri>:hearing:<bs-date>").
    dedup_key = models.CharField(max_length=300, unique=True, db_index=True)
    # Id of a proposal this one supersedes (a re-listed fact), if any.
    supersedes = models.CharField(max_length=64, blank=True, default="")

    # ── originating bus event (populated later by the consumer) ───────────────
    origin_subject = models.CharField(max_length=100, blank=True, default="")
    origin_msg_id = models.CharField(max_length=100, blank=True, default="")
    subject_refs = models.JSONField(default=list, blank=True)

    # ── review ────────────────────────────────────────────────────────────────
    reviewer = models.CharField(max_length=100, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # The serializer already rejects out-of-range confidence, but confidence is
            # the autonomy-dial input: a future producer writing through the ORM (or the
            # admin, or a shell) bypasses the serializer entirely and would persist a
            # value the dial then reads as gospel. Enforce the range in the DB, where
            # nothing can route around it.
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0.0, confidence__lte=1.0),
                name="case_proposal_confidence_between_0_and_1",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["case_slug", "status"]),
        ]

    def __str__(self):
        return f"{self.get_status_display()} proposal {self.pk} for {self.case_slug}"

    @property
    def intent_type(self):
        return (self.intent or {}).get("type")
