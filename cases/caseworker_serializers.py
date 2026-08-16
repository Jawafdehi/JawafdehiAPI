"""
Serializers used exclusively by the caseworker PATCH endpoint.

CasePatchSerializer validates the post-patch result dict (not the patch document
itself) before the changes are persisted.
"""

import re
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from jawafdehi_shared.entities.ids import (
    is_valid_entity_iri,
    is_valid_material_iri,
)

from .fields import EDIT_HISTORY_DATE_ERROR, parse_edit_history_date
from .models import (
    CaseState,
    CaseType,
    RelationshipOutcome,
    RelationshipType,
)
from .validators import validate_courtcase_iri, validate_slug

User = get_user_model()


class CaseInsensitiveChoiceField(serializers.ChoiceField):
    """ChoiceField that matches string input against its choices case-insensitively.

    The frontend sends UPPERCASE relationship_type values (e.g. ``"ACCUSED"``)
    while ``RelationshipType`` stores/returns lowercase (``"accused"``). We match
    the incoming value against the defined choice keys ignoring case and
    normalize to the exact choice casing before validation, so the stored value
    always matches the canonical choice (and the field is safe to reuse for
    choices with uppercase or mixed-case keys).
    """

    def to_internal_value(self, data):
        if isinstance(data, str):
            for choice_key in self.choice_strings_to_values:
                if choice_key.lower() == data.lower():
                    data = choice_key
                    break
        return super().to_internal_value(data)


