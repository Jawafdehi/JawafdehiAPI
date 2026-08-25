"""
Models for the Jawafdehi accountability platform.

See: .kiro/specs/accountability-platform-core/design.md
"""

import re
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from jawafdehi_shared.entities.ids import (
    build_case_iri,
    is_valid_entity_iri,
    is_valid_material_iri,
)

from .fields import (
    AuthorLinkListField,
    EditHistoryListField,
    HttpsURLField,
    TextListField,
    TimelineListField,
)
from .validators import (
    parse_courtcase_ref,
    validate_courtcase_iri,
    validate_slug,
)

User = get_user_model()


def name_for_user(user):
    """The display name for an account: full name, else username.

    Mirrors ``CaseStateChangeSerializer.get_actor_name`` — a display label only,
    never an email or other PII. Used to seed a new ``AuthorProfile`` and as the
    fallback when a profile carries no name of its own.
    """
    full_name = (user.get_full_name() or "").strip()
    return full_name or user.get_username()


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
    """Verdict outcome for an ACCUSED entity in a case.

    Meaningful ONLY for ``RelationshipType.ACCUSED`` — every other role leaves
    ``outcome`` NULL (enforced by the ``outcome_only_on_accused`` CHECK
    constraint). ``CHARGED`` = "formally charged, verdict pending"; the terminal
    outcomes (CONVICTED/ACQUITTED/ABATED) are set only from a primary court
    order — an acquitted defendant must never render as accused.
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
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Verdict outcome for this ACCUSED entity (NULL for every other "
            "role). 'charged' = formally charged, verdict pending. Distinct "
            "from relationship_type (the role); terminal verdicts are set only "
            "from a primary court order."
        ),
    )
    notes = models.TextField(
        blank=True,
        default="",
        max_length=500,
        help_text="Optional notes about this relationship",
    )
    # Stable display order of this bind within a case. Ordering used to be
    # ``-created_at`` alone, which is neither stable nor meaningful here: the
    # PATCH endpoint rewrites the whole list (delete-all + recreate), so every
    # entities-touching write re-stamped created_at and REVERSED the list, and
    # rows sharing a timestamp (bulk imports) came back in whatever order the
    # scan happened to produce. Index-based RFC-6902 paths (/entities/3/notes)
    # were unsafe as a result, and the public case page reshuffled its accused
    # list on every edit.
    ordinal = models.PositiveIntegerField(
        default=0,
        help_text="Display order of this entity bind within the case.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this relationship was created",
    )

    class Meta:
        verbose_name = "Case Entity Relationship"
        verbose_name_plural = "Case Entity Relationships"
        # ``pk`` is the final tie-breaker so the order is TOTAL: ordinal and
        # created_at can both tie (get_or_create paths leave ordinal at 0, bulk
        # imports share a timestamp), and a partial ORDER BY lets Postgres
        # return tied rows in any order it likes — including a different one on
        # successive reads of an unmodified case.
        ordering = ["ordinal", "created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "nes_id", "relationship_type"],
                name="unique_case_entity_relationship_type",
            ),
            # A verdict outcome is meaningful only for the ACCUSED role; every
            # other role must leave it NULL. Backstops the save()/serializer
            # normalization against any raw-SQL / bulk write path.
            models.CheckConstraint(
                condition=models.Q(relationship_type=RelationshipType.ACCUSED)
                | models.Q(outcome__isnull=True),
                name="outcome_only_on_accused",
            ),
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
            models.Index(fields=["case", "ordinal"], name="case_entity_ordinal_idx"),
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
        """Normalize the verdict outcome, validate, then save."""
        # A verdict outcome is meaningful only for the ACCUSED role: force it
        # NULL for every other role, and default an accused bind with no
        # explicit outcome to CHARGED (formally charged, verdict pending).
        # Backstopped by the ``outcome_only_on_accused`` DB CHECK constraint.
        if self.relationship_type != RelationshipType.ACCUSED:
            self.outcome = None
        elif not self.outcome:
            self.outcome = RelationshipOutcome.CHARGED
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


class AuthorProfile(models.Model):
    """The person behind a byline: one row per credited contributor.

    Everything here is PER-PERSON, which is the whole point of splitting it out
    of the byline. The old free-text ``public_notes`` carried "(BALLB 4th Year
    Student)" on three of Sambhav Koirala's eleven cases and nowhere else — not
    because his role differed case to case, but because a hand-copied line drifts.
    A description belongs to the person, so it is stored once and shows on every
    case they wrote.

    The trade-off, accepted deliberately: a title goes stale ("BALLB 4th Year
    Student" will not be true next year), and updating it changes the byline on
    every case that person ever wrote. That is the correct behaviour for a
    fact about a person, and it is why there is no per-case name snapshot — a
    byline points at a person, it does not freeze them.

    The profile is auto-created the first time someone is credited (see
    ``ensure_for``), so every case author has a slug from the moment they are
    credited. Photo/title/bio/links start blank and are filled in later;
    ``has_public_page`` gates the public profile page so an empty one is never
    published.

    Lives here rather than as columns on ``User`` because this project runs on
    stock ``django.contrib.auth.models.User`` (``AUTH_USER_MODEL`` is unset), so
    adding fields to it would mean swapping the user model on a live database.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="author_profile",
        help_text="The account this profile describes",
    )
    slug = models.SlugField(
        max_length=60,
        unique=True,
        db_index=True,
        validators=[validate_slug],
        help_text=(
            "URL handle for the public profile "
            "(jawafdehi.org/author/<slug>). Generated from the name on first "
            "credit and stable thereafter."
        ),
    )
    # Bilingual, mirroring the frontend team page's `displayName {en, ne}`. The
    # platform is Nepali-first, so a profile with only an English name renders
    # that in both languages rather than showing nothing.
    name_en = models.CharField(max_length=150, blank=True, default="")
    name_ne = models.CharField(max_length=150, blank=True, default="")
    photo_url = HttpsURLField(
        blank=True,
        max_length=500,
        help_text="Profile picture URL (hosted; the team photos live on R2)",
    )
    title = models.TextField(
        blank=True,
        default="",
        help_text=(
            "One-line role shown under the name and on the author card on every "
            "case page, e.g. 'Caseworker' or 'BALLB 4th Year Student'. Keep it "
            "short — the card truncates. Longer prose goes in ``bio``."
        ),
    )
    # Separate from ``title`` because the two have different jobs and different
    # space: the title rides along on a compact card on every case page, where a
    # paragraph would wreck the layout, while this renders only on the profile
    # page.
    bio = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Longer public biography shown on the author's profile page "
            "(markdown). Empty = the About section is not rendered."
        ),
    )
    # A personal address, so it is opt-in by presence: blank = not shown. There
    # is no separate visibility flag because an empty field already says "no".
    email = models.EmailField(
        blank=True,
        default="",
        help_text="Public contact address. Blank = not shown anywhere.",
    )
    links = AuthorLinkListField(
        help_text="Public social links: [{type, value}] with https:// URLs.",
    )
    has_public_page = models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            "Whether /author/<slug> is published and author cards link to it. "
            "Off by default: a profile is created automatically on first credit "
            "and starts empty, and an empty public page is worse than none."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Author Profile"
        verbose_name_plural = "Author Profiles"
        ordering = ["name_en", "slug"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        """The name to show: the profile's English name, else the account's."""
        return self.name_en.strip() or name_for_user(self.user)

    def name_for_language(self, language="en"):
        """The profile name in ``language``, falling back to the other one.

        A profile with only an English name must still render on the Nepali site
        (and vice versa) — showing a blank byline would be worse than showing the
        name in the wrong script.
        """
        if str(language).startswith("ne"):
            return self.name_ne.strip() or self.display_name
        return self.display_name

    @classmethod
    def ensure_for(cls, user):
        """Get or create this user's profile, minting a slug on first sight.

        Called whenever a credit is written, so "every case author has a slug"
        holds by construction rather than by remembering to run a backfill.
        """
        profile = cls.objects.filter(user=user).first()
        if profile is not None:
            return profile
        name = name_for_user(user)
        try:
            # savepoint so a losing race does not poison the caller's atomic
            # block — on PostgreSQL an IntegrityError aborts the whole
            # transaction unless the failing statement is rolled back to one.
            with transaction.atomic():
                return cls.objects.create(
                    user=user,
                    slug=cls._generate_unique_slug(name),
                    name_en=name,
                )
        except IntegrityError:
            # Lost a race on either ``user`` (another credit for this account
            # committed first) or ``slug`` (a same-named colleague took the
            # candidate between the exists() check and the insert). Both are
            # check-then-create windows and both are benign: re-read.
            profile = cls.objects.filter(user=user).first()
            if profile is not None:
                return profile
            # The slug collided rather than the user — retry once, now that the
            # winner's slug is visible to _generate_unique_slug.
            return cls.objects.create(
                user=user,
                slug=cls._generate_unique_slug(name),
                name_en=name,
            )

    @staticmethod
    def _generate_unique_slug(name):
        """A unique, URL-safe slug derived from a person's name.

        Suffixes ``-2``, ``-3``… on collision rather than a random hex tail (as
        cases do): an author slug is a person's public handle and is read aloud
        and typed, so "sambhav-koirala-2" beats "sambhav-koirala-a3f9c1".
        """
        from django.utils.text import slugify

        base = slugify(name or "") or "author"
        # validate_slug requires a leading letter (^[a-zA-Z]).
        if not base[0].isalpha():
            base = f"author-{base}"
        base = base[:55].strip("-") or "author"

        candidate = base
        suffix = 1
        while AuthorProfile.objects.filter(slug=candidate).exists():
            suffix += 1
            tail = f"-{suffix}"
            candidate = f"{base[: 60 - len(tail)]}{tail}"
        return candidate


class CaseAuthor(models.Model):
    """The Case <-> author through table. Carries the byline ORDER, nothing else.

    Everything else about an author — name, photo, description, links — is
    per-person and lives on ``AuthorProfile``. The only per-case fact is where
    someone sits in the byline, so that is the only column here beyond the two
    keys. This is Django's spelling of an ordered many-to-many: ``Case.authors``
    is the ``ManyToManyField`` and this is its ``through``.

    ``user`` is ``PROTECT``, deliberately unlike ``CaseStateChange.actor``
    (``SET_NULL``): there the actor is incidental to an internal log, so losing
    the user row may blank it. Here the name IS published content, so deleting a
    credited account must fail loudly rather than silently strip a byline off a
    live case.
    """

    case = models.ForeignKey(
        "Case",
        on_delete=models.CASCADE,
        related_name="author_credits",
        help_text="The case this author credit belongs to",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="case_authorships",
        help_text="The Django account credited as an author of this case",
    )
    ordinal = models.PositiveIntegerField(
        default=0,
        help_text="Display order of this author within the case byline.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this author credit was created",
    )

    class Meta:
        verbose_name = "Case Author"
        verbose_name_plural = "Case Authors"
        ordering = ["ordinal", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "user"],
                name="unique_case_author",
            )
        ]
        indexes = [
            models.Index(fields=["case", "ordinal"], name="case_author_ordinal_idx"),
        ]

    def __str__(self):
        return f"{self.case.slug} - {self.user_id}"

    def save(self, *args, **kwargs):
        """Guarantee the credited account has a profile (and therefore a slug)."""
        super().save(*args, **kwargs)
        AuthorProfile.ensure_for(self.user)


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
    BANKING_OFFENCE = "BANKING_OFFENCE", "Banking Offence"


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


