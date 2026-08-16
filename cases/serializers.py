"""
Serializers for the Jawafdehi accountability platform API.

See: .kiro/specs/accountability-platform-core/design.md
"""

import logging

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from .models import (
    Case,
    CaseAuthor,
    CaseEntityRelationship,
    CaseStateChange,
    Feedback,
    FeedbackType,
)

logger = logging.getLogger(__name__)


def _viewer_has_casework_access(context) -> bool:
    """Whether the requesting user may see internal casework notes.

    Mirrors the casework-visibility boundary enforced by
    ``CaseViewSet.get_queryset`` (Admin/Moderator/Caseworker/ReadOnly). Anonymous
    callers and other authenticated users are "public" and must NOT receive
    internal ``notes`` — the authoring UI labels notes "not shown publicly"
    (BB-04).

    The result is memoized on the request for the life of the response: the
    group-membership check runs a few queries and this helper is called once per
    case for both ``notes`` and each entity's note, so a list response would
    otherwise repeat it N times (N+1).
    """
    from cases.rules.predicates import (
        is_admin_or_moderator,
        is_readonly,
    )

    request = context.get("request") if context else None
    if request is None:
        return False

    cached = getattr(request, "_jawafdehi_casework_access", None)
    if cached is not None:
        return cached

    user = getattr(request, "user", None)
    if not (user and getattr(user, "is_authenticated", False)):
        result = False
    else:
        result = is_admin_or_moderator(user) or is_readonly(user)

    try:
        request._jawafdehi_casework_access = result
    except Exception:  # pragma: no cover  # noqa: BLE001 - an immutable request object is fine; the value is only cached
        pass
    return result


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


class CaseAuthorCandidateSerializer(serializers.Serializer):
    """One selectable account for the case-author byline picker.

    ``display_name`` is computed by the same helper the join uses
    (``CaseAuthor.name_for``), so what the picker shows is exactly what gets
    snapshotted onto the credit. ``username`` rides along to disambiguate two
    colleagues with the same display name — never an email or other PII.
    """

    id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(read_only=True)
    display_name = serializers.SerializerMethodField()

    @extend_schema_field(OpenApiTypes.STR)
    def get_display_name(self, obj):
        return CaseAuthor.name_for(obj)


class CaseStateChangeSerializer(serializers.ModelSerializer):
    """Read-only serializer for a single case workflow transition.

    Powers the case history / author-feedback panel: what changed, who did it,
    when, and the moderator's reason. ``actor_name`` is a display label only
    (username), never an email or other PII.
    """

    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = CaseStateChange
        fields = [
            "id",
            "from_state",
            "to_state",
            "actor_name",
            "reason",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.STR)
    def get_actor_name(self, obj):
        # Prefer a human name, fall back to username; empty when the actor row
        # was deleted (SET_NULL) or the change was system-initiated.
        if obj.actor is None:
            return ""
        return obj.actor.get_full_name() or obj.actor.get_username()


