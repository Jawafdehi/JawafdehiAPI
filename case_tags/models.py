"""The case tag controlled vocabulary, and the review queue that changes it.

Four models, two jobs.

**The vocabulary** — :class:`TagAxis`, :class:`Tag`, :class:`TagAlias`. These replace
the markdown tables in ``management/policies/case-tagging/policy.md`` §4.1/§5.1/§5.3/
§8.1–8.3 as the operative source of truth. The policy document remains the *rationale*;
this is what the code reads.

Why the database rather than a file vendored from the meta repo: a label change through
a file would be a PR to meta, a PR here, a content-hash bump and a deploy — which
guarantees nobody does it, and the quarterly review policy §12 asks for never happens.

**The review queue** — :class:`TagProposal`. Every vocabulary change an automation wants
lands here first and waits for a human tick. Deliberately shaped like
``case_proposals.CaseUpdateProposal`` (same three statuses, same ``dedup_key``
idempotency spine, same ``reviewer``/``reviewed_at``/``review_notes`` triple) because it
is the same pattern and a second, differently-shaped review pipeline would be one more
thing to learn.

WHY THE VOCABULARY IS NOT SELF-SERVICE. The live corpus carries 144 distinct tags over
82 published cases and **97 of them are used exactly once** — because
``.agents/caseworker/instructions/case-template.md:106`` tells caseworkers to "pick from
existing tags where possible… or add a new one if needed", against a list nothing ever
exposed. A tag used once filters nothing. So invention is still allowed here, but it is
routed: propose freely, and only a human tick makes a term public.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class TagStatus(models.TextChoices):
    """Lifecycle from design.md §12.

    ``MERGED`` is the interesting one: it keeps a retired slug resolvable rather than
    deleting it, so stored ``Case.tags`` values and ``?tags=`` URLs minted before the
    merge keep working. See :meth:`Tag.canonical`.
    """

    PROPOSED = "proposed", "Proposed"
    ACTIVE = "active", "Active"
    DEPRECATED = "deprecated", "Deprecated"
    MERGED = "merged", "Merged"


class AxisMembers(models.TextChoices):
    """Where an axis's legal values come from.

    An axis that is not ``ENUMERATED`` legitimately has **zero** :class:`Tag` rows, and
    a validator must not read that as "no legal values". ``institution`` and ``person``
    resolve against the case's ``entities`` relation (policy §8.6: the tag "is a filter
    handle, not an independent name store"); ``geography`` is the official 7-province /
    77-district list, which is fixed by law and not ours to edit.
    """

    ENUMERATED = "enumerated", "Enumerated here"
    ENTITIES = "entities", "From the case entities relation"
    EXTERNAL = "external", "From a fixed external list"


class ProposalKind(models.TextChoices):
    """The two decisions, kept apart because their gates differ.

    An ``ALIAS_EQUIVALENCE`` maps a string the corpus already contains onto a term that
    already exists, so the only question is whether it is *correct* — one informed tick
    settles it. A ``NEW_TERM`` adds to the public filter set, so correctness is not
    enough and recurrence has to carry it (policy §12: at least three existing cases,
    and "a term justified by a single case is a nickname, not a controlled term").

    Collapsing the two would lose exactly that distinction and hand back the failure
    mode this app exists to close.
    """

    ALIAS_EQUIVALENCE = "alias_equivalence", "Alias equivalence"
    NEW_TERM = "new_term", "New term"


class ProposalStatus(models.TextChoices):
    """Mirrors ``case_proposals.ProposalStatus`` verbatim, on purpose."""

    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class AliasSource(models.TextChoices):
    SEED = "seed", "Seeded from policy"
    LLM = "llm", "LLM proposal, human-approved"
    HUMAN = "human", "Entered by a human"


# Following ``merged_into`` is a walk, not a hop, and the data is human-editable
# through the admin, so a cycle is reachable by ordinary mistake. Bound the walk and
# raise rather than spin.
_MAX_MERGE_DEPTH = 10


class TagAxis(models.Model):
    """One question a tag answers — offence, sector, status, and so on.

    The whole point of the axis is that today's tags do not have one: ``Embezzlement``,
    ``Bagmati``, ``CIAA`` and ``Forestry`` sit in a single flat list as though they were
    the same kind of fact, which is why the public filter panel is unreadable.

    The per-case bounds live here as DATA rather than in code so the write validator is
    table-driven — adding an axis must not mean editing a validator.
    """

    id = models.SlugField(primary_key=True, max_length=40)
    label_ne = models.CharField(max_length=100)
    label_en = models.CharField(max_length=100)

    # policy §3. NOTE: offence is seeded 0..3, not 1..3 as §3 writes it — see the
    # 2026-08-23 decision recorded in the seed migration. §3 required at least one
    # offence tag while §8.1 may not cover every case, so as written a case fitting
    # none could not legally be saved.
    min_per_case = models.PositiveSmallIntegerField(default=0)
    max_per_case = models.PositiveSmallIntegerField(default=3)

    # policy §3 — status and verdict are displayed prominently and separately, because
    # they answer the questions a reader asks first ("is this still going? what
    # happened?"). Neither was recorded at all before this programme.
    highlighted = models.BooleanField(default=False)

    members = models.CharField(
        max_length=12, choices=AxisMembers.choices, default=AxisMembers.ENUMERATED
    )
    # Free text rather than choices: "court-data", "entity-outcomes", "caseworker",
    # "llm+review". It is documentation for the reviewer, not a dispatch key — nothing
    # branches on it, and pinning it to an enum would invite something to start.
    set_by = models.CharField(max_length=40, blank=True, default="")
    sort_order = models.PositiveSmallIntegerField(default=100)
    note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name_plural = "tag axes"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_per_case__gte=models.F("min_per_case")),
                name="tag_axis_max_at_least_min",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.id} ({self.label_en})"


class Tag(models.Model):
    """One canonical term. The slug is the stored value; the labels are display only.

    policy §7.1 rule 2 and decision D10: readers see कर छली, the store holds
    ``tax-evasion``. That is what keeps filters and URLs out of the business of being
    script-sensitive — the corpus currently holds ``Ncell`` and ``एनसेल`` as two
    separate tags, so a filter on either finds half the cases.
    """

    id = models.SlugField(primary_key=True, max_length=60)
    axis = models.ForeignKey(TagAxis, on_delete=models.PROTECT, related_name="tags")

    # NULLABLE, and not an oversight. ``first-instance-decided`` has no single Nepali
    # label: policy §4.2 composes it from the deciding court (विशेष अदालतको फैसला /
    # जिल्ला अदालतको फैसला / उच्च अदालतको फैसला) and writes "*(composed — see 4.2)*" in
    # the label column for exactly that reason. Inventing one label there would encode
    # a wrong fact permanently, which is the failure this whole programme is about.
    label_ne = models.CharField(max_length=200, blank=True, null=True)
    label_ne_composed = models.JSONField(blank=True, null=True)
    label_en = models.CharField(max_length=200)

    status = models.CharField(
        max_length=12, choices=TagStatus.choices, default=TagStatus.PROPOSED, db_index=True
    )
    # Required when status is MERGED, forbidden otherwise — enforced in clean() and by
    # a DB constraint, because the admin and the shell both bypass clean().
    merged_into = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="merged_from"
    )

    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["axis__sort_order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status=TagStatus.MERGED, merged_into__isnull=False)
                    | (~models.Q(status=TagStatus.MERGED) & models.Q(merged_into__isnull=True))
                ),
                name="tag_merged_iff_merged_into",
            ),
            models.CheckConstraint(
                condition=~models.Q(merged_into=models.F("id")),
                name="tag_not_merged_into_itself",
            ),
        ]

    def __str__(self) -> str:
        return self.id

    def clean(self) -> None:
        super().clean()
        if self.status == TagStatus.MERGED and self.merged_into_id is None:
            raise ValidationError({"merged_into": "Required when status is 'merged'."})
        if self.status != TagStatus.MERGED and self.merged_into_id is not None:
            raise ValidationError(
                {"merged_into": "Only a 'merged' tag may point at a replacement."}
            )
        if self.merged_into_id == self.id:
            raise ValidationError({"merged_into": "A tag cannot be merged into itself."})

    def canonical(self) -> Tag:
        """Follow ``merged_into`` to the term that should actually be used.

        Returns ``self`` for anything not merged. Raises on a cycle rather than
        looping: the merge chain is human-editable through the admin, so a cycle is an
        ordinary mistake, and a hang is a much worse symptom than an exception.
        """
        seen = {self.id}
        current = self
        for _ in range(_MAX_MERGE_DEPTH):
            if current.status != TagStatus.MERGED or current.merged_into_id is None:
                return current
            nxt = current.merged_into
            if nxt.id in seen:
                raise ValidationError(
                    f"Merge cycle in tag chain starting at {self.id!r}: revisited {nxt.id!r}."
                )
            seen.add(nxt.id)
            current = nxt
        raise ValidationError(
            f"Merge chain from {self.id!r} exceeded {_MAX_MERGE_DEPTH} hops; likely a cycle."
        )

    def label(self, lang: str = "ne") -> str:
        """Display label, falling back rather than ever returning blank.

        ``ne`` falls back to English when there is no Nepali label — which is the
        composed-label case above, and better than an empty chip. A caller that needs
        the composed form must ask for it explicitly; this method cannot, because it
        does not know the deciding court.
        """
        if lang == "ne" and self.label_ne:
            return self.label_ne
        return self.label_en or self.id


class TagAlias(models.Model):
    """A raw string that resolves to a canonical :class:`Tag`.

    This is the table that does the actual collapsing, and the only place the corpus's
    fragmentation is expressible: ``research/corpus-analysis.md`` §6 measures illicit
    enrichment stored seven ways (``Illegal Property Acquisition`` ×16, ``Assets Beyond
    Known Income`` ×8, ``Illicit Enrichment`` ×2, ``Illegal Enrichment`` ×2, ``Illegal
    enrichment`` ×1, ``Illegal Property`` ×1, ``Illegal Wealth`` ×1) and procurement
    four ways.

    No mechanical rule produces these. Nothing derives ``एनसेल`` → ``ncell``, and
    deciding that ``Assets Beyond Known Income`` is the same concept as ``Illegal
    Property Acquisition`` is a judgement about our own published cases. So rows here
    are created by an approved :class:`TagProposal` (or the seed), never inferred.

    ``value`` stores the NORMALIZED form (``jawafdehi_shared.tags.normalize``), so the
    resolver normalizes once and looks up once, and casing collisions cannot produce
    two rows for one string.
    """

    value = models.CharField(max_length=300, unique=True, db_index=True)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="aliases")
    source = models.CharField(
        max_length=8, choices=AliasSource.choices, default=AliasSource.LLM
    )
    # Who ticked it, and when. Blank for seeded rows.
    approved_by = models.CharField(max_length=100, blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["value"]
        verbose_name_plural = "tag aliases"

    def __str__(self) -> str:
        return f"{self.value} -> {self.tag_id}"


class TagProposal(models.Model):
    """A vocabulary change awaiting a human tick.

    Nothing here writes a :class:`Tag` or a :class:`TagAlias` until it is approved, at
    which point :mod:`case_tags.apply` performs the write. That ordering is the entire
    safety property: an automation may propose anything, and only a human makes it
    public.

    It also makes an approval REVERSIBLE, which is why aliases are applied at index
    time rather than by rewriting ``Case.tags``. Un-approve, reindex, and the facet
    reverts — no data migration, no snapshot, nothing irreversible. That property is
    what took the two riskiest steps out of this programme.
    """

    kind = models.CharField(max_length=20, choices=ProposalKind.choices, db_index=True)

    # Tagged union keyed on ``kind``; shape-validated by the serializer on create and
    # again in ``apply``:
    #   alias_equivalence -> {raw_value, proposed_tag_id, case_count, example_case_slugs}
    #   new_term          -> {axis, proposed_slug, label_ne, label_en, rationale,
    #                         quoted_span, case_slug}
    payload = models.JSONField()

    confidence = models.FloatField()
    status = models.CharField(
        max_length=12,
        choices=ProposalStatus.choices,
        default=ProposalStatus.PENDING,
        db_index=True,
    )

    # ── provenance ────────────────────────────────────────────────────────────────
    # "consumer:<name>" for automation, "caseworker:<id>" for a hand-filed one, as in
    # case_proposals.
    detected_by = models.CharField(max_length=100)
    # Idempotency spine, same role as on CaseUpdateProposal: the same fact never
    # re-proposes and a REJECTION STAYS STICKY. Without this the alias proposer refills
    # the queue every run with rows a human already refused, which is how a review
    # queue becomes something people stop opening. Producers build it deterministically
    # (e.g. "alias:<normalized raw value>").
    dedup_key = models.CharField(max_length=300, unique=True, db_index=True)

    # ── review ────────────────────────────────────────────────────────────────────
    reviewer = models.CharField(max_length=100, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-confidence", "-created_at"]
        constraints = [
            # Same reasoning as case_proposals: the serializer rejects out-of-range
            # confidence, but the queue is sorted by it and the admin/shell bypass the
            # serializer entirely.
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0.0, confidence__lte=1.0),
                name="tag_proposal_confidence_between_0_and_1",
            ),
        ]
        indexes = [models.Index(fields=["kind", "status"])]

    def __str__(self) -> str:
        return f"{self.kind}:{self.dedup_key} [{self.status}]"

    @property
    def is_decided(self) -> bool:
        return self.status != ProposalStatus.PENDING
