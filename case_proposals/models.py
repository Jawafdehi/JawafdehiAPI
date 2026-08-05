from django.db import models
from django.utils import timezone


class ProposalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class SignalSource(models.TextChoices):
    NGM_DOCKET = "ngm_docket", "Court docket"
    COURT_ORDER = "court_order", "Court order"
    CIAA_PRESS = "ciaa_press", "CIAA press release"
    NEWS = "news", "News"
    CASEWORKER = "caseworker", "Caseworker"


# The intent vocabulary — accepted on create AND applyable on approve; there is
# deliberately no second, wider "stageable but not applyable" set. ``set_status``
# was dropped: a Case has no legal-status field (only the editorial ``state``
# workflow), so status facts belong in the timeline, and ``raw_patch`` is the
# escape hatch for anything the typed intents don't cover. See case_proposals.apply.
#
# ``set_entity_outcome`` is typed rather than left to ``raw_patch`` because
# ``raw_patch`` deliberately cannot reach ``entities`` at all: an outcome write is
# a relationship write guarded by the ``outcome_only_on_accused`` CHECK, so a
# generic JSON patch over Case scalars is the wrong instrument. Without it the
# single most consequential enrichment a verdict produces — moving an acquitted
# defendant off ``charged`` — had no reviewable path, and the August 2026 docket
# sweep found 71 such rows live on published cases.
SUPPORTED_INTENT_TYPES = (
    "append_timeline_entry",
    "link_material",
    "raw_patch",
    "set_entity_outcome",
)


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

    # The tagged-union change intent (see SUPPORTED_INTENT_TYPES). Shape-validated by
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

    # ── originating bus event (populated later by the consumer) ───────────────
    origin_subject = models.CharField(max_length=100, blank=True, default="")
    # 300, matching `dedup_key`, because that is literally what goes in it: the
    # proposal-builder sets origin_msg_id to the matched signal's dedup key. At
    # the original 100 it was too small for the values it is given — a routine
    # docket key ("matched:docket:<courtcase IRI>:hearing:<bs-date>:<slug>") is
    # 108 characters — so every docket-derived proposal failed serializer
    # validation, was recorded as "the model produced something unusable", and
    # left no row for the duplicate check to find. The next scrape then bought
    # another premium model call to fail the same way. Keep these two equal.
    origin_msg_id = models.CharField(max_length=300, blank=True, default="")
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
