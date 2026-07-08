"""
Serializers for the Jawafdehi accountability platform API.

See: .kiro/specs/accountability-platform-core/design.md
"""

import logging

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from .models import (
    Case,
    CaseEntityRelationship,
    Feedback,
)

logger = logging.getLogger(__name__)


class CaseEntityRelationshipSerializer(serializers.ModelSerializer):
    """
    Serializer for the CaseEntityRelationship bind.

    The bind holds the canonical NES entity @id IRI (``nes_id``,
    ``https://jawafdehi.org/entity/<prefix>/<slug>``) directly; entity
    display details are resolved from NES out-of-band (see
    ``cases.services.nes_resolver``) and are not part of this serializer.
    """

    class Meta:
        model = CaseEntityRelationship
        fields = [
            "id",
            "nes_id",
            "relationship_type",
            "outcome",
            "notes",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_relationship_type(self, value):
        """
        Validate that relationship_type is one of the allowed choices.
        """
        from .models import RelationshipType

        valid_types = [choice[0] for choice in RelationshipType.choices]
        if value not in valid_types:
            raise serializers.ValidationError(
                f"Invalid relationship type '{value}'. Must be one of: {', '.join(valid_types)}"
            )
        return value


class CaseSerializer(serializers.ModelSerializer):
    """
    Serializer for Case model.

    Exposes all fields except contributors (internal only).

    The state field is always included to indicate case status (PUBLISHED or IN_REVIEW).

    Uses the unified entities list for all related entities.

    SCHEMA FIX: Removed legacy alleged_entities and related_entities fields to eliminate
    schema discrepancy. The API now returns only the unified format as documented.
    """

    entities = serializers.SerializerMethodField(
        help_text="Entity binds for this case (NES entity id, relationship type, "
        "notes), with display details resolved from NES"
    )

    @extend_schema_field(
        inline_serializer(
            name="CaseEntity",
            many=True,
            fields={
                "nes_id": serializers.CharField(),
                "display_name": serializers.CharField(allow_null=True),
                "entity_type": serializers.CharField(allow_null=True),
                "type": serializers.CharField(),
                "outcome": serializers.CharField(),
                "notes": serializers.CharField(allow_blank=True),
            },
        )
    )
    def get_entities(self, obj):
        """Get the case's entity binds, resolving display details from NES.

        Each entry is ``{nes_id, display_name, entity_type, type, notes}`` where
        ``type`` is the relationship type. ``display_name``/``entity_type`` come
        from the NES resolver (``None`` when NES can't resolve the id).
        """
        from cases.services.nes_resolver import resolve_entities

        try:
            relationships = list(obj.entity_relationships.all())
            resolved = resolve_entities(rel.nes_id for rel in relationships)
            return [
                {
                    "nes_id": rel.nes_id,
                    "display_name": resolved[rel.nes_id]["display_name"],
                    "entity_type": resolved[rel.nes_id]["entity_type"],
                    "type": rel.relationship_type,
                    "outcome": rel.outcome,
                    "notes": rel.notes,
                }
                for rel in relationships
            ]
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(
                f"Error serializing entities for case {obj.slug}: {e}",
                exc_info=True,
                extra={"slug": obj.slug},
            )
            raise

    @extend_schema_field(
        inline_serializer(
            name="CaseEvidence",
            many=True,
            fields={
                "material_iri": serializers.CharField(),
                "additional_details": serializers.CharField(allow_blank=True),
            },
        )
    )
    def get_evidence(self, obj):
        """Evidence as material references (the CaseMaterialReference join).

        Each entry is ``{material_iri, additional_details}`` in display order.
        ``CaseDetailSerializer`` additionally enriches each entry with a resolved
        ``material`` object (title/type/links) from NGM.
        """
        return [
            {
                "material_iri": ref.material_iri,
                "additional_details": ref.additional_details,
            }
            for ref in obj.material_references.all()
        ]

    court_cases = serializers.ListField(
        child=serializers.CharField(),
        allow_null=True,
        required=False,
        help_text=(
            "List of canonical court-case @id IRIs "
            "(https://jawafdehi.org/courtcase/<court>/<case_number>), from the "
            "CaseCourtCaseReference join"
        ),
    )
    tags = serializers.ListField(
        child=serializers.CharField(),
        help_text="List of tags for categorization (e.g., 'land-encroachment', 'national-interest')",
        required=False,
    )
    key_allegations = serializers.ListField(
        child=serializers.CharField(),
        help_text="List of key allegation statements",
        required=False,
    )
    timeline = serializers.ListField(
        child=serializers.DictField(),
        help_text="List of timeline entries with date, title, and description",
        required=False,
    )
    evidence = serializers.SerializerMethodField(
        help_text="Evidence: material references (material_iri + additional_details)",
    )
    versionInfo = serializers.JSONField(
        help_text="Version metadata tracking changes (version_number, user_id, change_summary, datetime)",
        required=False,
    )
    public_iri = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Canonical public case @id IRI "
        "(https://jawafdehi.org/case/<slug>), minted at publish: present only "
        "when the case is PUBLISHED, otherwise null.",
    )

    class Meta:
        model = Case
        fields = [
            "id",
            "slug",
            "public_iri",
            "case_type",
            "state",
            "title",
            "short_description",
            "thumbnail_url",
            "banner_url",
            "case_start_date",
            "case_end_date",
            "case_start_date_bs",
            "case_end_date_bs",
            "entities",
            "tags",
            "description",
            "key_allegations",
            "timeline",
            "evidence",
            "notes",
            "court_cases",
            "missing_details",
            "bigo",
            "versionInfo",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields  # API is read-only


class CaseDetailSerializer(CaseSerializer):
    """
    Serializer for Case detail view.

    Extends CaseSerializer by enriching each evidence entry (a
    CaseMaterialReference) with a nested `material` object containing the
    resolved title, material_type, and roled links from NGM. When the referenced
    material does not exist or has been soft-deleted, `material` carries a stub
    (display_name/material_type null, empty urls) so the response stays stable.
    """

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_evidence(self, obj):
        """Evidence entries enriched with resolved NGM material details.

        Each entry is ``{material_iri, additional_details, material}`` where
        ``material`` is ``{display_name, material_type, urls: [{link, role}]}``
        from ``resolve_materials`` (a stub when the material can't be resolved).
        """
        from cases.services.material_resolver import resolve_materials

        refs = list(obj.material_references.all())
        if not refs:
            return []
        resolved = resolve_materials(ref.material_iri for ref in refs)

        def _material(iri):
            # resolve_materials is total over TRUTHY ids; a blank/None material_iri
            # (only reachable via a non-API write) would KeyError, so fall back to
            # a stub rather than 500 the whole case detail.
            rec = resolved.get(iri)
            if rec is None:
                return {"display_name": None, "material_type": None, "urls": []}
            return {
                "display_name": rec["display_name"],
                "material_type": rec["material_type"],
                "urls": rec["urls"],
            }

        return [
            {
                "material_iri": ref.material_iri,
                "additional_details": ref.additional_details,
                "material": _material(ref.material_iri),
            }
            for ref in refs
        ]

    class Meta(CaseSerializer.Meta):
        pass


class ContactMethodSerializer(serializers.Serializer):
    """Serializer for contact method within feedback."""

    type = serializers.ChoiceField(
        choices=["email", "phone", "whatsapp", "instagram", "facebook", "other"],
        help_text="Type of contact method",
    )
    value = serializers.CharField(
        max_length=300, help_text="Contact value (email, phone, username, etc.)"
    )


class ContactInfoSerializer(serializers.Serializer):
    """Serializer for contact information within feedback."""

    name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, help_text="Submitter's name"
    )
    contactMethods = ContactMethodSerializer(
        many=True, required=False, help_text="List of contact methods"
    )


