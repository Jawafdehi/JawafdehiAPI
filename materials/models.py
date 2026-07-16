"""The ``Material`` representation: a stored schema.org JSON-LD document.

A material is any CreativeWork-family NGM document (court order/verdict/
manuscript, charge sheet, legal corpus item, official report, or a court-case
record). Mirroring the NES entity remodel, the canonical stored form is the
JSON-LD document itself (``data``), keyed by a material ``@id`` IRI (``iri``,
the PK + join key). Promoted columns (``material_type``, ``source``, ``ident``)
are derived from the IRI/JSON-LD for routing + filtering.

Clean slate: no legacy ids. ``iri`` is the only identity.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from jawafdehi_shared.entities.ids import (
    JAWAF_SOURCE,
    MAX_IRI_LENGTH,
    is_valid_material_iri,
    parse_material_iri,
)

from .jsonld import validate_material_jsonld


def validate_material_iri(value: str) -> None:
    """Field validator: ``iri`` must be a canonical material ``@id`` IRI."""
    if not is_valid_material_iri(value):
        raise ValidationError(
            f"{value!r} is not a valid material @id IRI "
            "(expected https://<base>/material/<source>/<ident>)."
        )


class Visibility(models.TextChoices):
    """Cached publication tier for a Material (ADR: cases own no documents).

    ``visibility`` is the DERIVED, cached result that every anon-facing consumer
    (sitemaps, unified search, retrieve endpoint) keys on. It is computed from the
    material's ``visibility_policy`` (the caseworker-controlled INPUT — see
    :class:`Policy`) by ``materials.visibility.recompute_material_visibility``:

    * ``LISTED``   — public, searchable, in sitemaps.
    * ``UNLISTED`` — reachable by direct IRI, but NOT searchable / NOT in sitemaps.
    * ``PRIVATE``  — not public at all; authed caseworker/readonly only.

    How ``visibility_policy`` maps here: ``PUBLIC`` → always ``LISTED``;
    ``PRIVATE`` → always ``PRIVATE``; ``CASE_GATED`` → the MAX over the states of
    the cases that cite the material as evidence (PUBLISHED→LISTED,
    IN_REVIEW→UNLISTED, DRAFT/CLOSED/none→PRIVATE — YouTube-unlisted semantics).

    Default is ``LISTED`` so a freshly-inserted row is public until the recompute
    settles it (corpus materials are born ``PUBLIC`` and stay ``LISTED``).
    """

    LISTED = "LISTED", "Listed"
    UNLISTED = "UNLISTED", "Unlisted"
    PRIVATE = "PRIVATE", "Private"


class Policy(models.TextChoices):
    """Caseworker-controlled visibility policy — the INPUT that determines a
    material's cached :class:`Visibility` (ADR: cases own no documents).

    Separates a document's intrinsic publicness from the publication state of the
    case that happens to cite it, so a DRAFT case can no longer hide an
    already-public document:

    * ``PUBLIC``     — always ``LISTED``, regardless of any citing case's state.
      The default for corpus materials (court orders, press releases, charge
      sheets, precedents, ...) that are public on their own merits.
    * ``CASE_GATED`` — visibility tracks the citing cases (the historical rule):
      not public until a case reaches in-review/published. The default for
      case-UPLOADED evidence (``source == jawafdehi``), so raw uploads attached to
      a draft are not exposed until a caseworker vets + publishes (or opts the
      material into ``PUBLIC``).
    * ``PRIVATE``    — always ``PRIVATE``: an absolute withhold for a sensitive
      source, even after the citing case is published.
    """

    PUBLIC = "PUBLIC", "Public"
    CASE_GATED = "CASE_GATED", "Case-gated"
    PRIVATE = "PRIVATE", "Private"


def default_policy_for(source: str) -> str:
    """The visibility policy a freshly-ingested material is born with.

    Keyed on ``source`` (not ``material_type``): a case-uploaded document is
    minted at ``/material/jawafdehi/<ident>`` (``JAWAF_SOURCE``) regardless of the
    ``material_type`` the caseworker picked, so ``source`` is the reliable signal
    that a document was uploaded THROUGH a case (embargo by default) versus a
    corpus document that exists on its own merits (public by default). A
    caseworker can override either default per material (see the materials PATCH).
    """
    return Policy.CASE_GATED if source == JAWAF_SOURCE else Policy.PUBLIC


#: Visibility tiers a member of the public (anon) may retrieve by direct IRI.
#: PRIVATE is authed-only. Sitemaps/search expose LISTED only (see consumers).
PUBLIC_VISIBILITIES = (Visibility.LISTED, Visibility.UNLISTED)


class Material(models.Model):
    """A schema.org JSON-LD material document, keyed by its ``@id`` IRI.

    ``data`` is the full JSON-LD (``@context``/``@type``/``@id`` + properties) —
    the canonical served form. ``material_type`` / ``source`` / ``ident`` are
    promoted from the IRI + JSON-LD for routing and filtering. The relational
    court tables remain the projection for cases/parties; ``Material`` is the
    published-document representation (and where court-case JSON-LD is
    materialized via ``materials.jsonld.court_case_to_jsonld``).
    """

    # The canonical @id IRI is the primary key + cross-surface join key. Width is
    # pinned to the shared MAX_IRI_LENGTH so it matches the other join-key columns
    # (NGM/Jawafdehi nes_id) and never exceeds what a consumer can store.
    iri = models.CharField(
        primary_key=True, max_length=MAX_IRI_LENGTH, validators=[validate_material_iri]
    )
    # The schema.org/material classification token (see jsonld.MaterialType).
    material_type = models.CharField(max_length=40, db_index=True)
    # Derived from the IRI for routing/filtering (`/material/<source>/<ident>`).
    source = models.CharField(max_length=120, db_index=True)
    ident = models.CharField(max_length=300, db_index=True)
    # The full schema.org JSON-LD document.
    data = models.JSONField()
    # Soft-delete flag (accountability platform: rows are never hard-deleted).
    # Reads (list/detail) exclude ``is_deleted=True`` rows; DELETE flips it True.
    is_deleted = models.BooleanField(default=False, db_index=True)
    # Caseworker-controlled visibility policy (see Policy) — the INPUT the
    # recompute maps to ``visibility``. Default PUBLIC so corpus materials are
    # public as before; case-uploaded evidence is born CASE_GATED at ingest (see
    # default_policy_for / the upsert primitive). A re-ingest never clobbers this
    # (create_defaults, INSERT-only); a caseworker changes it via the PATCH.
    visibility_policy = models.CharField(
        max_length=12,
        choices=Policy.choices,
        default=Policy.PUBLIC,
        db_index=True,
    )
    # Cached, derived publication tier (see Visibility). Computed from
    # ``visibility_policy`` by materials.visibility.recompute_material_visibility.
    # The anon-facing consumers (sitemaps, unified search, retrieve endpoint) key
    # on THIS column, so it MUST be kept in sync or a draft case's evidence leaks.
    visibility = models.CharField(
        max_length=10,
        choices=Visibility.choices,
        default=Visibility.LISTED,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "materials"
        indexes = [
            models.Index(fields=["source", "material_type"]),
        ]

    def __str__(self) -> str:
        return self.iri

    def clean(self) -> None:
        """Validate the IRI, the promoted columns' agreement with it, and the
        JSON-LD doc (lightweight: known @type, valid @id, name present)."""
        super().clean()
        if not is_valid_material_iri(self.iri):
            raise ValidationError({"iri": "not a valid material @id IRI"})
        parsed = parse_material_iri(self.iri)
        if self.source != parsed.source or self.ident != parsed.ident:
            raise ValidationError(
                "source/ident must match the iri's /material/<source>/<ident>."
            )
        try:
            validate_material_jsonld(self.data, iri=self.iri)
        except ValueError as exc:
            raise ValidationError({"data": str(exc)}) from exc

    @classmethod
    def from_jsonld(cls, data: dict, *, material_type: str) -> Material:
        """Build (unsaved) a ``Material`` from a JSON-LD doc, deriving the
        promoted ``source``/``ident`` columns from its ``@id`` and the source-based
        default ``visibility_policy``. Validates the doc (known @type, valid @id,
        name present).

        The policy here is the birth default only (corpus→PUBLIC,
        jawafdehi-upload→CASE_GATED); the upsert primitive keeps it INSERT-only so
        a re-upsert never clobbers a caseworker's manual policy on an UPDATE.
        """
        validate_material_jsonld(data)
        parsed = parse_material_iri(data["@id"])
        return cls(
            iri=data["@id"],
            material_type=material_type,
            source=parsed.source,
            ident=parsed.ident,
            data=data,
            visibility_policy=default_policy_for(parsed.source),
        )
