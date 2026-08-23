"""T25 — perform the vocabulary write an approved :class:`TagProposal` describes.

Mirrors :mod:`case_proposals.apply`: nothing here runs until a human approves, and every
failure is a ``400`` naming what was wrong rather than a partial write.

The safety property this file exists to hold is that **an automation can propose anything
and change nothing**. The only paths that create a :class:`~case_tags.models.TagAlias` or
activate a :class:`~case_tags.models.Tag` are here, behind an approval, behind
``IsContentStaff`` — which excludes the machine role, so the proposer cannot approve its
own work.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from jawafdehi_shared.tags.normalize import normalize_tag

from case_tags.models import (
    AliasSource,
    AxisMembers,
    ProposalKind,
    Tag,
    TagAlias,
    TagAxis,
    TagStatus,
)


def _require(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value in (None, "", []):
        raise ValidationError({key: "Required."})
    return value


def _apply_alias_equivalence(payload: dict[str, Any], reviewer: str) -> TagAlias:
    """Record that a raw string means an existing term.

    Stores the NORMALIZED form, because that is what the resolver looks up. Storing the
    raw form would make the alias unreachable for every casing variant but the one that
    happened to be proposed — which is the ``Ncell``/``ncell`` defect, recreated inside
    the fix for it.
    """
    raw = _require(payload, "raw_value")
    tag_id = _require(payload, "proposed_tag_id")

    value = normalize_tag(raw)
    if not value:
        raise ValidationError({"raw_value": "Normalizes to empty; nothing to alias."})

    try:
        tag = Tag.objects.get(pk=tag_id)
    except Tag.DoesNotExist:
        raise ValidationError({"proposed_tag_id": f"No such tag: {tag_id!r}."}) from None

    if tag.status == TagStatus.MERGED:
        # Pointing a new alias at a retired term would work (the resolver follows
        # merges) but it bakes in an indirection nobody asked for. Send it to the
        # replacement instead, so the table says what it means.
        raise ValidationError(
            {
                "proposed_tag_id": (
                    f"{tag_id!r} is merged into {tag.merged_into_id!r}; "
                    "alias the replacement instead."
                )
            }
        )

    existing = TagAlias.objects.filter(value=value).first()
    if existing is not None:
        if existing.tag_id == tag.id:
            # Idempotent: approving twice must not 500 on a unique violation.
            return existing
        raise ValidationError(
            {
                "raw_value": (
                    f"{value!r} already resolves to {existing.tag_id!r}. "
                    "Re-point or remove that alias first."
                )
            }
        )

    return TagAlias.objects.create(
        value=value,
        tag=tag,
        source=AliasSource.LLM,
        approved_by=reviewer,
        approved_at=timezone.now(),
    )


def _apply_new_term(payload: dict[str, Any], reviewer: str) -> Tag:
    """Create a term, active, on an enumerated axis.

    Guards worth their weight:

    * the axis must exist AND be ``enumerated`` — adding a term to ``geography`` (a fixed
      official list) or ``person`` (the entities relation) means somebody has
      misunderstood where those values come from, and silently accepting it would put a
      hand-typed district beside the official ones;
    * the slug must be a clean ASCII slug already (policy §7.1 rule 1). This function
      will not mint one: transliterating ``महालेखा परीक्षक`` is not deterministic, so two
      approvals would produce two slugs for one concept.
    """
    axis_id = _require(payload, "axis")
    slug = _require(payload, "proposed_slug")
    label_en = _require(payload, "label_en")

    try:
        axis = TagAxis.objects.get(pk=axis_id)
    except TagAxis.DoesNotExist:
        raise ValidationError({"axis": f"No such axis: {axis_id!r}."}) from None

    if axis.members != AxisMembers.ENUMERATED:
        raise ValidationError(
            {
                "axis": (
                    f"Axis {axis_id!r} takes its values from {axis.members!r}, "
                    "not from this vocabulary."
                )
            }
        )

    if slug != normalize_tag(slug) or " " in slug:
        raise ValidationError(
            {"proposed_slug": f"Must be a normalized lowercase-kebab ASCII slug: {slug!r}."}
        )

    existing = Tag.objects.filter(pk=slug).first()
    if existing is not None:
        # Idempotent on re-approval; a genuine collision with a different term is a
        # conflict a human has to look at.
        if existing.axis_id == axis.id and existing.label_en == label_en:
            return existing
        raise ValidationError({"proposed_slug": f"Tag {slug!r} already exists."})

    return Tag.objects.create(
        id=slug,
        axis=axis,
        label_ne=payload.get("label_ne") or None,
        label_en=label_en,
        status=TagStatus.ACTIVE,
        note=f"Approved from proposal by {reviewer}.",
    )


def apply_proposal(kind: str, payload: dict[str, Any], reviewer: str) -> Tag | TagAlias:
    """Dispatch on ``kind``. Raises ``ValidationError`` (→ 400) on any problem."""
    if not isinstance(payload, dict):
        raise ValidationError({"payload": "Must be an object."})
    if kind == ProposalKind.ALIAS_EQUIVALENCE:
        return _apply_alias_equivalence(payload, reviewer)
    if kind == ProposalKind.NEW_TERM:
        return _apply_new_term(payload, reviewer)
    raise ValidationError({"kind": f"Unsupported proposal kind: {kind!r}."})
