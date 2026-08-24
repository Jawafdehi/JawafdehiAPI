"""Serializers for the vocabulary read endpoint."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from case_tags.models import Tag, TagAxis, TagStatus


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
