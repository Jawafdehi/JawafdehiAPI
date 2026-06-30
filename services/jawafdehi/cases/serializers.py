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
    DocumentSource,
    Feedback,
    SourceLinkRole,
    validate_upload_file_extension,
    validate_upload_file_mimetype,
    validate_upload_file_size,
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

    court_cases = serializers.ListField(
        child=serializers.CharField(),
        allow_null=True,
        required=False,
        help_text="List of court case references in format <court_identifier>:<case_number>",
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
    evidence = serializers.ListField(
        child=serializers.DictField(),
        help_text="List of evidence entries with source_id and description",
        required=False,
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

    Extends CaseSerializer by enriching each evidence entry with a nested
    `source` object containing title, source_type, and url from the linked
    DocumentSource. When the referenced source does not exist or has been
    soft-deleted, `source` is null so the response remains stable.
    """

    evidence = serializers.SerializerMethodField(
        help_text="List of evidence entries enriched with source details"
    )

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_evidence(self, obj):
        """Return evidence entries enriched with data from the linked DocumentSource."""
        raw_evidence = obj.evidence or []
        if not raw_evidence:
            return []

        def resolve_source_id(entry):
            """Extract a string source_id from an entry, handling embedded dicts."""
            sid = entry.get("source_id")
            if isinstance(sid, dict):
                return sid.get("source_id") or sid.get("link")
            return sid

        source_ids = [sid for e in raw_evidence if (sid := resolve_source_id(e))]
        sources = {
            s.source_id: DocumentSourceSerializer(s, context=self.context).data
            for s in DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            )
        }

        return [
            entry
            | {
                "source": (
                    {
                        k: sources[sid][k]
                        for k in ["title", "source_type", "url", "urls"]
                    }
                    if (sid := resolve_source_id(entry)) in sources
                    else None
                )
            }
            for entry in raw_evidence
        ]

    class Meta(CaseSerializer.Meta):
        pass


class SourceLinkField(serializers.Field):
    """Field that accepts a ``{'link': str, 'role': str}`` source-link dict.

    Plain URL strings are no longer accepted — every link must be a dict with a
    ``link`` key and an explicit ``role`` (one of the ``SourceLinkRole`` values).
    File uploads are recorded as ``RAW`` automatically by the view; this field
    is only used for caller-supplied external URLs, which must name their role.
    """

    def to_internal_value(self, data):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.core.validators import URLValidator

        validator = URLValidator()
        valid_roles = [r.value for r in SourceLinkRole]

        if not isinstance(data, dict):
            raise serializers.ValidationError(
                "Must be a dict with 'link' and 'role' keys; "
                "plain URL strings are no longer accepted."
            )

        link = data.get("link")
        role = data.get("role")
        if not link or not isinstance(link, str) or not link.strip():
            raise serializers.ValidationError(
                "Dict must contain a 'link' key with a non-empty string value."
            )
        stripped_link = link.strip()
        try:
            validator(stripped_link)
        except DjangoValidationError:
            raise serializers.ValidationError("Enter a valid URL.")

        if role is None:
            raise serializers.ValidationError(
                f"A 'role' is required. Must be one of {valid_roles}."
            )
        if role not in valid_roles:
            raise serializers.ValidationError(
                f"Invalid role '{role}'. Must be one of {valid_roles}."
            )
        return {"link": stripped_link, "role": role}

    def to_representation(self, value):
        if isinstance(value, str):
            return {"link": value, "role": SourceLinkRole.RAW.value}
        if isinstance(value, dict):
            return {
                "link": value.get("link"),
                "role": value.get("role") or SourceLinkRole.RAW.value,
            }
        return value


class DocumentSourceSerializer(serializers.ModelSerializer):
    """
    Serializer for DocumentSource model.

    Used for public API access to sources associated with published cases.
    """

    url = serializers.SerializerMethodField(
        help_text="Deprecated — use 'urls'. List of URL strings for this source, "
        "including uploaded file URL when available"
    )
    urls = serializers.SerializerMethodField(
        help_text="List of URL dicts with 'link' and 'role' keys for this source, "
        "including uploaded file URL when available"
    )

    @extend_schema_field(serializers.ListField(child=serializers.URLField()))
    def get_url(self, obj):
        """Backward-compat: return only link strings (deprecated).

        Deduplicated by link — the same link under two roles (e.g. RAW + the
        MARKDOWN-converted view) collapses to a single string here.
        """
        seen = set()
        links = []
        for u in self.get_urls(obj):
            link = u["link"]
            if link not in seen:
                seen.add(link)
                links.append(link)
        return links

    @extend_schema_field(
        inline_serializer(
            many=True,
            name="SourceLink",
            fields={
                "link": serializers.URLField(),
                "role": serializers.ChoiceField(
                    choices=[r.value for r in SourceLinkRole]
                ),
            },
        )
    )
    def get_urls(self, obj):
        request = self.context.get("request")
        merged_urls = []
        seen = set()

        def add_url(value, role="RAW"):
            if not value:
                return
            candidate = value
            if request is not None:
                candidate = request.build_absolute_uri(candidate)
            dedupe_key = (candidate, role)
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                merged_urls.append({"link": candidate, "role": role})

        for item in list(obj.url or []):
            if isinstance(item, dict):
                link = item.get("link")
                role = item.get("role") or "RAW"
                add_url(link, role)
            else:
                add_url(item)

        return merged_urls

    class Meta:
        model = DocumentSource
        fields = [
            "id",
            "source_id",
            "title",
            "description",
            "source_type",
            "url",
            "urls",
            "publication_date",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields  # API is read-only


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


class DocumentSourceCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating DocumentSource records with file uploads via API.

    A source's links live solely in its ``url`` list. An uploaded file is an
    ingestion convenience: we store it to S3 and append the resulting permanent
    URL to ``url`` (role ``upload_role``, default RAW) rather than persisting a
    separate uploaded-file record.
    """

    url = serializers.ListField(
        child=SourceLinkField(),
        required=False,
        default=list,
        help_text="List of external URLs for this source (e.g. original article link). "
        "Each item is a dict with 'link' and 'role' keys.",
    )
    uploaded_file = serializers.FileField(
        required=False,
        write_only=True,
        validators=[
            validate_upload_file_extension,
            validate_upload_file_size,
            validate_upload_file_mimetype,
        ],
        help_text="Optional file to ingest: stored to S3 and appended to `url` "
        "as a link (role `upload_role`, default RAW).",
    )
    upload_role = serializers.ChoiceField(
        choices=[r.value for r in SourceLinkRole],
        required=False,
        default=SourceLinkRole.RAW.value,
        write_only=True,
        help_text="Role to assign to an uploaded_file's link in `url` (default RAW).",
    )

    class Meta:
        model = DocumentSource
        fields = [
            "id",
            "source_id",
            "title",
            "description",
            "source_type",
            "url",
            "publication_date",
            "uploaded_file",
            "upload_role",
        ]
        read_only_fields = ["id", "source_id"]

    def create(self, validated_data):
        """Store any uploaded file to S3 and record its link in ``url``.

        The file is NOT persisted to the ``uploaded_file`` FileField; ``url`` is
        the single source of truth for a source's links.

        The source row is created first (which runs model validation, e.g. the
        publication_date requirement for NEWS), and the file is stored only
        afterwards — so a validation failure never leaves an orphaned S3 object.
        """
        from cases.services.source_files import store_file_as_link

        uploaded_file = validated_data.pop("uploaded_file", None)
        upload_role = validated_data.pop("upload_role", SourceLinkRole.RAW.value)

        instance = super().create(validated_data)

        if uploaded_file is not None:
            link = store_file_as_link(uploaded_file, role=upload_role)
            instance.url = list(instance.url or []) + [link]
            instance.save(update_fields=["url", "updated_at"])

        return instance

    def to_internal_value(self, data):
        """
        Handle the url field arriving as a JSON-encoded string from multipart/form-data.

        When the API is called via multipart, the url list must be submitted as a
        JSON string (e.g. '["https://example.com"]'). This method parses it back into
        a Python list before normal validation runs.
        """
        import json as _json

        if isinstance(data, dict) and "url" in data and isinstance(data["url"], str):
            try:
                data = data.copy()
                data["url"] = _json.loads(data["url"])
            except (_json.JSONDecodeError, ValueError):
                pass
        return super().to_internal_value(data)

    def validate_title(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Title is required and cannot be empty")
        return value.strip()


class DocumentSourceUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating an existing DocumentSource (PATCH).

    Supports updating the ``url`` list — including adding a ``MARKDOWN``-role
    link (e.g. once a source has been converted to markdown) — plus the basic
    descriptive fields. ``source_id`` is immutable.
    """

    url = serializers.ListField(
        child=SourceLinkField(),
        required=False,
        help_text=(
            "List of URLs for this source. Each item must be a dict with "
            "'link' and 'role' keys (role can be "
            f"{', '.join(r.value for r in SourceLinkRole)})."
        ),
    )

    class Meta:
        model = DocumentSource
        fields = [
            "id",
            "source_id",
            "title",
            "description",
            "source_type",
            "url",
            "publication_date",
        ]
        read_only_fields = ["id", "source_id"]

    def to_internal_value(self, data):
        import json as _json

        if isinstance(data, dict) and "url" in data and isinstance(data["url"], str):
            try:
                data = data.copy()
                data["url"] = _json.loads(data["url"])
            except (_json.JSONDecodeError, ValueError):
                pass
        return super().to_internal_value(data)