# Paths that callers are not permitted to target in a patch operation.
# The view rejects any op whose `path` equals or is prefixed by one of these.
# Note: /slug is conditionally blocked based on case state (see api_views.py)
BLOCKED_PATH_PREFIXES = frozenset(
    [
        "/id",
        "/case_type",
        "/version",
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


class AuthorIdListField(serializers.ListField):
    """The writable byline: an ORDERED list of account ids.

    Order in the list IS the byline order — the only per-case fact about an
    author. Name, photo, description and links are per-person and are edited on
    the author's ``AuthorProfile``, never through a case patch.

    Accepts a bare id or a ``{"user_id": N}`` object per entry, so the editor can
    PATCH back a list derived from the richer read shape without reshaping it.
    """

    child = serializers.JSONField()

    def to_internal_value(self, data):
        entries = super().to_internal_value(data)
        ids = []
        for entry in entries:
            raw = entry.get("user_id") if isinstance(entry, dict) else entry
            # ``bool`` is an ``int`` subclass, so True would otherwise credit
            # account 1. ``child`` is a JSONField, so a float reaches here too:
            # 3.7 must be rejected, not silently truncated to account 3.
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise serializers.ValidationError(
                    f"Each author must be an integer account id or "
                    f"{{'user_id': N}}: {entry!r}"
                )
            user_id = raw
            # Bounded to the signed 64-bit range the pk column can hold. Without
            # this, 10**30 passes validation and blows up inside the ``pk__in``
            # query — a 500 on PostgreSQL, where sqlite quietly returns no rows,
            # so the test suite would not catch the production behaviour.
            if not (-(2**63) <= user_id < 2**63):
                raise serializers.ValidationError(f"Author id out of range: {raw!r}")
            # Dropped rather than rejected: a double-click in the picker should
            # not 422 an otherwise-valid save.
            if user_id not in ids:
                ids.append(user_id)

        # Resolved here so an unknown id is a 422 on /authors rather than an
        # IntegrityError partway through the join rewrite.
        known = set(
            User.objects.filter(pk__in=ids).values_list("pk", flat=True)
        )
        missing = [user_id for user_id in ids if user_id not in known]
        if missing:
            raise serializers.ValidationError(
                f"No user with id {', '.join(str(m) for m in missing)}."
            )
        return ids


class EditHistoryItemSerializer(serializers.Serializer):
    """One public edit-history entry: an AD ISO date plus free-text remarks."""

    date = serializers.CharField()
    remarks = serializers.CharField()

    def validate_date(self, value):
        # Shared with the model field so the rule cannot drift between layers.
        try:
            parse_edit_history_date(value)
        except (ValueError, TypeError):
            raise serializers.ValidationError(EDIT_HISTORY_DATE_ERROR)
        return value

    def validate_remarks(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Remarks cannot be empty.")
        return value


class EntityPatchItemSerializer(serializers.Serializer):
    # The bind holds the canonical NES entity id directly; entities are owned by
    # NES and must already exist there (no display-name fallback).
    nes_id = serializers.CharField()
    relationship_type = CaseInsensitiveChoiceField(choices=RelationshipType.choices)
    # No default: an omitted outcome stays absent from validated data so the
    # persist step can PRESERVE an accused bind's existing verdict across the
    # whole-list replace, rather than resetting it to 'charged'. A new accused
    # bind with no prior outcome falls back to 'charged' server-side; a
    # non-accused role always resolves to NULL. allow_null accepts a client
    # echoing the read snapshot's ``outcome: null`` for non-accused entities.
    outcome = CaseInsensitiveChoiceField(
        choices=RelationshipOutcome.choices,
        required=False,
        allow_null=True,
    )
    notes = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default=""
    )

    def to_internal_value(self, data):
        """Reject undeclared keys instead of dropping them.

        DRF ignores unknown keys, so ``add /entities/0/whatever`` would validate,
        return 200 with a fresh ETag, and store nothing — a write that reports
        success while doing nothing is the worst failure mode for an automated
        caller. The patch snapshot builds each entity from exactly the four
        declared fields, so any other key can only have come from the caller's
        own patch document and is always a mistake worth a 422.
        """
        if isinstance(data, dict):
            # str() the keys rather than sorting the raw dict keys: a JSON object
            # always has string keys, but the checker only knows them as
            # ``object``, which is not orderable.
            unknown = sorted(str(key) for key in data if key not in self.fields)
            if unknown:
                raise serializers.ValidationError(
                    {
                        field: (
                            "Unknown field for an entity bind. Writable fields "
                            f"are: {', '.join(sorted(self.fields))}."
                        )
                        for field in unknown
                    }
                )
        return super().to_internal_value(data)

    def validate_nes_id(self, value):
        value = (value or "").strip()
        if not is_valid_entity_iri(value):
            raise serializers.ValidationError(
                f"Invalid NES entity id: {value!r}. Must be a canonical entity "
                "@id IRI of the form "
                "'https://<authority>/entity/<prefix>/<slug>'."
            )
        return value

    def validate(self, attrs):
        # A verdict outcome is only meaningful on an ACCUSED bind. Reject it on
        # any other role (a 400 beats silently dropping it, or a DB
        # CHECK-constraint 500). 'charged' included — a non-accused role has no
        # verdict of any kind.
        if attrs.get("outcome") and (
            attrs.get("relationship_type") != RelationshipType.ACCUSED
        ):
            raise serializers.ValidationError(
                {
                    "outcome": (
                        "A verdict outcome may only be set on an 'accused' " "entity."
                    )
                }
            )
        return attrs


class CourtCaseRefsValidationMixin:
    def validate_court_cases(self, value):
        """Validate court-case references: canonical @id IRIs ONLY.

        Each ref must be the canonical court-case IRI
        (https://<base>/courtcase/<court>/<case_number>, lowercase grammar,
        known court) — no other reference form is accepted, mirroring
        ``nes_id``/``material_iri``. Deduplicated with order preserved.
        """
        if value is None:
            return None
        refs = []
        errors = []
        for ref in value:
            try:
                validate_courtcase_iri(ref)
            except DjangoValidationError as exc:
                errors.extend(exc.messages)
                continue
            if ref not in refs:
                refs.append(ref)
        if errors:
            raise serializers.ValidationError(errors)
        return refs


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


class CaseWriteFieldsSerializer(serializers.Serializer):
    """The 17 field declarations and 2 normalizers common to create and PATCH.

    Must subclass `serializers.Serializer`: DRF collects declared fields in
    `SerializerMetaclass`, reading `_declared_fields` off each base, so fields
    declared on a plain non-Serializer mixin are never collected at all and
    silently disappear from the subclasses.

    Field ORDER does change as a result. `_get_declared_fields` returns
    ``dict(base_fields + fields)``, so these 17 now come first and each
    subclass's own declarations follow:

        create: title…bigo, case_type, state, alleged_entities, related_entities
        PATCH : title…bigo, state, case_type, entities

    Previously `case_type` led on create and sat 9th on PATCH. This is
    positional only — the same field names with the same types, validators and
    required/allow_null/default flags, verified field-by-field against the
    pre-refactor serializers. JSON object key order carries no meaning for
    request parsing or DRF validation; the visible effect is the property order
    in the generated OpenAPI schema.

    `state` stays declared per-subclass because the two genuinely differ: create
    defaults it to ``CaseState.DRAFT``, PATCH leaves it absent (`empty`).
    """

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
    # Internal casework notes (Case.notes TextField; no max_length). Casework-only
    # — read gating stays in the read serializer's SerializerMethodField (BB-04);
    # this only makes the field writable via PATCH (BB-28).
    # NOT ``allow_null``: the model column is NOT NULL (``default=""``), so a JSON
    # ``null`` can't be stored — clear notes by sending "" (allow_blank). A literal
    # ``null`` is rejected (422) rather than silently coerced, matching the column.
    notes = serializers.CharField(required=False, allow_blank=True)
    # Public notes (Case.public_notes TextField, markdown). DEPRECATED — the
    # byline is now authors + case_publish_date + public_edit_history below —
    # but still writable so the ~72 un-backfilled legacy cases can be edited (and
    # cleared) until the backfill retires the field. Same NOT-NULL/default=""
    # contract as ``notes``: allow_blank to clear, NOT allow_null.
    public_notes = serializers.CharField(required=False, allow_blank=True)
    # The date the case first went live. ``allow_null`` (unlike the text fields
    # above) because the column IS nullable — a DRAFT legitimately has none. The
    # publish gate lives in ``Case.validate()``, not here, so a caseworker can
    # still save a half-finished draft.
    case_publish_date = serializers.DateField(required=False, allow_null=True)
    public_edit_history = EditHistoryItemSerializer(many=True, required=False)
    # Declared on the SHARED write serializer, not just the patch one: the SPA's
    # new-case form posts the whole form in one go, so authors typed there would
    # be silently dropped if this were PATCH-only.
    authors = AuthorIdListField(required=False)
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
        help_text=(
            "Court-case references: canonical @id IRIs "
            "(https://jawafdehi.org/courtcase/<court>/<case_number>) only"
        ),
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
    # This PATCH path is the only way to set the editorial weight — cases are
    # read-only in Django admin. NOT ``allow_null``, because the column is NOT NULL
    # (``default=0``): send 0 to unrank. 32-bit bounds because the model field is an
    # IntegerField, not bigo's BigInteger, so a wider value would pass here and then
    # fail at the DB.
    weight = serializers.IntegerField(
        required=False,
        min_value=-2147483648,
        max_value=2147483647,
    )

    def validate_missing_details(self, value):
        """Normalize empty/whitespace missing_details to None."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    def validate_slug(self, value):
        """Normalize empty/whitespace slugs to None.

        Slug immutability on PATCH is enforced at the view layer via
        ``BLOCKED_PATH_PREFIXES``, not here.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value


class CaseCreateSerializer(
    CourtCaseRefsValidationMixin,
    CaseEntityValidationMixin,
    CaseWriteFieldsSerializer,
):
    case_type = serializers.ChoiceField(choices=CaseType.choices)
    state = serializers.ChoiceField(
        choices=CaseState.choices,
        required=False,
        default=CaseState.DRAFT,
    )
    alleged_entities = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    related_entities = serializers.ListField(
        child=serializers.CharField(), required=False
    )


class CasePatchSerializer(CourtCaseRefsValidationMixin, CaseWriteFieldsSerializer):
    state = serializers.ChoiceField(choices=CaseState.choices, required=False)
    case_type = serializers.ChoiceField(choices=CaseType.choices)
    entities = EntityPatchItemSerializer(many=True, required=False)
