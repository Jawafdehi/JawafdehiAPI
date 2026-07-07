"""
Models for the Jawafdehi accountability platform.

See: .kiro/specs/accountability-platform-core/design.md
"""

import re
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from jawafdehi_shared.entities.ids import (
    build_case_iri,
    is_valid_entity_iri,
    is_valid_material_iri,
)

from .fields import (
    TextListField,
    TimelineListField,
)
from .validators import (
    parse_courtcase_ref,
    validate_courtcase_iri,
    validate_slug,
)

User = get_user_model()


# File upload configuration
ALLOWED_UPLOAD_EXTENSIONS = ["pdf", "doc", "docx", "jpg", "jpeg", "png", "md", "txt"]
ALLOWED_UPLOAD_MIMETYPES = [
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
    "text/plain",
    "text/markdown",
]
MAX_UPLOAD_FILE_SIZE = 10 * 1024 * 1024  # 10 MB in bytes


def validate_upload_file_extension(file):
    """
    Validate that the uploaded file has an allowed extension.

    Args:
        file: The uploaded file object

    Raises:
        ValidationError: If file extension is not allowed
    """
    if not file:
        return

    import os

    ext = os.path.splitext(file.name)[1].lstrip(".").lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        allowed = ", ".join(ALLOWED_UPLOAD_EXTENSIONS)
        raise ValidationError(
            f"File extension '.{ext}' is not allowed. Allowed extensions: {allowed}"
        )


def validate_upload_file_size(file):
    """
    Validate that the uploaded file is within size limits.

    Args:
        file: The uploaded file object

    Raises:
        ValidationError: If file exceeds max size
    """
    if not file:
        return

    if file.size > MAX_UPLOAD_FILE_SIZE:
        max_mb = MAX_UPLOAD_FILE_SIZE / (1024 * 1024)
        raise ValidationError(
            f"File size is {file.size / (1024 * 1024):.2f} MB, which exceeds the maximum allowed size of {max_mb} MB"
        )


def validate_upload_file_mimetype(file):
    """
    Validate that the uploaded file's MIME type is allowed.

    Uses the content_type attribute set by Django's file upload handler.
    This provides a defence-in-depth check against renamed files that pass
    the extension validator.

    Args:
        file: The uploaded file object

    Raises:
        ValidationError: If MIME type is not in ALLOWED_UPLOAD_MIMETYPES
    """
    if not file:
        return

    content_type = getattr(file, "content_type", None)
    if content_type and content_type not in ALLOWED_UPLOAD_MIMETYPES:
        allowed = ", ".join(ALLOWED_UPLOAD_MIMETYPES)
        raise ValidationError(
            f"File MIME type '{content_type}' is not allowed. Allowed types: {allowed}"
        )


def validate_nes_id(value):
    """Validate that ``value`` is a canonical NES entity @id IRI.

    NES (Nepal Entity Service) is the single source of truth for entities;
    Jawafdehi stores only the entity @id IRI
    (``https://jawafdehi.org/entity/<prefix>/<slug>``) as a join key — never
    entity data (names/type). Display details are resolved from NES in-process
    via ``cases.services.nes_resolver``.

    STRICT: the scheme+host must be the canonical ``iri_base()`` — a non-canonical
    host/scheme/port is rejected (host is part of the join key), so the stored
    ``nes_id`` always matches the NES PK.

    Raises:
        ValidationError: if ``value`` is not a valid canonical entity @id IRI.
    """
    if not value or not is_valid_entity_iri(value):
        raise ValidationError(
            f"Invalid NES entity id: {value!r}. Must be a canonical entity "
            "@id IRI of the form 'https://<authority>/entity/<prefix>/<slug>'."
        )


def validate_material_iri(value):
    """Validate that ``value`` is a canonical NGM material @id IRI.

    NGM is the single source of truth for documents ("materials"); Jawafdehi
    stores only the material @id IRI
    (``https://jawafdehi.org/material/<source>/<ident>``) as a join key on the
    ``CaseMaterialReference`` bind — never document data (title/type/links).
    Display details resolve from NGM in-process via
    ``cases.services.material_resolver``.

    STRICT: the scheme+host must be canonical (host is part of the join key), so
    the stored ``material_iri`` always matches the Material PK.

    Raises:
        ValidationError: if ``value`` is not a valid canonical material @id IRI.
    """
    if not value or not is_valid_material_iri(value):
        raise ValidationError(
            f"Invalid NGM material id: {value!r}. Must be a canonical material "
            "@id IRI of the form 'https://<authority>/material/<source>/<ident>'."
        )


