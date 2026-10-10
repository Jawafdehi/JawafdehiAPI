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
      sheets, precedents, ...) that are public on their own merits — and now also
      the default for case uploads (see ``default_policy_for``), which are sourced
      by their material_type rather than the legacy ``jawafdehi`` bucket.
    * ``CASE_GATED`` — visibility tracks the citing cases (the historical rule):
      not public until a case reaches in-review/published. No longer a birth
      default; a caseworker sets it explicitly (via the materials PATCH) to
      embargo a document until its case is vetted + published. It also still
      governs any residual ``jawafdehi``-sourced rows.
    * ``PRIVATE``    — always ``PRIVATE``: an absolute withhold for a sensitive
      source, even after the citing case is published.
    """

    PUBLIC = "PUBLIC", "Public"
    CASE_GATED = "CASE_GATED", "Case-gated"
    PRIVATE = "PRIVATE", "Private"


def default_policy_for(source: str) -> str:
    """The visibility policy a freshly-ingested material is born with.

    Every non-``jawafdehi`` material is born ``PUBLIC``. Case uploads are now
    sourced by their ``material_type`` (news → ``news``, a misc upload →
    ``document``, …), so they too are born ``PUBLIC`` — a case-attached document
    is public on ingest, and a caseworker embargoes a sensitive one by explicitly
    setting ``CASE_GATED``/``PRIVATE`` via the materials PATCH. The legacy
    ``jawafdehi`` source is retained here as ``CASE_GATED`` for any residual rows
    in that historical namespace, but new uploads never land there.
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


# ── Extracted document data ──────────────────────────────────────────────────
#
# A ``Material`` is the *published document*: its JSON-LD metadata plus, where we
# have it, a flat transcript. These four models carry the STRUCTURE recovered
# from inside that document — the ruled tables and the charts, with the charts'
# underlying series — as rows rather than prose.
#
# They are deliberately generic: nothing here knows about the CIAA. The
# CIAA-specific part (which upstream document maps to which material) lives in
# the ingest command, not in the schema, so a second corpus needs no migration.
#
# All four live in the ``ngm`` database alongside ``Material`` (see
# config.db_router), so the FKs below never cross a database.


class DocumentExtraction(models.Model):
    """One extraction pass over one material's source document.

    Exists to hang provenance off: *which* upstream dataset produced these rows,
    at which revision, and what that dataset says about its own quality. The
    tables and figures below cascade from it, so a re-ingest is a delete of this
    row plus a fresh insert — there is no partial-update path to get wrong.
    """

    # PK *is* the material: one extraction per document, and the join key is the
    # IRI the rest of the platform already uses.
    material = models.OneToOneField(
        "materials.Material",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="extraction",
        db_column="material_iri",
    )
    # The upstream dataset's own id for this document (e.g. ``ciaa-2074-75``).
    # Recorded so a row can be traced back to its source without the mapping file.
    doc_id = models.CharField(max_length=120, db_index=True)
    # Dataset coordinates, e.g. ``damo-da/ciaa-annual-reports`` + a commit sha.
    # Blank revision is tolerated: not every source is a content-addressed repo.
    dataset = models.CharField(max_length=200)
    dataset_revision = models.CharField(max_length=80, blank=True)
    # What the upstream says about the document and its own confidence. Free-form
    # on purpose — these are another project's vocabularies, and pinning them to
    # a TextChoices here would turn their next added verdict into our 500.
    page_count = models.PositiveIntegerField(default=0)
    text_source = models.CharField(max_length=40, blank=True)
    transcript_verdict = models.CharField(max_length=40, blank=True)
    figures_verdict = models.CharField(max_length=40, blank=True)
    ingested_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "material_document_extractions"

    def __str__(self) -> str:
        return f"{self.doc_id} ({self.material_id})"