class CaseQuerySet(models.QuerySet):
    """Case queryset that records slug history on ANY bulk ``.update(slug=…)``.

    ``Case.save()`` records a slug change (BB-38), but the model's ``slug`` is
    immutable once the case leaves DRAFT — so re-slugging a PUBLISHED case is
    done operationally with a bulk ``QuerySet.update(slug=…)``, which bypasses
    ``save()`` and its history hook entirely. The API's DRAFT re-slug PATCH also
    persists via ``update()``. Recording here makes the bulk-update path the
    single durable choke point: whatever route re-slugs a case — the API PATCH,
    an admin action, or an ad-hoc pod ORM edit — the retired slug is remembered
    and its URL 301-redirects instead of hard-404ing.

    History migrations use ``apps.get_model()`` historical models with plain
    managers, so schema migrations that touch ``slug`` do NOT record history
    (correctly — they run before the redirect feature existed) and cannot
    recurse through this override.
    """

    def update(self, **kwargs):
        # Only slug changes are of interest; every other bulk update (state
        # transitions, the updated_at bump, enrichment writes, …) takes the
        # cheap path with no extra query.
        if "slug" not in kwargs:
            return super().update(**kwargs)

        new_slug = kwargs["slug"]
        # Only a concrete string slug can be retired. A query expression
        # (``F()``, the ``Case``/``When`` that Django's own ``bulk_update()``
        # builds, a subquery, …) has no Python-side value to record, so fall
        # through to a plain update rather than feeding an expression object
        # into ``record()``.
        if not isinstance(new_slug, str):
            return super().update(**kwargs)

        # Snapshot the affected cases (with their PRE-update slugs) before the
        # write. Rows already at ``new_slug`` are excluded — nothing retires.
        # ``slug`` is globally unique on Case, so a slug update targets at most
        # one row in practice; the loop is written for correctness regardless.
        # Only pk + slug are needed to record history; ``.only()`` avoids loading
        # the case's large text columns (description, notes, timeline, …).
        changing = list(self.only("id", "slug").exclude(slug=new_slug))
        result = super().update(**kwargs)
        # ``result`` is the count of rows actually updated. If a concurrent
        # delete raced the snapshot to zero, record nothing — the snapshot is
        # stale and its case may no longer exist (a dangling FK insert would
        # fail).
        if result:
            for case in changing:
                # ``update()`` does not touch the in-memory instances, so
                # ``case.slug`` still holds the retired value here.
                CaseSlugHistory.record(case, case.slug, new_slug)
        return result


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

    # ``CaseQuerySet.update()`` records slug history for any bulk re-slug
    # (published-case re-slugs and the API's DRAFT PATCH both use ``update()``).
    # ``as_manager()`` has ``use_in_migrations = False``, so this adds no
    # migration and historical models keep their plain manager.
    objects = CaseQuerySet.as_manager()

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
    thumbnail_url = HttpsURLField(
        blank=True,
        max_length=500,
        help_text="URL to a small thumbnail picture for the case",
    )
    banner_url = HttpsURLField(
        blank=True, max_length=500, help_text="URL to a large banner image for the case"
    )
    # Date fields
    case_start_date = models.DateField(
        null=True, blank=True, help_text="When the alleged incident began"
    )
    case_end_date = models.DateField(
        null=True, blank=True, help_text="When the alleged incident ended"
    )
    # The date the case was FIRST published on jawafdehi.org — about our
    # publication, not about the alleged incident (case_start_date/case_end_date
    # above). Nullable at the column so DRAFTs can exist without one; required
    # before a case may leave DRAFT (see validate()). Deliberately NOT derived
    # from created_at or from the first PUBLISHED CaseStateChange: cases are
    # routinely published here long after the research was done, and the state
    # log only goes back to 2026-07. Caseworker-editable for exactly that reason.
    case_publish_date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Date this case was first published on jawafdehi.org (AD). Set by "
            "casework staff and required before the case can leave DRAFT."
        ),
    )

    # Entity relationships live on the CaseEntityRelationship bind (the
    # ``entity_relationships`` reverse relation), which holds the canonical NES
    # entity id (nes_id) directly. There is no M2M to a local entity table —
    # NES is the single source of truth for entities.

    # The public byline, as an ORDERED many-to-many: the through table carries
    # only the ordinal, and everything else about an author (name, photo, bio,
    # links) is per-person on AuthorProfile. Declared here — rather than relying
    # on the reverse ``author_credits`` alone — so the reverse accessor
    # ``user.authored_cases`` exists, which is exactly the query the public
    # author page runs. NOT ordered by default: an M2M ignores the through
    # model's Meta.ordering, so every read that cares sorts by
    # ``caseauthor__ordinal`` (see the ``author_ids`` property and the
    # serializers).
    authors = models.ManyToManyField(
        User,
        through="CaseAuthor",
        related_name="authored_cases",
        blank=True,
        help_text="Credited authors of this case, ordered by CaseAuthor.ordinal",
    )

    # Content fields
    tags = TextListField(blank=True, help_text="List of tags for categorization")
    # The pre-cleanup snapshot of ``tags``, written ONCE by ``rebuild_case_tags``
    # before it rewrites ``tags`` into canonical vocabulary ids.
    #
    # ``tags`` currently holds whatever a caseworker or the enricher typed -- 144
    # distinct values across 82 cases, including seven spellings of "illicit
    # enrichment", 21 money amounts and a handful of people's names. Canonicalising
    # it is lossy on purpose, so the original is kept here rather than discarded:
    # it is the rollback path (``tags`` recomputes from this plus the alias table,
    # so the migration is re-runnable and reversible) and the audit trail for what
    # a value USED to be. This is design.md §12 step 7 -- "preserve the original
    # value only when needed for source display".
    #
    # NULL means "never snapshotted": either the case predates the rebuild, or it
    # was created after it and never had free-text tags. Both are correct; do not
    # read NULL as "not yet migrated".
    #
    # NOT part of any public serializer -- it holds the person and organisation
    # names deliberately removed from ``tags``. The serializers list their fields
    # explicitly, so it stays out by default; keep it that way.
    tags_source = models.JSONField(
        null=True,
        blank=True,
        editable=False,
        help_text="Pre-canonicalisation snapshot of tags. Audit trail, not public.",
    )
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

    # v3 authz model: the per-case `contributors` M2M (object-level assignment
    # gating) is retired — the single content-staff role can edit any case.

    # Metadata
    versionInfo = models.JSONField(
        default=dict, blank=True, help_text="Version metadata tracking changes"
    )

    # Timestamps
    # NOTE: the list endpoint orders by ``-created_at`` on every request, but
    # the cases table is small (hundreds–few thousand rows) so the sort is
    # sub-millisecond and an index buys ~nothing today — the endpoint's speed
    # comes from the anon cache + batched entity resolution, not from here. If
    # the table grows, add a *composite* index matching the hot public query
    # (``WHERE state='PUBLISHED' ORDER BY created_at DESC``):
    #   indexes = [models.Index(fields=["state", "-created_at"])]
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Notes field (markdown supported, internal use)
    notes = models.TextField(
        blank=True,
        default="",
        help_text="Internal notes about the case (markdown supported)",
    )
    # Public counterpart to ``notes``. Unlike ``notes`` (internal, BB-04-gated on
    # read), this IS returned to everyone.
    #
    # SUPERSEDED, kept for one release as a rendering fallback. This single free
    # text field used to carry the whole byline — attribution AND the "first
    # published / last edited" line. That is now three structured fields:
    # ``author_credits`` (who), ``case_publish_date`` (when it went live) and
    # ``public_edit_history`` (what changed since). The frontend renders the
    # structured byline when a case has authors and falls back to this text
    # otherwise, so the ~72 legacy cases keep their byline until they are
    # backfilled. Nothing new should be written here; it is dropped once the
    # backfill lands.
    public_notes = models.TextField(
        blank=True,
        default="",
        help_text="DEPRECATED (superseded by authors / case_publish_date / "
        "public_edit_history): free-text public byline, still rendered as a "
        "fallback on cases that have no structured authors yet.",
    )
    # The public, caseworker-curated edit log: ``[{date, remarks}]``, e.g.
    # "2026-08-14 — corrected the bigo figure after the Special Court order".
    # Distinct from CaseStateChange, which is machine-written, carries moderator
    # names and send-back reasons, and is gated for every state including
    # PUBLISHED. A JSON list (like ``timeline``) rather than its own table: it
    # has no per-row permissions or authorship, and this way it rides the
    # existing RFC-6902 patch path and the If-Match lock unchanged.
    public_edit_history = EditHistoryListField(
        help_text=(
            "Public edit history shown on the case page: a list of "
            "{date, remarks} entries, newest handling at the caseworker's "
            "discretion. Empty = nothing rendered."
        ),
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
    # Plain unbounded integer rather than fractional/gap positioning: there is no
    # drag-to-reorder UI, staff just type a number.
    weight = models.IntegerField(
        default=0,
        help_text=(
            "Editorial priority weight — higher values surface first in the "
            "homepage 'Featured Cases' section. 0 (the default) means unranked; "
            "ties fall back to newest-first."
        ),
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

    # Same contract as ``_pending_court_cases`` above, for the CaseAuthor join.
    _pending_authors = None

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
    def author_ids(self):
        """Credited account ids in byline order (or the pending assignment).

        The byline's only per-case fact is ORDER, so this is a plain ordered list
        of user ids — that is the whole writable surface. Display details resolve
        from each author's ``AuthorProfile``.
        """
        if self._pending_authors is not None:
            return list(self._pending_authors)
        if self.pk is None:
            return []
        return [credit.user_id for credit in self.author_credits.all()]

    @author_ids.setter
    def author_ids(self, value):
        if value is None:
            value = []
        if not isinstance(value, (list, tuple)):
            raise ValidationError("authors must be a list")
        ids = []
        for entry in value:
            # Accept a bare id or a {"user_id": N} object, so a client can echo
            # back the read shape without reshaping it first.
            user_id = entry.get("user_id") if isinstance(entry, dict) else entry
            if user_id is None:
                raise ValidationError("Each author must carry a user_id")
            try:
                user_id = int(user_id)
            except (TypeError, ValueError):
                raise ValidationError(f"Invalid author id: {user_id!r}")
            # A duplicate is dropped rather than rejected: the unique constraint
            # would otherwise turn a double-click in the picker into a 500
            # mid-rewrite.
            if user_id not in ids:
                ids.append(user_id)
        self._pending_authors = ids

    def _sync_author_credits(self, desired=None):
        """Persist the byline order to the CaseAuthor through table.

        THE single write path for the join, with the same replace semantics and
        unchanged-is-a-no-op contract as ``_sync_courtcase_references``. Rows are
        created one at a time (not ``bulk_create``) so ``CaseAuthor.save()``
        runs and every credited account is guaranteed an ``AuthorProfile``.
        """
        if desired is None:
            desired = self._pending_authors
        if desired is None:
            # Nothing assigned and nothing passed — no write intent.
            return
        self._pending_authors = None
        normalized = []
        for entry in desired:
            user_id = entry.get("user_id") if isinstance(entry, dict) else entry
            user_id = int(user_id)
            if user_id not in normalized:
                normalized.append(user_id)
        current = [credit.user_id for credit in self.author_credits.all()]
        if normalized == current:
            return
        with transaction.atomic():
            self.author_credits.all().delete()
            for ordinal, user_id in enumerate(normalized):
                CaseAuthor(case=self, user_id=user_id, ordinal=ordinal).save()
        if hasattr(self, "_prefetched_objects_cache"):
            self._prefetched_objects_cache.pop("author_credits", None)

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

        # Record a slug change (old → new) so the retired slug's URL can
        # 301-redirect to the canonical one (BB-38). ``_original_slug`` still
        # holds the pre-save value here (it is refreshed on the next line);
        # brand-new cases (no prior slug) and no-op saves are ignored by
        # ``record()``. NOTE: the API's DRAFT re-slug PATCH persists via a bulk
        # ``QuerySet.update()`` that bypasses ``save()`` — that path records
        # history explicitly in ``CaseViewSet.partial_update``.
        previous_slug = getattr(self, "_original_slug", None)
        if previous_slug and previous_slug != self.slug:
            CaseSlugHistory.record(self, previous_slug, self.slug)

        # Update cached original slug after successful save
        self._original_slug = self.slug

        # Persist any pending court_cases assignment to the join table now
        # that the row (and pk) exist.
        if self._pending_court_cases is not None:
            self._sync_courtcase_references()

        # Same, for a pending `authors` assignment.
        if self._pending_authors is not None:
            self._sync_author_credits()

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
            # Every case type requires a named SUBJECT — at least one non-location
            # entity (a person or organization) — before it can leave DRAFT. The
            # former CORRUPTION-only "at least one ACCUSED" hard gate is retired:
            # systemic / unsubstantiated cases (e.g. a project-level irregularity
            # with no charged individual, like budhigandaki) must be publishable,
            # so the requirement is a named subject, not specifically an accused
            # party. Naming an accused is still tracked as a review-quality signal
            # (see cases.models.requires_accused / review.rules_engine), just no
            # longer a publish blocker. A location-only case is not a valid subject
            # (the UI also excludes locations when naming a case's subject).
            has_required_entity = self.entity_relationships.exclude(
                relationship_type=RelationshipType.LOCATION
            ).exists()
            if not has_required_entity:
                errors["entities"] = (
                    "At least one non-location entity is required for IN_REVIEW or PUBLISHED state"
                )

            if not self.key_allegations or len(self.key_allegations) == 0:
                errors["key_allegations"] = (
                    "At least one key allegation is required for IN_REVIEW or PUBLISHED state"
                )

            if not self.description or not self.description.strip():
                errors["description"] = (
                    "Description is required for IN_REVIEW or PUBLISHED state"
                )

            # A case may not go public unattributed. Reads the ``author_ids``
            # property, not the reverse manager, so it also sees a pending
            # assignment on an unsaved instance and returns [] (rather than
            # raising) when there is no pk yet.
            if not self.author_ids:
                errors["authors"] = (
                    "At least one author is required for IN_REVIEW or PUBLISHED state"
                )

            # Required from IN_REVIEW onward, same as the rules above. At submit
            # this is the INTENDED publication date and stays editable; the point
            # is that no case reaches the public site without one, since the
            # alternative is the machine created_at, which is routinely wrong by
            # months for cases researched long before they were entered here.
            if not self.case_publish_date:
                errors["case_publish_date"] = (
                    "A publish date is required for IN_REVIEW or PUBLISHED state"
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
    # A member of the public reporting alleged corruption, as opposed to
    # feedback about the platform. Handled differently on submission: the
    # reporter's IP and user agent are never stored (see FeedbackView.post).
    CASE_REPORT = "case_report", "Corruption Case Report"


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


class CaseStateChange(models.Model):
    """Append-only log of a case's workflow transitions (DRAFT/IN_REVIEW/
    PUBLISHED/CLOSED), with the actor and an optional human reason.

    Why a dedicated table rather than reusing ``Case.versionInfo`` or auditlog:
      - ``versionInfo`` holds only the *latest* action (it is overwritten on
        every transition) and carries no actor and no reason — so it can't
        answer "who sent my case back to draft, and why".
      - auditlog ``LogEntry`` captures the field diff + actor but has no place
        for a moderator's free-text reason, and isn't API-exposed.

    This log is the source for the case author's feedback loop (a moderator's
    "send back to draft" reason travels via the ``X-Transition-Reason`` request
    header and lands in ``reason`` here) and for the case history panel. It is
    written from the PATCH state-transition path; rows are never mutated or
    deleted (the case itself is only ever soft-closed).
    """

    case = models.ForeignKey(
        Case, on_delete=models.CASCADE, related_name="state_changes"
    )
    # from_state may be blank for the very first recorded transition if the
    # prior state was somehow unknown; in practice it is always set.
    from_state = models.CharField(
        max_length=64, choices=CaseState.choices, blank=True, default=""
    )
    to_state = models.CharField(max_length=64, choices=CaseState.choices)
    # SET_NULL (not CASCADE): losing the actor's user row must never erase the
    # transition record — the history is about the case, not the user.
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="case_state_changes",
    )
    # Optional free-text reason (e.g. why a submission was sent back or closed).
    # Never published; internal to casework, same trust boundary as notes.
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Case State Change"
        verbose_name_plural = "Case State Changes"
        # Newest-first is the display order for the history panel; also the
        # index the per-case history query rides.
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["case", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.case_id}: {self.from_state or '∅'} → {self.to_state}"


class CaseSlugHistory(models.Model):
    """Retired case slugs → their current case, for 301-redirecting stale URLs.

    A case's ``slug`` is its public URL handle (the canonical ``@id`` IRI is
    derived from it). It is editable while DRAFT (regenerates from the title),
    and published cases are also re-slugged operationally — so a slug that was
    already shared, newslettered, or search-indexed can go stale and hard-404
    (BB-38). Each row remembers that ``slug`` USED to address ``case``; the
    case-retrieve path consults this table on a lookup miss and 301-redirects
    the caller to the case's current canonical URL.

    Invariants (maintained by :meth:`record`):
      * A LIVE slug always wins. The retrieve path resolves a live case first
        and only falls back to this table on a 404, so a slug currently owned
        by some case never redirects; :meth:`record` additionally drops any
        history row that collides with a newly-claimed live slug, so the table
        never shadows a live slug.
      * ``slug`` is globally unique here, and a recycled slug repoints to its
        most-recent former owner (update-or-create), so the redirect always
        targets the latest case that vacated the slug.
    """

    # Mirrors Case.slug (SlugField, max_length=50). Unique so a retired slug
    # maps to exactly one former owner; indexed for the retrieve-path lookup.
    slug = models.SlugField(max_length=50, unique=True, db_index=True)
    # CASCADE: a redirect target that no longer exists is useless, so history
    # rows die with their case (cases are only ever soft-CLOSED, never deleted,
    # in normal operation — this is a safety net, not a routine path).
    case = models.ForeignKey(
        Case, on_delete=models.CASCADE, related_name="slug_history"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Case Slug History"
        verbose_name_plural = "Case Slug Histories"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.slug} → {self.case_id}"

    @classmethod
    def record(cls, case, old_slug, new_slug):
        """Remember that ``old_slug`` used to address ``case`` (now ``new_slug``).

        No-ops when there is nothing to record (no prior slug, or the slug did
        not actually change). Upholds the "live slug wins" invariant by dropping
        any history row that collides with the new (now-live) slug, then upserts
        ``old_slug → case`` so a recycled slug repoints to its newest former
        owner rather than tripping the unique constraint.
        """
        if not old_slug or old_slug == new_slug:
            return
        # The new slug is now LIVE for ``case``; never let a stale history row
        # shadow it with a redirect (a live slug must resolve to its own case).
        if new_slug:
            cls.objects.filter(slug=new_slug).delete()
        cls.objects.update_or_create(slug=old_slug, defaults={"case": case})
