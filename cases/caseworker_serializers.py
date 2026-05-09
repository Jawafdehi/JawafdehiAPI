"""
Serializers used exclusively by the caseworker PATCH endpoint.

CasePatchSerializer validates the post-patch result dict (not the patch document
itself) before the changes are persisted.
"""

from datetime import datetime

from rest_framework import serializers

from .models import (
    CaseState,
    CaseType,
    JawafEntity,
    RelationshipType,
)
from .validators import validate_slug, validate_court_cases

# Paths that callers are not permitted to target in a patch operation.
# The view rejects any op whose `path` equals or is prefixed by one of these.
# Note: /slug is conditionally blocked based on case state (see api_views.py)
BLOCKED_PATH_PREFIXES = frozenset(
    [
        "/id",
        "/case_id",
        "/case_type",
        "/version",
        "/contributors",
        "/created_at",
        "/updated_at",
        "/versionInfo",
    ]
)


class TimelineItemSerializer(serializers.Serializer):
    date = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True)

    def validate_date(self, value):
        try:
            datetime.fromisoformat(value)
        except (ValueError, TypeError):
            raise serializers.ValidationError(
                "Invalid date format (expected ISO format YYYY-MM-DD)"
            )
        return value

    def validate_title(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Title must be a non-empty string")
        return value


class EvidenceItemSerializer(serializers.Serializer):
    source_id = serializers.CharField()
    description = serializers.CharField()

    def validate_source_id(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("source_id must be a non-empty string")
        return value

    def validate_description(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("description must be a non-empty string")
        return value


class EntityPatchItemSerializer(serializers.Serializer):
    entity = serializers.IntegerField()
    relationship_type = serializers.ChoiceField(choices=RelationshipType.choices)
    notes = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default=""
    )

    def validate_entity(self, value):
        if not JawafEntity.objects.filter(id=value).exists():
            raise serializers.ValidationError(f"Entity ID not found: {value}")
        return value


class CaseEntityValidationMixin:
    def validate_alleged_entities(self, value):
        return self._validate_entity_ids(value)

    def validate_related_entities(self, value):
        return self._validate_entity_ids(value)

    def _validate_entity_ids(self, ids):
        if not ids:
            return ids
        existing = set(
            JawafEntity.objects.filter(id__in=ids).values_list("id", flat=True)
        )
        missing = set(ids) - existing
        if missing:
            raise serializers.ValidationError(
                f"Entity IDs not found: {sorted(missing)}"
            )
        return ids


class CaseCreateSerializer(CaseEntityValidationMixin, serializers.Serializer):
    case_type = serializers.ChoiceField(choices=CaseType.choices)
    state = serializers.ChoiceField(
        choices=CaseState.choices,
        required=False,
        default=CaseState.DRAFT,
    )
    title = serializers.CharField(max_length=200)
    short_description = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    thumbnail_url = serializers.URLField(
        required=False, allow_blank=True, max_length=500
    )
    banner_url = serializers.URLField(required=False, allow_blank=True, max_length=500)
    case_start_date = serializers.DateField(required=False, allow_null=True)
    case_end_date = serializers.DateField(required=False, allow_null=True)
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    key_allegations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    timeline = TimelineItemSerializer(many=True, required=False)
    evidence = EvidenceItemSerializer(many=True, required=False)
    notes = serializers.CharField(required=False, allow_blank=True)
    alleged_entities = serializers.ListField(
        child=serializers.IntegerField(), required=False
    )
    related_entities = serializers.ListField(
        child=serializers.IntegerField(), required=False
    )
    slug = serializers.SlugField(
        max_length=50,
        required=False,
        allow_null=True,
        validators=[validate_slug],
    )
    court_cases = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
        validators=[validate_court_cases],
    )
    missing_details = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    bigo = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=-9223372036854775808,
        max_value=9223372036854775807,
    )

    def validate_missing_details(self, value):
        """Normalize empty/whitespace missing_details to None."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    def validate_slug(self, value):
        """Normalize empty/whitespace slugs to None."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value


class CasePatchSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=CaseState.choices, required=False)
    title = serializers.CharField(max_length=200)
    short_description = serializers.CharField(required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    thumbnail_url = serializers.URLField(
        required=False, allow_blank=True, max_length=500
    )
    banner_url = serializers.URLField(required=False, allow_blank=True, max_length=500)
    case_start_date = serializers.DateField(required=False, allow_null=True)
    case_end_date = serializers.DateField(required=False, allow_null=True)
    case_type = serializers.ChoiceField(choices=CaseType.choices)
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    key_allegations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    timeline = TimelineItemSerializer(many=True, required=False)
    evidence = EvidenceItemSerializer(many=True, required=False)
    entities = EntityPatchItemSerializer(many=True, required=False)
    slug = serializers.SlugField(
        max_length=50,
        required=False,
        allow_null=True,
        validators=[validate_slug],
    )
    court_cases = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
        validators=[validate_court_cases],
    )
    missing_details = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    bigo = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=-9223372036854775808,
        max_value=9223372036854775807,
    )

    def validate_missing_details(self, value):
        """Normalize empty/whitespace missing_details to None."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    def validate_slug(self, value):
        """
        Validate slug for PATCH operations.

        Normalize empty/whitespace slugs to None.
        Note: Slug immutability is enforced at the view layer via BLOCKED_PATH_PREFIXES.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value