class ExtractedTable(models.Model):
    """One ruled table lifted out of the document, kept as markdown.

    Markdown rather than cells: the published artefact is a *rendered* table, and
    a cell grid would be a second representation to keep in sync for no reader
    benefit. The largest single table in the CIAA corpus is ~170 KiB, which is
    why the manifest endpoint omits this column and a table is fetched on demand.
    """

    extraction = models.ForeignKey(
        DocumentExtraction, on_delete=models.CASCADE, related_name="tables"
    )
    # 1-based position within the document — presentation order ONLY. It is
    # recomputed on every ingest, so a revision that recovers a table missed in
    # the middle of a report shifts every later one. Never address a table by it.
    ordinal = models.PositiveIntegerField()
    # The stable, URL-safe handle: ``p0018-t1`` (page 18, first table on it).
    # Derived from coordinates that describe where the table physically IS, so it
    # survives the upstream gaining or losing tables elsewhere in the document —
    # which ``ordinal`` does not, and which would otherwise make a saved link
    # silently resolve to a different table. The upstream ``uid`` would do as
    # well but carries a ``#`` that cannot sit in a path unescaped.
    key = models.CharField(max_length=40)
    uid = models.CharField(max_length=160)
    page_no = models.PositiveIntegerField()
    index_on_page = models.PositiveSmallIntegerField(default=1)
    # The prose introducing the table, where it has any — frequently a real
    # caption ("तालिका २.१ …"), frequently just a trailing colon. Long: the
    # observed maximum is ~1.3 kB.
    caption = models.TextField(blank=True)
    # The header row as a list of strings. Ragged and repetitive in the source
    # (merged header cells arrive as empty strings), so it is stored as-is
    # rather than normalised into something that would lose the column count.
    header = models.JSONField(default=list, blank=True)
    n_rows = models.PositiveIntegerField(default=0)
    n_cols = models.PositiveIntegerField(default=0)
    # How the upstream rates this table's fidelity (``tight`` / ``ocr_vision``).
    fidelity = models.CharField(max_length=40, blank=True)
    markdown = models.TextField()

    class Meta:
        db_table = "material_extracted_tables"
        ordering = ("ordinal",)
        constraints = [
            models.UniqueConstraint(
                fields=["extraction", "ordinal"], name="uniq_extracted_table_ordinal"
            ),
            models.UniqueConstraint(
                fields=["extraction", "key"], name="uniq_extracted_table_key"
            ),
            models.UniqueConstraint(
                fields=["extraction", "uid"], name="uniq_extracted_table_uid"
            ),
        ]

    def __str__(self) -> str:
        return self.uid


class ExtractedFigure(models.Model):
    """One chart found in the document, with its series in ``points``."""

    extraction = models.ForeignKey(
        DocumentExtraction, on_delete=models.CASCADE, related_name="figures"
    )
    # Presentation order only — see the note on ExtractedTable.ordinal.
    ordinal = models.PositiveIntegerField()
    # Stable handle, ``p0018-f1``. Figures ride inline in the manifest rather
    # than having a route of their own, but a client still needs something
    # durable to anchor or deep-link a chart to.
    key = models.CharField(max_length=40)
    uid = models.CharField(max_length=160)
    page_no = models.PositiveIntegerField()
    index_on_page = models.PositiveSmallIntegerField(default=1)
    title = models.TextField(blank=True)
    # Upstream's own chart vocabulary, which is wider and less tidy than an enum
    # would be (``pie``, ``pie_3d``, ``stacked bar`` AND ``stacked_bar``). Kept
    # verbatim; normalising is a presentation decision, not a storage one.
    chart_type = models.CharField(max_length=40, blank=True)
    unit = models.CharField(max_length=80, blank=True)
    x_axis = models.CharField(max_length=300, blank=True)
    y_axis = models.CharField(max_length=300, blank=True)
    # The transcriber's reasoning about this chart — how a disputed value was
    # resolved, which total it reconciles against. Long (observed ~4.8 kB) and
    # worth keeping: it is the audit trail behind every number in ``points``.
    notes = models.TextField(blank=True)
    verify_note = models.TextField(blank=True)
    # Upstream's verification flag. Nullable because "not checked" and "checked
    # and found wanting" are different claims and the UI renders them differently.
    verified = models.BooleanField(null=True, blank=True)

    class Meta:
        db_table = "material_extracted_figures"
        ordering = ("ordinal",)
        constraints = [
            models.UniqueConstraint(
                fields=["extraction", "ordinal"], name="uniq_extracted_figure_ordinal"
            ),
            models.UniqueConstraint(
                fields=["extraction", "key"], name="uniq_extracted_figure_key"
            ),
            models.UniqueConstraint(
                fields=["extraction", "uid"], name="uniq_extracted_figure_uid"
            ),
        ]

    def __str__(self) -> str:
        return self.uid


class ExtractedFigurePoint(models.Model):
    """One datum of one chart: a value, its label, and which series it belongs to.

    ``is_estimated`` is the load-bearing field. A point read off a chart image is
    not the same claim as one read from a printed figure, and the two must never
    be presented as equivalent — the API returns it on every point and the UI is
    expected to show it.
    """

    figure = models.ForeignKey(
        ExtractedFigure, on_delete=models.CASCADE, related_name="points"
    )
    point_index = models.PositiveIntegerField()
    label = models.CharField(max_length=300, blank=True)
    series = models.CharField(max_length=300, blank=True)
    # Nullable: a chart can name a category it prints no value for.
    value = models.FloatField(null=True, blank=True)
    is_estimated = models.BooleanField(default=False)

    class Meta:
        db_table = "material_extracted_figure_points"
        ordering = ("point_index", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["figure", "point_index"], name="uniq_figure_point_index"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.figure_id}#{self.point_index}"
