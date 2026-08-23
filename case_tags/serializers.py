"""Serializers for the vocabulary read endpoint (T15) and the review queue (T25)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from case_tags.models import ProposalKind, Tag, TagAxis, TagProposal, TagStatus


class TagSerializer(serializers.ModelSerializer):
    """One term. ``case_count`` is injected by the view, not computed per-instance."""

    case_count = serializers.SerializerMethodField()

    class Meta:
        model = Tag
        fields = [
            "id",
            "axis",
            "label_ne",
            "label_ne_composed",
            "label_en",
            "status",
            "merged_into",
            "case_count",
            "note",
        ]

    def get_case_count(self, obj: Tag) -> int:
        # The view computes every count in one pass and passes the map through context;
        # doing it here per term would be a query per row.
        return self.context.get("case_counts", {}).get(obj.id, 0)


class TagAxisSerializer(serializers.ModelSerializer):
    terms = serializers.SerializerMethodField()

    class Meta:
        model = TagAxis
        fields = [
            "id",
            "label_ne",
            "label_en",
            "min_per_case",
            "max_per_case",
            "highlighted",
            "members",
            "set_by",
            "note",
            "terms",
        ]

    # Return type is `Any` rather than `list[dict]`: DRF hands back a `ReturnList`,
    # which is list-like but not a `list`, and narrowing here would be a lie the type
    # checker correctly rejects.
    def get_terms(self, obj: TagAxis) -> Any:
        terms = self.context.get("terms_by_axis", {}).get(obj.id, [])
        return TagSerializer(terms, many=True, context=self.context).data


class TagProposalSerializer(serializers.ModelSerializer):
    """Read + create. ``status`` and the review fields are never client-settable.

    A client that could POST ``status: approved`` would bypass the entire review gate,
    so they are read-only here and only :mod:`case_tags.views` moves them.
    """

    class Meta:
        model = TagProposal
        fields = [
            "id",
            "kind",
            "payload",
            "confidence",
            "status",
            "detected_by",
            "dedup_key",
            "reviewer",
            "reviewed_at",
            "review_notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["status", "reviewer", "reviewed_at", "review_notes"]

    def validate_confidence(self, value: float) -> float:
        # Also a DB CheckConstraint. Both, because the constraint gives a 500-shaped
        # IntegrityError and this gives a 400 that says which field.
        if not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Must be between 0 and 1.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Shape-check the payload against its ``kind`` at CREATE time.

        Deep validation happens again in ``apply`` on approve — but catching a malformed
        payload here means a reviewer never sees a row that cannot be approved, which is
        the difference between a queue of decisions and a queue of decisions plus
        litter.
        """
        kind = attrs.get("kind") or getattr(self.instance, "kind", None)
        payload = attrs.get("payload")
        if payload is None:
            return attrs
        if not isinstance(payload, dict):
            raise serializers.ValidationError({"payload": "Must be an object."})

        required = {
            ProposalKind.ALIAS_EQUIVALENCE: ("raw_value", "proposed_tag_id"),
            ProposalKind.NEW_TERM: ("axis", "proposed_slug", "label_en", "rationale"),
        }.get(kind, ())
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise serializers.ValidationError(
                {"payload": f"Missing for kind {kind!r}: {', '.join(missing)}."}
            )

        # A new_term proposal without evidence is not reviewable — a reviewer cannot
        # judge "does this recur" or "is it real" from a bare slug. policy §12 wants
        # three supporting cases; the quoted span is what makes each one checkable.
        if kind == ProposalKind.NEW_TERM and not payload.get("quoted_span"):
            raise serializers.ValidationError(
                {"payload": "new_term requires a quoted_span from the case text."}
            )
        return attrs


class TagProposalDecisionSerializer(serializers.Serializer):
    """Optional reviewer note attached to an approve/reject."""

    notes = serializers.CharField(required=False, allow_blank=True, default="")


class TagProposalPayloadEditSerializer(serializers.Serializer):
    """Correct a pending proposal's payload before approving it.

    The common case is an alias proposed against a plausible-but-wrong term. Without
    this the reviewer's only options are approve-the-wrong-thing or reject-and-lose-it,
    and the second means the value silently stays unresolved.
    """

    payload = serializers.JSONField()

    def validate_payload(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be an object.")
        return value


def active_terms_by_axis(include_all: bool = False) -> dict[str, list[Tag]]:
    """Terms grouped by axis id, one query.

    Excludes ``proposed`` and ``merged`` by default: a proposed term is not yet a legal
    choice, and a merged one must not be offered as a new one — but both stay reachable
    via ``include_all`` so a reviewer or a migration can see the whole table.
    """
    qs = Tag.objects.select_related("axis").order_by("axis__sort_order", "id")
    if not include_all:
        qs = qs.filter(status__in=[TagStatus.ACTIVE, TagStatus.DEPRECATED])
    grouped: dict[str, list[Tag]] = {}
    for tag in qs:
        grouped.setdefault(tag.axis_id, []).append(tag)
    return grouped
