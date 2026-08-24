"""The case tag controlled vocabulary: axes, canonical terms, and their aliases.

Three models. They replace the markdown tables in
``management/policies/case-tagging/policy.md`` §4.1/§5.1/§5.3/§8.1–8.3 as the operative
source of truth — the policy document remains the *rationale*, this is what code reads.

Why the database rather than a file vendored from the meta repo: a label change through
a file would be a PR to meta, a PR here, a content-hash bump and a deploy — which
guarantees nobody does it, and the quarterly review policy §12 asks for never happens.

WHAT THIS IS FOR. The live corpus carries 144 distinct tags over 82 published cases and
**97 of them are used exactly once** — because
``.agents/caseworker/instructions/case-template.md:106`` tells caseworkers to "pick from
existing tags where possible… or add a new one if needed", against a list nothing ever
exposed. Told to reuse and given no way to discover, people invent, and a tag used once
filters nothing.

So the vocabulary is a real, readable list (``case_tags.views.VocabularyView``) that the
tagger picks from, and :mod:`case_tags.tagger` is what writes to it. There is no review
queue here: the tagger applies its own output, so every guard the vocabulary needs is
enforced in code on the write path — see :mod:`case_tags.write`.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from jawafdehi_shared.tags.normalize import normalize_tag


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


class AliasSource(models.TextChoices):
    """Who put an alias in the table.

    Worth recording rather than inferring: ``SEED`` rows come from the policy
    transcription and are as trustworthy as the document, ``LLM`` rows are the tagger's
    own work applied without review, and ``HUMAN`` rows were typed in the admin. When a
    mapping later turns out wrong, the first question is which of those three it was.
    """

    SEED = "seed", "Seeded from policy"
    LLM = "llm", "Written by the tagger"
    HUMAN = "human", "Entered by a human in the admin"


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

        Returns ``self`` for anything not merged. Raises on a cycle rather than looping:
        the merge chain is human-editable through the admin, so a cycle is an ordinary
        mistake, and a hang is a much worse symptom than an exception.

        Termination comes from ``seen``, not from a hop limit. A fixed cap was tried and
        removed: it cannot prevent anything ``seen`` does not already prevent, and its
        only distinct effect is to reject a *legitimate* chain longer than the cap as if
        it were corrupt.
        """
        seen = {self.id}
        current = self
        while current.status == TagStatus.MERGED and current.merged_into_id is not None:
            nxt = current.merged_into
            if nxt.id in seen:
                raise ValidationError(
                    f"Merge cycle in tag chain starting at {self.id!r}: revisited {nxt.id!r}."
                )
            seen.add(nxt.id)
            current = nxt
        return current

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
    are created by the tagger (or the seed) and never inferred by rule.

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

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize ``value`` on the way in, wherever the write came from.

        :mod:`case_tags.apply` already normalizes, but it is not the only writer: the
        Django admin, a shell session and a data migration all reach the model directly.
        A raw-cased row written that way satisfies the unique constraint and then never
        resolves, because :meth:`TagResolver.resolve` looks up the normalized form — the
        alias would sit in the table looking correct and doing nothing.

        Enforcing it here also makes the unique constraint mean what it should: two rows
        differing only in casing or trailing punctuation are now a collision rather than
        two separate aliases for one string.

        ``bulk_create`` and ``QuerySet.update`` still bypass this, as they bypass every
        model ``save``. The resolver normalizes its lookup keys too, which covers that
        gap on the read side.
        """
        self.value = normalize_tag(self.value)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.value} -> {self.tag_id}"
