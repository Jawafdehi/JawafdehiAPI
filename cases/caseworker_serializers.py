"""
Serializers used exclusively by the caseworker PATCH endpoint.

CasePatchSerializer validates the post-patch result dict (not the patch document
itself) before the changes are persisted.
"""

import re
from datetime import datetime

from rest_framework import serializers

from jawafdehi_shared.entities.ids import (
    is_valid_entity_iri,
    is_valid_material_iri,
)

from .models import (
    CaseState,
    CaseType,
    RelationshipType,
)
from .validators import validate_court_cases, validate_slug

# Paths that callers are not permitted to target in a patch operation.
# The view rejects any op whose `path` equals or is prefixed by one of these.
# Note: /slug is conditionally blocked based on case state (see api_views.py)
BLOCKED_PATH_PREFIXES = frozenset(
    [
        "/id",
        "/case_type",
        "/version",
        "/contributors",
        "/created_at",
        "/updated_at",
        "/versionInfo",
    ]
)


class TimelineItemSerializer(serializers.Serializer):
    # Bikram Sambat dates are not Gregorian-parseable, so they are validated by
    # shape only (mirrors cases.fields.TimelineListField._BS_DATE_RE).
    _BS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    date = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True)
    date_bs = serializers.CharField(required=False)
    end_date = serializers.CharField(required=False)
    end_date_bs = serializers.CharField(required=False)

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

    def validate_end_date(self, value):
        try:
            datetime.fromisoformat(value)
        except (ValueError, TypeError):
            raise serializers.ValidationError(
                "Invalid end_date format (expected ISO format YYYY-MM-DD)"
            )
        return value

    def validate_date_bs(self, value):
        return self._validate_bs("date_bs", value)

    def validate_end_date_bs(self, value):
        return self._validate_bs("end_date_bs", value)

    def _validate_bs(self, field_name, value):
        if not self._BS_DATE_RE.match(value):
            raise serializers.ValidationError(
                f"{field_name} must be a Bikram Sambat date string in YYYY-MM-DD "
                "format"
            )
        return value

    def validate(self, attrs):
        end_date = attrs.get("end_date")
        if end_date is not None:
            # date already validated to ISO format by validate_date
            if datetime.fromisoformat(end_date) < datetime.fromisoformat(attrs["date"]):
                raise serializers.ValidationError(
                    {"end_date": "end_date must be on or after date"}
                )
        return attrs


class EvidenceItemSerializer(serializers.Serializer):
    """One evidence entry = a reference to an NGM material (the
    CaseMaterialReference join). ``material_iri`` is required + strict-validated;
    ``additional_details`` is an optional case-specific note (ADR: cases own no
    documents).
    """

    material_iri = serializers.CharField()
    additional_details = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default=""
    )

    def validate_material_iri(self, value):
        value = (value or "").strip()
        if not is_valid_material_iri(value):
            raise serializers.ValidationError(
                f"Invalid NGM material id: {value!r}. Must be a canonical material "
                "@id IRI of the form "
                "'https://<authority>/material/<source>/<ident>'."
            )
        return value

    def validate_additional_details(self, value):
        # Optional note; normalize null to empty string.
        return (value or "").strip()


class EntityPatchItemSerializer(serializers.Serializer):
    # The bind holds the canonical NES entity id directly; entities are owned by
    # NES and must already exist there (no display-name fallback).
    nes_id = serializers.CharField()
    relationship_type = serializers.ChoiceField(choices=RelationshipType.choices)
    notes = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default=""
    )

    def validate_nes_id(self, value):
        value = (value or "").strip()
        if not is_valid_entity_iri(value):
            raise serializers.ValidationError(
                f"Invalid NES entity id: {value!r}. Must be a canonical entity "
                "@id IRI of the form "
                "'https://<authority>/entity/<prefix>/<slug>'."
            )
        return value


class CaseEntityValidationMixin:
    def validate_alleged_entities(self, value):
        return self._validate_entity_ids(value)

    def validate_related_entities(self, value):
        return self._validate_entity_ids(value)

    def _validate_entity_ids(self, ids):
        if not ids:
            return ids
        cleaned = []
        invalid = []
        for nid in ids:
            nid = (nid or "").strip()
            if is_valid_entity_iri(nid):
                cleaned.append(nid)
            else:
                invalid.append(nid)
        if invalid:
            raise serializers.ValidationError(
                f"Invalid NES entity ids: {sorted(invalid)}"
            )
        return cleaned


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
        child=serializers.CharField(), required=False
    )
    related_entities = serializers.ListField(
        child=serializers.CharField(), required=False
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