class RelationshipType(models.TextChoices):
    """Enum for entity-case relationship types."""

    ALLEGED = "alleged", "Alleged"
    ACCUSED = "accused", "Accused"
    RELATED = "related", "Related"
    WITNESS = "witness", "Witness"
    OPPOSITION = "opposition", "Opposition"
    VICTIM = "victim", "Victim"
    LOCATION = "location", "Location"
    RESPONDENT = "respondent", "प्रत्यर्थी (respondent)"
    PETITIONER = "petitioner", "रिट निवेदक (petitioner)"


class RelationshipOutcome(models.TextChoices):
    """Verdict outcome of an entity's involvement in a case.

    Orthogonal to ``RelationshipType`` (the role). ``CHARGED`` is the neutral
    default = "no verdict yet / undecided", so pre-verdict binds and
    non-defendant roles need no value. Set the terminal outcomes only from a
    primary court order — an acquitted defendant must never render as accused.
    """

    CHARGED = "charged", "Charged / undecided"
    CONVICTED = "convicted", "Convicted"
    ACQUITTED = "acquitted", "Acquitted"
    ABATED = "abated", "Abated / discontinued"


class CaseEntityRelationship(models.Model):
    """
    The Case <-> NES-entity BIND, with relationship type and notes.

    This model IS the bind between a case and a Nepal Entity Service (NES)
    entity. NES is the single source of truth for entities, so the bind holds
    only the canonical NES entity @id IRI (``nes_id``,
    ``https://jawafdehi.org/entity/<prefix>/<slug>``) as the join key — it does
    NOT store any entity data (names/type). A bind cannot be created without a
    valid ``nes_id`` (no display-name fallback): private plaintiffs/defendants
    must be registered as NES entities first (privacy carve-out). There is no
    cross-DB
    foreign key — the three databases are kept and routed independently — so the
    relation to NES is by id only. Entity display details are resolved from NES
    in-process via ``cases.services.nes_resolver.resolve_entities``.
    """

    case = models.ForeignKey(
        "Case",
        on_delete=models.CASCADE,
        related_name="entity_relationships",
        help_text="The case this relationship belongs to",
    )
    nes_id = models.CharField(
        max_length=300,
        db_index=True,
        validators=[validate_nes_id],
        help_text=(
            "Canonical NES entity @id IRI "
            "(https://jawafdehi.org/entity/<prefix>/<slug>) this case is bound "
            "to. NES owns the entity data; this is the join key only."
        ),
    )
    relationship_type = models.CharField(
        max_length=20,
        choices=RelationshipType.choices,
        help_text="Type of relationship between case and entity",
    )
    outcome = models.CharField(
        max_length=20,
        choices=RelationshipOutcome.choices,
        default=RelationshipOutcome.CHARGED,
        db_default=RelationshipOutcome.CHARGED,
        db_index=True,
        help_text=(
            "Verdict outcome for this entity in this case (default 'charged' = "
            "undecided). Distinct from relationship_type (the role); set only "
            "from a primary court order."
        ),
    )
    notes = models.TextField(
        blank=True,
        default="",
        max_length=500,
        help_text="Optional notes about this relationship",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this relationship was created",
    )

    class Meta:
        verbose_name = "Case Entity Relationship"
        verbose_name_plural = "Case Entity Relationships"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "nes_id", "relationship_type"],
                name="unique_case_entity_relationship_type",
            )
        ]
        indexes = [
            models.Index(
                fields=["case", "relationship_type"],
                name="case_relationship_type_idx",
            ),
            models.Index(
                fields=["nes_id", "relationship_type"],
                name="entity_relationship_type_idx",
            ),
        ]

    def __str__(self):
        return f"{self.case.slug} - {self.nes_id} ({self.relationship_type})"

    def clean(self):
        """Validate relationship data."""
        errors = {}

        # Ensure case and a valid nes_id are provided
        if not self.case_id:
            errors["case"] = "Case is required"
        if not self.nes_id:
            errors["nes_id"] = "A NES entity id is required"
        else:
            try:
                validate_nes_id(self.nes_id)
            except ValidationError as exc:
                errors["nes_id"] = exc.messages

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Override save to validate before saving."""
        self.full_clean()
        super().save(*args, **kwargs)


class CaseMaterialReference(models.Model):
    """The Case <-> NGM-material BIND (evidence), with an optional per-case note.

    This model IS the evidence link between a case and an NGM ``Material``. NGM
    is the single source of truth for documents, so the bind holds only the
    canonical material @id IRI (``material_iri``,
    ``https://jawafdehi.org/material/<source>/<ident>``) as the join key — it does
    NOT store document data (title/type/links). There is no cross-DB foreign key
    (the three databases are routed independently), so the relation to NGM is by
    id only; display details resolve in-process via
    ``cases.services.material_resolver.resolve_materials``.

    Replaces the former denormalized ``Case.evidence`` JSON list of
    ``{source_id, description}`` (ADR: cases own no documents). The per-case
    evidence note is ``additional_details`` — OPTIONAL, and case-specific (why
    this document matters to THIS case), distinct from the Material's own global
    ``description``.
    """

    case = models.ForeignKey(
        "Case",
        on_delete=models.CASCADE,
        related_name="material_references",
        help_text="The case this evidence reference belongs to",
    )
    material_iri = models.CharField(
        max_length=300,
        db_index=True,
        validators=[validate_material_iri],
        help_text=(
            "Canonical NGM material @id IRI "
            "(https://jawafdehi.org/material/<source>/<ident>) cited as evidence. "
            "NGM owns the document data; this is the join key only."
        ),
    )
    additional_details = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Optional case-specific note on why this material matters to this "
            "case (distinct from the material's own global description)."
        ),
    )
    # Stable display order of evidence within a case (evidence was an ordered
    # JSON list; preserve that ordering intent explicitly).
    ordinal = models.PositiveIntegerField(
        default=0,
        help_text="Display order of this evidence reference within the case.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this evidence reference was created",
    )

    class Meta:
        verbose_name = "Case Material Reference"
        verbose_name_plural = "Case Material References"
        ordering = ["ordinal", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "material_iri"],
                name="unique_case_material_reference",
            )
        ]
        indexes = [
            models.Index(fields=["case", "ordinal"], name="case_material_ordinal_idx"),
        ]

    def __str__(self):
        return f"{self.case.slug} - {self.material_iri}"

    def clean(self):
        """Validate the bind."""
        errors = {}
        if not self.case_id:
            errors["case"] = "Case is required"
        if not self.material_iri:
            errors["material_iri"] = "A NGM material id is required"
        else:
            try:
                validate_material_iri(self.material_iri)
            except ValidationError as exc:
                errors["material_iri"] = exc.messages
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Override save to validate before saving."""
        self.full_clean()
        super().save(*args, **kwargs)


class CaseCourtCaseReference(models.Model):
    """The Case <-> NGM-court-case BIND (court record references).

    This model IS the link between a case and an NGM court case. NGM is the
    single source of truth for court records, so the bind holds only the
    canonical court-case @id IRI (``courtcase_iri``,
    ``https://jawafdehi.org/courtcase/<court>/<case_number>``) as the join key —
    it does NOT store court-record data. There is no cross-DB foreign key (the
    three databases are routed independently), so the relation to NGM is by id
    only; the IRI mirrors the read-plane route
    ``/api/courtcases/<court>/<case_number>``.

    Replaces the former denormalized ``Case.court_cases`` JSON list of
    ``"<court>:<case_number>"`` strings. ``Case.court_cases`` remains as a
    property over this join (returning the IRIs in ordinal order); the IRI is
    the ONLY reference form, everywhere — API, admin, and importers.
    """

    case = models.ForeignKey(
        "Case",
        on_delete=models.CASCADE,
        related_name="courtcase_references",
        help_text="The case this court-case reference belongs to",
    )
    courtcase_iri = models.CharField(
        max_length=300,
        db_index=True,
        validators=[validate_courtcase_iri],
        help_text=(
            "Canonical court-case @id IRI "
            "(https://jawafdehi.org/courtcase/<court>/<case_number>) this case "
            "references. NGM owns the court record; this is the join key only."
        ),
    )
    # Stable display order of references within a case (court_cases was an
    # ordered JSON list — the primary/first-instance reference comes first).
    ordinal = models.PositiveIntegerField(
        default=0,
        help_text="Display order of this court-case reference within the case.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this court-case reference was created",
    )

    class Meta:
        verbose_name = "Case Court Case Reference"
        verbose_name_plural = "Case Court Case References"
        ordering = ["ordinal", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "courtcase_iri"],
                name="unique_case_courtcase_reference",
            )
        ]
        indexes = [
            models.Index(fields=["case", "ordinal"], name="case_courtcase_ordinal_idx"),
        ]

    def __str__(self):
        return f"{self.case.slug} - {self.courtcase_iri}"

    def clean(self):
        """Validate the bind."""
        errors = {}
        if not self.case_id:
            errors["case"] = "Case is required"
        if not self.courtcase_iri:
            errors["courtcase_iri"] = "A court-case @id IRI is required"
        else:
            try:
                validate_courtcase_iri(self.courtcase_iri)
            except ValidationError as exc:
                errors["courtcase_iri"] = exc.messages
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Override save to validate before saving."""
        self.full_clean()
        super().save(*args, **kwargs)


class CaseType(models.TextChoices):
    """Enum for case types."""

    CORRUPTION = "CORRUPTION", "Corruption"
    BRIBERY = "BRIBERY", "Bribery"
    FORGERY = "FORGERY", "Forgery"
    EMBEZZLEMENT = "EMBEZZLEMENT", "Embezzlement"
    ABUSE_OF_OFFICE = "ABUSE_OF_OFFICE", "Abuse of Office"
    MONEY_LAUNDERING = "MONEY_LAUNDERING", "Money Laundering"
    ILLEGAL_PROPERTY = "ILLEGAL_PROPERTY", "Illegal Property"
    EXAM_RIGGING = "EXAM_RIGGING", "Exam Rigging"
    TAX_EVASION = "TAX_EVASION", "Tax Evasion"


# Case types that must name at least one ACCUSED entity before they can leave
# DRAFT. Every other case type only requires a non-location entity (i.e. a named
# subject). This is the single source of truth for the accused-entity policy;
# the model, the admin formset, and the review engine all consult it so they
# never drift. ``requires_accused`` accepts either a ``CaseType`` member or its
# plain string value (``TextChoices`` values are ``str`` subclasses).
CASE_TYPES_REQUIRING_ACCUSED = frozenset({CaseType.CORRUPTION})


def requires_accused(case_type):
    """Whether a case of this type must tag at least one ACCUSED entity."""
    return case_type in CASE_TYPES_REQUIRING_ACCUSED


class CaseState(models.TextChoices):
    """Enum for case states."""

    DRAFT = "DRAFT", "Draft"
    IN_REVIEW = "IN_REVIEW", "In Review"
    PUBLISHED = "PUBLISHED", "Published"
    CLOSED = "CLOSED", "Closed"


class Case(models.Model):
    """
    Core model representing a case of alleged misconduct.

    IDENTIFIERS
    -----------
    * Internal identifier: the ``slug`` (a stable, unique ``SlugField``). The
      legacy opaque ``case_id`` (``case-<hex>``) column has been DROPPED — it
      was redundant with the slug, which is already unique + stable + the URL
      addressing key. Code that needs a stable per-case handle uses ``slug``
      (or the DB ``pk`` for purely in-process joins).
    * External / public identifier: the ``slug`` is the public handle, surfaced
      as the canonical case ``@id`` IRI ``https://jawafdehi.org/case/<slug>``.
    * Court case REFERENCES (the ``courtcase_references`` join, surfaced as the
      ``court_cases`` property of canonical court-case @id IRIs) are a
      DIFFERENT thing — external references to court records, NOT this case's
      identifier.

    The canonical case ``@id`` IRI is MINTED AT PUBLISH: ``public_iri`` returns
    the IRI only once ``state == PUBLISHED`` (else ``None``). The IRI is derived
    from the slug — no separate stored column.

    Each case has a single row. Edits are made in-place. State transitions
    (submit/publish) are recorded in the versionInfo JSON field.
    """

    # Core fields
    case_type = models.CharField(
        max_length=20,
        choices=CaseType.choices,
        help_text="Type of case",
    )
    state = models.CharField(
        max_length=20,
        choices=CaseState.choices,
        default=CaseState.DRAFT,
        db_index=True,
        help_text="Current state in the workflow",
    )
    title = models.CharField(max_length=200, help_text="Case title")
    short_description = models.TextField(
        blank=True, help_text="Short description/summary of the case"
    )
    thumbnail_url = models.URLField(
        blank=True,
        max_length=500,
        help_text="URL to a small thumbnail picture for the case",
    )
    banner_url = models.URLField(
        blank=True, max_length=500, help_text="URL to a large banner image for the case"
    )
    # Date fields
    case_start_date = models.DateField(
        null=True, blank=True, help_text="When the alleged incident began"
    )
    case_end_date = models.DateField(
        null=True, blank=True, help_text="When the alleged incident ended"
    )

    # Entity relationships live on the CaseEntityRelationship bind (the
    # ``entity_relationships`` reverse relation), which holds the canonical NES
    # entity id (nes_id) directly. There is no M2M to a local entity table —
    # NES is the single source of truth for entities.

    # Content fields
    tags = TextListField(blank=True, help_text="List of tags for categorization")
    description = models.TextField(
        blank=True, help_text="Markdown description of the case"
    )
    key_allegations = TextListField(
        blank=True, help_text="List of key allegation statements"
    )

    # Structured data fields
    timeline = TimelineListField(help_text="List of timeline entries")
    # Evidence is no longer a denormalized JSON list on the case. It is now the
    # CaseMaterialReference join (case.material_references) keyed by material_iri
    # (ADR: cases own no documents).

    # Relationships
    contributors = models.ManyToManyField(
        User,
        blank=True,
        related_name="assigned_cases",
        help_text="Contributors assigned to this case",
    )

    # Metadata
    versionInfo = models.JSONField(
        default=dict, blank=True, help_text="Version metadata tracking changes"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Notes field (markdown supported, internal use)
    notes = models.TextField(
        blank=True,
        default="",
        help_text="Internal notes about the case (markdown supported)",
    )

    # New fields for case identification and tracking
    slug = models.SlugField(
        max_length=50,
        blank=True,
        null=False,
        unique=True,
        db_index=True,
        validators=[validate_slug],
        help_text="A slug will go in the URL (e.g., jawafdehi.org/case/YOUR-SLUG). For CIAA corruption cases, you can prepend the special court case number (e.g., case-078-WC-0123-sunil-poudel). Must start with a letter and contain only letters, numbers, and hyphens (max 50 characters). Immutable once set, auto-generated on save if not provided.",
    )
    # Court-case references live on the CaseCourtCaseReference join (the
    # ``courtcase_references`` reverse relation), which holds the canonical
    # court-case @id IRI directly. Surfaced here as the ``court_cases``
    # property (list of IRIs, strict — no other reference form is accepted).
    missing_details = models.TextField(
        blank=True,
        null=True,
        help_text="Notes about missing or incomplete information for this case",
    )
    bigo = models.BigIntegerField(
        blank=True,
        null=True,
        help_text="Bigo (बिगो) — the total disputed or embezzled amount claimed in the case (in NPR)",
    )

    class Meta:
        ordering = ["-created_at"]

    # Pending (assigned but not yet saved) court-case reference list. Class
    # default None = "not assigned"; the ``court_cases`` setter replaces it on
    # the instance with the canonicalized IRI list, and ``save()`` syncs it to
    # the CaseCourtCaseReference join. A class attribute (not set in
    # ``__init__``) because Django's ``Model.__init__`` applies property
    # kwargs (``Case(court_cases=[...])``) via the setter DURING
    # ``super().__init__``, which an ``__init__`` assignment would clobber.
    _pending_court_cases = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track original slug value to detect changes without extra query
        self._original_slug = self.slug

    def __str__(self):
        return f"{self.slug or '(no slug)'} - {self.title} ({self.state})"

    @property
    def court_cases(self):
        """The case's court-case references as canonical @id IRIs.

        Reads the ``CaseCourtCaseReference`` join (in ordinal order), or the
        pending not-yet-saved assignment. Assignment accepts canonical IRIs
        ONLY (strict-validated, deduplicated, order preserved) and persists on
        the next ``save()``.
        """
        if self._pending_court_cases is not None:
            return list(self._pending_court_cases)
        if self.pk is None:
            return []
        return [ref.courtcase_iri for ref in self.courtcase_references.all()]

    @court_cases.setter
    def court_cases(self, value):
        if value is None:
            value = []
        if not isinstance(value, (list, tuple)):
            raise ValidationError("court_cases must be a list")
        refs = []
        for ref in value:
            if not isinstance(ref, str):
                raise ValidationError("Each court case reference must be a string")
            validate_courtcase_iri(ref)
            if ref not in refs:
                refs.append(ref)
        self._pending_court_cases = refs

    def _sync_courtcase_references(self, desired=None):
        """Persist court-case references (canonical IRIs) to the join table.

        THE single write path for the join (the PATCH endpoint calls it with
        the validated list; ``save()`` calls it with the pending property
        assignment). Replace semantics: rows are rewritten so the set +
        ordering match ``desired`` exactly (mirrors the material-reference
        rewrite) — but an unchanged list is a no-op, so row identity,
        ``created_at`` provenance, and the audit trail don't churn on saves
        that didn't touch the references. Atomic: a failure mid-rewrite rolls
        back rather than leaving the case with a partial reference set.
        """
        if desired is None:
            desired = self._pending_court_cases
        if desired is None:
            # Nothing assigned and nothing passed — no write intent.
            return
        self._pending_court_cases = None
        current = [ref.courtcase_iri for ref in self.courtcase_references.all()]
        if list(desired) == current:
            return
        with transaction.atomic():
            self.courtcase_references.all().delete()
            for ordinal, iri in enumerate(desired):
                CaseCourtCaseReference.objects.create(
                    case=self, courtcase_iri=iri, ordinal=ordinal
                )
        # A stale prefetch would otherwise keep serving the pre-sync rows.
        if hasattr(self, "_prefetched_objects_cache"):
            self._prefetched_objects_cache.pop("courtcase_references", None)

    @property
    def public_iri(self):
        """The canonical public case ``@id`` IRI, minted at publish.

        Returns ``https://jawafdehi.org/case/<slug>`` only when the case is
        PUBLISHED (and has a slug); otherwise ``None``. The IRI is derived from
        the slug — there is no separate stored column.
        """
        if self.state != CaseState.PUBLISHED or not self.slug:
            return None
        return build_case_iri(self.slug)

    def get_entities_by_type(self, relationship_type):
        """
        Get the NES entity ids bound to this case for a relationship type.

        Args:
            relationship_type: RelationshipType enum value or string

        Returns:
            List of canonical NES entity @id IRI strings
            (``https://jawafdehi.org/entity/<prefix>/<slug>``) for the binds of
            the given relationship type. Resolve display
            details via ``cases.services.nes_resolver.resolve_entities``.
        """
        return list(
            CaseEntityRelationship.objects.filter(
                case=self,
                relationship_type=relationship_type,
            ).values_list("nes_id", flat=True)
        )

    def _generate_unique_slug(self) -> str:
        """
        Generate a unique, URL-friendly slug.

        Derived from the court case number / title, with a short random suffix
        for uniqueness. The slug is the case's stable internal+public identifier
        (the legacy ``case_id`` column has been dropped), so it is generated once
        at creation and is immutable thereafter (outside DRAFT).
        """
        parts = []
        from django.utils.text import slugify

        # 1. Try to extract the case number from the court-case references
        #    (canonical IRIs; the pending assignment at create time)
        for cc in self.court_cases:
            parsed = parse_courtcase_ref(cc)
            if parsed and parsed[1]:
                parts.append(slugify(parsed[1]))
                break

        # 2. If no court_cases CR number, try to extract case number from title
        #    (e.g. "CIAA Special Court Case 080-CR-0127" → "080-cr-0127")
        if not parts and self.title:
            # NB: ``re`` is imported at module level. A redundant local
            # ``import re`` here would make ``re`` a method-local name for the
            # WHOLE function, so the unconditional ``re.sub(...)`` below (reached
            # when this branch is skipped) raised UnboundLocalError.
            cr_match = re.search(r"(\d{3}-CR-\d{4})", self.title)
            if cr_match:
                parts.append(slugify(cr_match.group(1)))

        # 3. Add title (truncated to avoid overly long slugs)
        if self.title:
            parts.append(slugify(self.title)[:30])

        base = "-".join(p for p in parts if p)

        if not base:
            base = "case"

        # Django's slugify() PRESERVES underscores, but validate_slug (and the
        # case @id IRI grammar) forbid them — so strip underscores to hyphens and
        # collapse, guaranteeing the generated slug always satisfies validate_slug
        # (^[a-zA-Z][a-zA-Z0-9-]{0,49}$). Without this, a title containing "_"
        # produced an invalid slug → build_case_iri()/public_iri raised on read.
        base = re.sub(r"[_-]+", "-", base).strip("-")

        if not base:
            base = "case"

        # Ensure base starts with a letter (required by validate_slug)
        if base and not base[0].isalpha():
            base = f"case-{base}"

        # Random short suffix for uniqueness (the slug is the case's identity, so
        # there is no pre-existing stable key to hash; a fresh case gets a fresh
        # slug).
        suffix = uuid.uuid4().hex[:6]
        slug = f"{base}-{suffix}"

        return slug[:50]

    def save(self, *args, **kwargs):
        """Override save; auto-generate the slug (case identity) for new cases."""
        # Normalize empty/whitespace slug to None to avoid unique constraint violations
        if self.slug is not None and not self.slug.strip():
            self.slug = None

        # Validate title is not empty
        if not self.title or not self.title.strip():
            raise ValidationError("Title cannot be empty")

        # Auto-generate slug for any case without one (slug-only API addressing).
        if not self.slug or not self.slug.strip():
            self.slug = self._generate_unique_slug()

        # Enforce slug immutability (use cached original value to avoid extra query)
        # Allow slug modification for DRAFT cases
        if self.pk and hasattr(self, "_original_slug"):
            if (
                self._original_slug
                and self._original_slug != self.slug
                and self.state != CaseState.DRAFT
            ):
                raise ValidationError("Slug cannot be modified once set")

        super().save(*args, **kwargs)

        # Update cached original slug after successful save
        self._original_slug = self.slug

        # Persist any pending court_cases assignment to the join table now
        # that the row (and pk) exist.
        if self._pending_court_cases is not None:
            self._sync_courtcase_references()

    def validate(self):
        """
        Validate case data based on current state.

        - DRAFT: Lenient validation (only title required)
        - IN_REVIEW/PUBLISHED: Strict validation (all required fields must be complete)
        """
        errors = {}

        # Always require title
        if not self.title or not self.title.strip():
            errors["title"] = "Title is required"

        # Strict validation for IN_REVIEW and PUBLISHED states
        if self.state in [CaseState.IN_REVIEW, CaseState.PUBLISHED]:
            # Entity requirement depends on case type. CORRUPTION cases must name
            # at least one ACCUSED entity; other case types (e.g. TAX_EVASION)
            # only require a named subject — any non-location entity. A
            # location-only case is not a valid subject (the UI also excludes
            # locations when naming a case's subject).
            if requires_accused(self.case_type):
                has_required_entity = self.entity_relationships.filter(
                    relationship_type=RelationshipType.ACCUSED
                ).exists()
                entity_error = "At least one accused entity is required for IN_REVIEW or PUBLISHED state"
            else:
                has_required_entity = self.entity_relationships.exclude(
                    relationship_type=RelationshipType.LOCATION
                ).exists()
                entity_error = "At least one non-location entity is required for IN_REVIEW or PUBLISHED state"
            if not has_required_entity:
                errors["entities"] = entity_error

            if not self.key_allegations or len(self.key_allegations) == 0:
                errors["key_allegations"] = (
                    "At least one key allegation is required for IN_REVIEW or PUBLISHED state"
                )

            if not self.description or not self.description.strip():
                errors["description"] = (
                    "Description is required for IN_REVIEW or PUBLISHED state"
                )

        # Auto-generate slug for any case without one (slug-only API addressing).
        if not self.slug or not self.slug.strip():
            self.slug = self._generate_unique_slug()

        if errors:
            raise ValidationError(errors)

    def submit(self):
        """
        Submit a draft case for review.

        Transitions state from DRAFT to IN_REVIEW after validation.
        """
        if self.state != CaseState.DRAFT:
            raise ValidationError(
                f"Can only submit cases in DRAFT state, current state is {self.state}"
            )

        # Validate before submission
        self.state = CaseState.IN_REVIEW
        self.validate()

        # Update versionInfo
        self.versionInfo = {
            "action": "submitted",
            "datetime": timezone.now().isoformat(),
        }

        self.save()

    def publish(self):
        """
        Publish this case.

        Sets state to PUBLISHED and updates versionInfo.
        Auto-generates slug if not already set.
        """
        if self.state not in [CaseState.IN_REVIEW, CaseState.DRAFT]:
            raise ValidationError(
                f"Can only publish cases in IN_REVIEW or DRAFT state, current state is {self.state}"
            )

        # Set state to PUBLISHED
        self.state = CaseState.PUBLISHED

        # Ensure slug exists for published cases
        if not self.slug or not self.slug.strip():
            self.slug = self._generate_unique_slug()

        # Validate before publishing
        self.validate()

        # Update versionInfo
        self.versionInfo = {
            "action": "published",
            "datetime": timezone.now().isoformat(),
        }

        self.save()

    def delete(self, using=None, keep_parents=False):
        """
        Soft delete the case by setting state to CLOSED.

        The case record is never hard-deleted; state is set to CLOSED so it
        remains in the database but is no longer publicly visible.
        """
        self.state = CaseState.CLOSED

        # Update versionInfo to track the deletion
        self.versionInfo = {
            "action": "deleted",
            "datetime": timezone.now().isoformat(),
        }

        self.save()

        # Return a tuple (num_deleted, dict) to match Django's delete() signature
        # Since we're soft deleting, we report 0 actual deletions
        return (0, {self._meta.label: 0})


class FeedbackType(models.TextChoices):
    """Enum for feedback types."""

    BUG = "bug", "Bug Report"
    FEATURE = "feature", "Feature Request"
    USABILITY = "usability", "Usability Issue"
    CONTENT = "content", "Content Feedback"
    GENERAL = "general", "General Feedback"


class FeedbackStatus(models.TextChoices):
    """Enum for feedback status."""

    SUBMITTED = "submitted", "Submitted"
    IN_REVIEW = "in_review", "In Review"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"


class Feedback(models.Model):
    """
    Platform feedback submissions from users.

    Stores feedback, bug reports, feature requests, and general comments
    about the Jawafdehi platform.
    """

    # Core fields
    feedback_type = models.CharField(
        max_length=20, choices=FeedbackType.choices, help_text="Type of feedback"
    )
    subject = models.CharField(max_length=200, help_text="Brief summary of feedback")
    description = models.TextField(
        max_length=5000, help_text="Detailed feedback description"
    )
    related_page = models.CharField(
        max_length=300, blank=True, help_text="Page or feature related to feedback"
    )

    # Contact information (stored as JSON for flexibility)
    contact_info = models.JSONField(
        default=dict, blank=True, help_text="Optional contact information"
    )

    # Status tracking
    status = models.CharField(
        max_length=20,
        choices=FeedbackStatus.choices,
        default=FeedbackStatus.SUBMITTED,
        db_index=True,
        help_text="Current status of feedback",
    )

    # Metadata
    ip_address = models.GenericIPAddressField(
        null=True, blank=True, help_text="IP address of submitter (for rate limiting)"
    )
    user_agent = models.TextField(blank=True, help_text="User agent string")

    # File attachment
    attachment = models.FileField(
        upload_to="feedback_attachments/",
        blank=True,
        null=True,
        help_text="Optional file attachment (max 10 MB)",
    )

    # Admin notes
    admin_notes = models.TextField(
        blank=True, help_text="Internal notes for administrators"
    )

    # Timestamps
    submitted_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["feedback_type", "status"]),
            models.Index(fields=["status", "submitted_at"]),
        ]

    def __str__(self):
        return f"{self.feedback_type.upper()}: {self.subject}"

    def clean(self):
        """Validate attachment size at the model level (covers admin and direct-save paths)."""
        super().clean()
        if self.attachment and self.attachment.size > 10 * 1024 * 1024:
            raise ValidationError(
                {"attachment": "Attachment must be 10 MB or smaller."}
            )

    def save(self, *args, **kwargs):
        """Enforce model-level validation before saving."""
        self.full_clean(validate_unique=False)
        super().save(*args, **kwargs)


class ChatUserIdentity(models.Model):
    owui_user_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Chat system user identifier (e.g., OpenWebUI user ID)",
    )
    owui_user_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Display name from the chat system",
    )
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_identities",
        help_text="Django user associated with this chat identity (must be mapped for authorization)",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this identity mapping was created",
    )

    class Meta:
        verbose_name = "Chat User Identity"
        verbose_name_plural = "Chat User Identities"

    def __str__(self):
        user_display = self.user.get_username() if self.user else "(unmapped)"
        return f"{self.owui_user_id} -> {user_display}"


class StatisticsSnapshot(models.Model):
    """Precomputed /api/statistics/ payload (one row, key ``statistics``).

    Written out-of-band by ``cases.services.statistics.refresh_statistics``
    (the ``refresh_statistics`` management command, run on a schedule) and read
    by ``StatisticsView`` as a single primary-key lookup, so the public endpoint
    never pays the multi-second NES/NGM aggregation. Deliberately a keyed row
    rather than a TTL cache entry: a missed refresh serves stale-but-valid data
    instead of a request-blocking recompute or nothing.
    """

    key = models.CharField(max_length=64, primary_key=True)
    data = models.JSONField(
        help_text="The exact JSON payload served by /api/statistics/"
    )
    computed_at = models.DateTimeField(
        help_text="When this payload was computed (also carried as last_updated inside data)"
    )
    # True only for the bootstrap claim row (zeroed placeholder committed while
    # the winning request computes the real payload). Placeholder responses are
    # served with Cache-Control: no-store so the zeros are never edge-cached;
    # the refresh upsert clears the flag. db_default keeps inserts from
    # not-yet-rolled code valid during deploys.
    is_placeholder = models.BooleanField(default=False, db_default=False)

    class Meta:
        verbose_name = "Statistics Snapshot"
        verbose_name_plural = "Statistics Snapshots"

    def __str__(self):
        return f"{self.key} @ {self.computed_at.isoformat()}"
