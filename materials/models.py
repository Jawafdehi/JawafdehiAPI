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
    """Derived publication tier for a Material (ADR: cases own no documents).

    A material's visibility is the MAX over the states of all cases that
    reference it as evidence (YouTube-unlisted semantics):

    * ``LISTED``   — ≥1 PUBLISHED case references it (or it's an NGM-native
      material with no case referrers): public, searchable, in sitemaps.
    * ``UNLISTED`` — only IN_REVIEW referrers: reachable by direct IRI, but NOT
      searchable and NOT in sitemaps.
    * ``PRIVATE``  — only DRAFT/CLOSED referrers (or none, for a source-only
      draft): not public at all; authed caseworker/readonly only. (CLOSED is the
      case soft-delete tombstone, so a deleted case cannot keep evidence public.)

    Default is ``LISTED`` so NGM-native materials (court cases/orders, bulk
    ingest) are unaffected — only case-source materials get demoted by the
    recompute path.
    """

    LISTED = "LISTED", "Listed"
    UNLISTED = "UNLISTED", "Unlisted"
    PRIVATE = "PRIVATE", "Private"


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
    # Derived publication tier (see Visibility). Default LISTED so NGM-native
    # materials are public as before; case-source materials are demoted to
    # UNLISTED/PRIVATE by the recompute path when only draft/in-review cases
    # reference them. The anon-facing consumers (sitemaps, unified search,
    # retrieve endpoint) MUST honor this or a draft case's evidence leaks.
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
        promoted ``source``/``ident`` columns from its ``@id``. Validates the
        doc (known @type, valid @id, name present)."""
        validate_material_jsonld(data)
        parsed = parse_material_iri(data["@id"])
        return cls(
            iri=data["@id"],
            material_type=material_type,
            source=parsed.source,
            ident=parsed.ident,
            data=data,
        )
