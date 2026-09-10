"""The canonical tag vocabulary and its alias table.

Two tables, no case data. ``Tag`` is the controlled list that may become a public
filter; ``TagAlias`` maps every raw string we have ever seen onto one of them (or
records that we retired it). Applying tags to cases stays in ``cases`` -- see
``Case.tags``.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from case_tags.normalize import Resolution, ResolvedTag, normalize

__all__ = [
    "Resolution",
    "ResolvedTag",
    "Tag",
    "TagAlias",
    "TagStatus",
    "resolve",
]


class TagStatus(models.TextChoices):
    """design.md §12 lifecycle: proposed -> active -> deprecated | merged."""

    PROPOSED = "proposed", "Proposed"
    ACTIVE = "active", "Active"
    DEPRECATED = "deprecated", "Deprecated"
    MERGED = "merged", "Merged"


class Tag(models.Model):
    """One approved vocabulary term.

    The id is the slug (``land-grab``), not a surrogate key: it is what the search
    index stores, what ``?tags=`` carries, and what a reviewer reads in
    ``vocabulary.yml``. A numeric pk would put an opaque join between all three.
    """

    id = models.SlugField(primary_key=True, max_length=64)
    label_ne = models.CharField(max_length=120)
    label_en = models.CharField(max_length=120)
    status = models.CharField(
        max_length=16, choices=TagStatus.choices, default=TagStatus.PROPOSED
    )

    broader = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="narrower",
        help_text=(
            "A wider tag this rolls up into. A case tagged land-grab is also a land "
            "case, so the indexer writes both. One level only."
        ),
    )
    merged_into = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="merged_from",
        help_text="Replacement tag. Required when status is merged (§12).",
    )

    sort_order = models.IntegerField(
        default=0,
        help_text=(
            "§12 'approved priority' — the tie-break above alphabetical. Explicit "
            "because sorting Devanagari labels alphabetically needs a locale-aware "
            "collation that Postgres and Python do not agree on by default."
        ),
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("sort_order", "id")
        constraints = [
            models.CheckConstraint(
                # A merged tag with nowhere to point strands every case carrying it.
                condition=~models.Q(status=TagStatus.MERGED)
                | models.Q(merged_into__isnull=False),
                name="case_tags_merged_needs_target",
            ),
            models.CheckConstraint(
                condition=~models.Q(broader=models.F("id")),
                name="case_tags_broader_not_self",
            ),
            models.CheckConstraint(
                condition=~models.Q(merged_into=models.F("id")),
                name="case_tags_merged_into_not_self",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.id} ({self.label_ne})"

    def clean(self) -> None:
        """Reject the shapes the DB constraints cannot express.

        The one-level rule is the important one. Roll-up is applied at index time by
        walking ``broader``; allowing a chain would mean an unbounded walk per
        document and a facet where selecting a tag silently pulls in grandparents
        nobody chose.
        """
        super().clean()
        if self.broader_id is not None:
            if self.broader_id == self.id:
                raise ValidationError({"broader": "A tag cannot be broader than itself."})
            parent = Tag.objects.filter(pk=self.broader_id).only("broader_id").first()
            if parent is not None and parent.broader_id is not None:
                raise ValidationError(
                    {
                        "broader": (
                            f"{self.broader_id!r} already rolls up into "
                            f"{parent.broader_id!r}; broader is one level only."
                        )
                    }
                )
        if self.status == TagStatus.MERGED and self.merged_into_id is None:
            raise ValidationError(
                {"merged_into": "A merged tag must name its replacement."}
            )
        if self.merged_into_id is not None and self.merged_into_id == self.id:
            raise ValidationError({"merged_into": "A tag cannot merge into itself."})

    def with_broader(self) -> list[str]:
        """This tag id plus every tag it rolls up into, nearest first.

        What the indexer writes, so that filtering on ``land`` matches a case tagged
        only ``land-grab`` with a plain term query and no query-time rewriting.
        """
        chain: list[str] = [self.id]
        parent_id = self.broader_id
        while parent_id is not None and parent_id not in chain:
            chain.append(parent_id)
            parent_id = (
                Tag.objects.filter(pk=parent_id)
                .values_list("broader_id", flat=True)
                .first()
            )
        return chain


class TagAlias(models.Model):
    """One raw string -> one tag, or an explicit record that we retired it.

    ``tag`` is nullable on purpose. Three outcomes have to be distinguishable:
    resolves to a tag, was deliberately dropped, or was never seen. Collapsing the
    middle case into "unknown" turns every stale bookmark into what looks like a bug.
    """

    key = models.CharField(
        max_length=200,
        unique=True,
        help_text="ALREADY NORMALIZED — the output of normalize(), not the raw value.",
    )
    tag = models.ForeignKey(
        Tag,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="aliases",
        help_text="Null when this value was retired rather than mapped.",
    )
    retired_reason = models.CharField(
        max_length=64,
        blank=True,
        help_text="Why it was dropped, e.g. not-a-tag-money-amount. Set iff tag is null.",
    )
    source = models.CharField(
        max_length=200, blank=True, help_text="The raw value this key came from."
    )

    class Meta:
        verbose_name_plural = "tag aliases"
        constraints = [
            models.CheckConstraint(
                # Exactly one of the two — an alias that neither resolves nor
                # explains itself is a silent hole in the vocabulary.
                condition=models.Q(tag__isnull=False, retired_reason="")
                | models.Q(tag__isnull=True) & ~models.Q(retired_reason=""),
                name="case_tags_alias_maps_or_explains",
            )
        ]

    def __str__(self) -> str:
        return f"{self.key} -> {self.tag_id or self.retired_reason}"

    def clean(self) -> None:
        super().clean()
        if self.key != normalize(self.key):
            raise ValidationError(
                {"key": f"Alias keys are stored normalized; expected {normalize(self.key)!r}."}
            )


def resolve(value: str) -> ResolvedTag:
    """Resolve one raw tag value against the vocabulary.

    Follows ``merged_into`` so a merged tag's old id keeps working, which is the
    point of recording the merge rather than deleting the row.
    """
    alias = (
        TagAlias.objects.filter(key=normalize(value))
        .select_related("tag")
        .only("tag_id", "retired_reason", "tag__status", "tag__merged_into_id")
        .first()
    )
    if alias is None:
        return ResolvedTag(Resolution.UNKNOWN)
    if alias.tag is None:
        return ResolvedTag(Resolution.RETIRED, reason=alias.retired_reason)

    tag = alias.tag
    seen: set[str] = set()
    while tag.status == TagStatus.MERGED and tag.merged_into_id is not None:
        if tag.merged_into_id in seen:
            break
        seen.add(tag.merged_into_id)
        next_tag = Tag.objects.filter(pk=tag.merged_into_id).first()
        if next_tag is None:
            break
        tag = next_tag
    return ResolvedTag(Resolution.CANONICAL, tag_id=tag.id)