class FeedbackSerializer(serializers.ModelSerializer):
    """Serializer for Feedback model."""

    ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

    feedbackType = serializers.CharField(
        source="feedback_type", help_text="Type of feedback"
    )
    relatedPage = serializers.CharField(
        source="related_page",
        required=False,
        allow_blank=True,
        help_text="Page or feature related to feedback",
    )
    contactInfo = ContactInfoSerializer(
        source="contact_info", required=False, help_text="Optional contact information"
    )
    submittedAt = serializers.DateTimeField(
        source="submitted_at",
        read_only=True,
        help_text="Timestamp when feedback was submitted",
    )
    attachment = serializers.FileField(
        required=False,
        allow_null=True,
        help_text="Optional file attachment (max 10 MB)",
    )

    class Meta:
        model = Feedback
        fields = [
            "id",
            "feedbackType",
            "subject",
            "description",
            "relatedPage",
            "contactInfo",
            "attachment",
            "status",
            "submittedAt",
        ]
        read_only_fields = ["id", "status", "submittedAt"]

    def validate_feedbackType(self, value):
        """Validate feedback type."""
        from .models import FeedbackType

        valid_types = [choice[0] for choice in FeedbackType.choices]
        if value not in valid_types:
            raise serializers.ValidationError(
                f"Invalid feedback type. Must be one of: {', '.join(valid_types)}"
            )
        return value

    def validate_attachment(self, value):
        """Validate attachment file size (max 10 MB)."""
        if value is None:
            return value
        if value.size > self.ATTACHMENT_MAX_BYTES:
            raise serializers.ValidationError(
                f"File size must not exceed 10 MB. Received: {value.size / (1024 * 1024):.1f} MB."
            )
        return value

    def validate_contactInfo(self, value):
        """Validate contact info structure."""
        if not value:
            return {}

        # Validate contact methods if present
        if "contactMethods" in value:
            valid_types = [
                "email",
                "phone",
                "whatsapp",
                "instagram",
                "facebook",
                "other",
            ]
            for method in value["contactMethods"]:
                if method.get("type") not in valid_types:
                    raise serializers.ValidationError(
                        f"Invalid contact method type. Must be one of: {', '.join(valid_types)}"
                    )

        return value

    def to_representation(self, instance):
        """Convert to camelCase response format."""
        data = super().to_representation(instance)

        # Return simplified response for API
        return {
            "id": data["id"],
            "feedbackType": data["feedbackType"],
            "subject": data["subject"],
            "status": data["status"],
            "submittedAt": data["submittedAt"],
            "message": "Thank you for your feedback! We will review it and get back to you if needed.",
        }