class CaseListSerializer(serializers.ListSerializer):
    """List serializer that resolves NES entities for the whole page in one pass.

    ``CaseSerializer.get_entities`` would otherwise call ``resolve_entities``
    once per case — a cross-DB N+1 across a page of cards (the home "Recently
    Documented Cases" section and the ``/cases`` browse list). Here we collect
    every ``nes_id`` across all cases in the page, resolve them in a single
    ``resolve_entities`` call, and stash the superset map on the child
    serializer's context so ``get_entities`` reuses it instead of re-resolving.
    """

    def to_representation(self, data):
        from cases.services.nes_resolver import resolve_entities

        # ``data`` is the page of Case instances (entity_relationships prefetched
        # by CaseViewSet.get_queryset, so this touches no extra DB rows).
        instances = list(data)
        nes_ids = [
            rel.nes_id
            for case in instances
            for rel in case.entity_relationships.all()
            if rel.nes_id
        ]
        # One batched lookup for the whole page; keyed by nes_id so per-case
        # build_entity_binds(resolved) reads its slice via resolved.get(...).
        self.child.context["resolved_entities"] = resolve_entities(nes_ids)
        return super().to_representation(instances)


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
    notes = serializers.SerializerMethodField(
        help_text="Internal casework notes. Returned only to authenticated "
        "casework roles (Admin/Moderator/Caseworker/ReadOnly); an empty string "
        "for public/anonymous callers (notes are 'not shown publicly')."
    )

    @extend_schema_field(serializers.CharField(allow_blank=True))
    def get_notes(self, obj):
        """Return internal notes only to casework viewers, else an empty string.

        ``notes`` is internal-only (the authoring UI labels it "not shown
        publicly"). Gating here — rather than dropping the field from the
        serializer — keeps the casework/admin editor round-trip working, since it
        reloads existing notes through this same read endpoint before PATCHing.
        """
        if _viewer_has_casework_access(self.context):
            return obj.notes
        return ""

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
        from cases.services.nes_resolver import build_entity_binds, resolve_entities

        try:
            relationships = list(obj.entity_relationships.all())
            # On the list path CaseListSerializer pre-resolves the whole page in
            # one batched call and shares the superset map here (avoids a per-case
            # N+1). The retrieve path has no such map → resolve this case's ids.
            resolved = self.context.get("resolved_entities")
            if resolved is None:
                resolved = resolve_entities(rel.nes_id for rel in relationships)
            # Per-entity relationship notes are internal-only, same as the
            # case-level notes field (BB-04): expose them to casework roles only.
            # The shared shaper (build_entity_binds) defaults notes to "" so the
            # public search-card path can't leak them; we pass include_notes for
            # casework viewers here.
            notes_visible = _viewer_has_casework_access(self.context)
            return build_entity_binds(
                relationships, resolved, include_notes=notes_visible
            )
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
    authors = serializers.SerializerMethodField(
        help_text="Credited authors of this case, in byline order "
        "(display_name + optional credit_note; user_id for casework viewers only)"
    )

    @extend_schema_field(
        inline_serializer(
            name="CaseAuthorCredit",
            many=True,
            fields={
                "user_id": serializers.IntegerField(required=False),
                "display_name": serializers.CharField(),
                "credit_note": serializers.CharField(allow_blank=True),
            },
        )
    )
    def get_authors(self, obj):
        """The public byline, in order.

        ``user_id`` is included only for casework viewers: the editor needs it to
        round-trip the author list through PATCH, but there is no reason to
        publish internal account primary keys. Public callers get the name and
        credit note, which is all the byline renders — the same "display label
        only" boundary ``CaseStateChangeSerializer.actor_name`` holds.
        """
        include_user_id = _viewer_has_casework_access(self.context)
        credits = []
        for credit in obj.author_credits.all():
            entry = {
                "display_name": credit.display_name,
                "credit_note": credit.credit_note,
            }
            if include_user_id:
                entry["user_id"] = credit.user_id
            credits.append(entry)
        return credits

    public_edit_history = serializers.ListField(
        child=serializers.DictField(),
        help_text="Public edit history entries ({date, remarks}) shown on the case page",
        required=False,
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
        # Batch NES entity resolution across a page instead of per-case (N+1).
        list_serializer_class = CaseListSerializer
        fields = [
            "id",
            "slug",
            "public_iri",
            "case_type",
            "state",
            "title",
            "short_description",
            # The structured public byline: who wrote the case, when it first
            # went live, and the curated list of edits since. All three are
            # returned to everyone, unlike the BB-04-gated internal ``notes``.
            "authors",
            "case_publish_date",
            "public_edit_history",
            # DEPRECATED free-text byline, still returned so the frontend can
            # fall back to it on cases that have no structured authors yet.
            "public_notes",
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
            # Readable, not just writable: the SPA editor renders its form from this
            # payload, so without it an editor would set weights blind.
            "weight",
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


class FeedbackTriageSerializer(serializers.ModelSerializer):
    """Staff-facing read + triage view of a submission. Carries no reporter PII.

    Narrower than ``FeedbackSerializer`` on purpose: ``contact_info``,
    ``ip_address``, ``user_agent`` and the attachment's URL are absent from the
    field list, so no combination of query params surfaces them. A triager sees
    what was reported and moves it through the workflow; reaching the person who
    reported it stays a superuser action in Django admin.

    Writable: ``status``, ``adminNotes``, and ``feedbackType``. Everything the
    reporter wrote is read-only — triage records a decision about a submission
    and re-files it, but does not rewrite it.
    """

    # Reclassification is a triage decision, not a rewrite: the public form lets
    # anyone pick "general" for what is actually a corruption allegation, and
    # only ``case_report`` gets the notification and the distinct treatment in
    # the queue. Writable here because this is now the ONLY surface that can fix
    # it — Django admin is view-only, and the public endpoint is create-only.
    feedbackType = serializers.ChoiceField(
        source="feedback_type",
        choices=FeedbackType.choices,
        required=False,
        help_text="Reclassify the submission (e.g. a corruption report filed as 'general')",
    )
    relatedPage = serializers.CharField(source="related_page", read_only=True)
    adminNotes = serializers.CharField(
        source="admin_notes",
        required=False,
        allow_blank=True,
        help_text="Internal triage notes (staff-visible only)",
    )
    hasAttachment = serializers.SerializerMethodField(
        help_text="Whether an attachment was supplied (the file itself is not exposed here)"
    )
    hasContactInfo = serializers.SerializerMethodField(
        help_text="Whether contact details were supplied (the details are not exposed here)"
    )
    submittedAt = serializers.DateTimeField(source="submitted_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    #: The only model columns a triage PATCH may touch. Used to scope the write
    #: (see ``update``) so the statement can never carry a PII column.
    TRIAGE_FIELDS = ("status", "admin_notes", "feedback_type")

    class Meta:
        model = Feedback
        fields = [
            "id",
            "feedbackType",
            "subject",
            "description",
            "relatedPage",
            "status",
            "adminNotes",
            "hasAttachment",
            "hasContactInfo",
            "submittedAt",
            "updatedAt",
        ]
        # ONLY the undeclared model fields belong here. DRF short-circuits
        # declared fields before it consults extra_kwargs (serializers.py:
        # ``if field_name in declared_fields: ... continue``), so listing
        # ``relatedPage``/``submittedAt``/``updatedAt`` here would be inert —
        # their immutability comes from ``read_only=True`` on the declaration
        # above, and a list that looks like the guard but isn't is worse than no
        # list. ``id`` is read-only automatically.
        read_only_fields = ["subject", "description"]

    def get_hasAttachment(self, obj) -> bool:
        return bool(obj.attachment)

    def get_hasContactInfo(self, obj) -> bool:
        # ``name`` alone counts. ContactInfoSerializer makes both `name` and
        # `contactMethods` optional, so {"name": "..."} with no methods is a
        # valid submission — and reporting `false` for it would tell a triager a
        # report is anonymous while the reporter's name sits in the row.
        if not obj.contact_info:
            return False
        return bool(
            obj.contact_info.get("contactMethods") or obj.contact_info.get("name")
        )

    def update(self, instance, validated_data):
        """Write ONLY the triage columns, without running ``Model.save()``.

        ``Feedback.save()`` calls ``full_clean()``, which is wrong for this path
        in three ways. It reads every concrete field, so the ``ip_address`` /
        ``user_agent`` columns this endpoint defers get pulled back into the
        process on each edit and the resulting UPDATE rewrites them. It calls
        ``Feedback.clean()``, which evaluates ``self.attachment.size`` — a HEAD
        request to object storage on every status change, and an unhandled
        exception (not a ValidationError) if the object has been purged from the
        bucket, which would make such a row permanently un-triageable now that
        Django admin cannot edit it either. And it re-validates reporter fields
        this request never touched, so one legacy row that predates a constraint
        would 500 a status change.

        A column-scoped ``QuerySet.update()`` avoids all three, and the audit
        trail survives it: this model's manager mixes in
        ``jawafdehi_shared.db.audited.AuditedQuerySet``, whose ``update()``
        already writes the UPDATE entry that ``post_save`` would have, with the
        actor bound by ``AuditlogActorMixin`` on the viewset. Do NOT add an
        explicit ``log_bulk_update`` here — that logs the edit a second time.
        """
        updates = {
            field: validated_data[field]
            for field in self.TRIAGE_FIELDS
            if field in validated_data
        }
        if not updates:
            return instance

        # auto_now doesn't fire for QuerySet.update(), so stamp it ourselves —
        # the queue's "last updated" reads this column. AuditedQuerySet omits
        # auto_now-only columns from the diff, so this doesn't pollute the trail.
        updates["updated_at"] = timezone.now()
        Feedback.objects.filter(pk=instance.pk).update(**updates)

        for field, value in updates.items():
            setattr(instance, field, value)
        return instance
