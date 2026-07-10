"""The canonical @id-IRI corpus enumerator — ONE source of truth for "what is
public" on the platform.

The unified ``@id`` envelope drives BOTH public discovery surfaces (Sitemaps and
ResourceSync). Rather than let each generator re-derive "which resources exist
and are public", they both consume this single enumerator, so the two surfaces
can never disagree about the corpus.

Each record type yields a :class:`Resource` ``(iri, lastmod, type, jsonld_url)``:

    type        record (DB)                       canonical IRI
    ----------------------------------------------------------------------------
    entity      NES StoredEntity (nes)            /entity/<prefix>/<slug>
    material    NGM Material (ngm)                /material/<source>/<ident>
    courtcase   NGM CourtCase (ngm)               /courtcase/<court>/<case_number>
    case        Jawafdehi Case, PUBLISHED only    /case/<slug>

PUBLIC-ONLY GUARANTEE
---------------------
* Entities, materials and court-cases are public read planes, but each honors a
  soft-delete tombstone (``is_deleted``) and materials additionally gate on
  ``visibility``. This enumerator applies the SAME filters the read planes use —
  ``is_deleted=False`` for entities/court-cases, ``is_deleted=False`` +
  ``visibility=LISTED`` for materials — so a deleted/non-public row never lingers
  as a live ``<loc>`` in the public sitemap / ResourceSync.
* Cases are the ONE access-controlled type: ONLY ``state == PUBLISHED`` cases are
  public (they mint a ``public_iri``; drafts/in-review/closed return ``None``).
  The case enumerator filters ``state=PUBLISHED`` in the query AND skips any row
  whose ``public_iri`` is ``None`` (belt-and-suspenders against the published
  gate), mirroring the search indexer's CASE-ONLY-PUBLISHED rule.

ROUTER-CORRECT IN-PROCESS QUERIES
---------------------------------
Models are queried directly through the ORM; the platform DB router pins each to
its own database (``entities``→nes, ``courts``/``materials``→ngm, ``cases``→
default). There are no cross-DB joins — each type is enumerated independently.

The ``jsonld_url`` is the path to that resource's schema.org JSON-LD
representation where the platform serves one (entities, materials, court-cases);
``None`` where it does not (cases have no standalone JSON-LD endpoint today). It
feeds the ResourceSync ``<rs:ln rel="describedby">`` link.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

# Resource ``type`` tokens (match the unified-search vocabulary).
TYPE_ENTITY = "entity"
TYPE_MATERIAL = "material"
TYPE_COURTCASE = "courtcase"
TYPE_CASE = "case"

ALL_TYPES: tuple[str, ...] = (TYPE_ENTITY, TYPE_MATERIAL, TYPE_COURTCASE, TYPE_CASE)


@dataclass(frozen=True)
class Resource:
    """One public corpus member, keyed by its canonical ``@id`` IRI.

    * ``iri`` — the canonical jawafdehi.org IRI (also the public ``loc``).
    * ``lastmod`` — the record's ``updated_at`` (``None`` if unknown).
    * ``type`` — one of :data:`ALL_TYPES`.
    * ``jsonld_url`` — path to the schema.org JSON-LD describing the resource, or
      ``None`` when none is served (drives ResourceSync ``describedby``).
    """

    iri: str
    lastmod: datetime | None
    type: str
    jsonld_url: str | None = None


# ── per-type enumerators ─────────────────────────────────────────────────────
# Each is a generator so the corpus is streamed (never fully materialized) —
# important for the large-corpus / paginated-sitemap case.


def _iter_entities() -> Iterator[Resource]:
    """Every NES entity (all public). JSON-LD: ``/api/entities/<prefix>/<slug>``."""
    from entities.models import StoredEntity

    # Exclude soft-deleted rows: DELETE flips ``is_deleted=True`` and the entity
    # vanishes from every read plane (list/detail/search — ``entities.persistence
    # ._live()``). Its canonical @id must likewise drop out of the public sitemap /
    # ResourceSync, or a tombstoned resource keeps advertising a live ``<loc>``.
    qs = StoredEntity.objects.filter(is_deleted=False).values_list(
        "iri", "prefix", "slug", "updated_at"
    )
    for iri, prefix, slug, updated_at in qs.iterator():
        yield Resource(
            iri=iri,
            lastmod=updated_at,
            type=TYPE_ENTITY,
            jsonld_url=f"/api/entities/{prefix}/{slug}",
        )


def _iter_materials() -> Iterator[Resource]:
    """Every LISTED, live NGM material. JSON-LD: ``/api/materials/<source>/<ident>``.

    Only ``visibility=LISTED`` + non-deleted rows are enumerated for public
    Sitemaps/ResourceSync: UNLISTED (in-review-only) and PRIVATE (draft-only)
    case-source materials must NOT appear in the public discovery surface (ADR:
    cases own no documents). NGM-native materials default to LISTED, so their
    behavior is unchanged.
    """
    from materials.models import Material, Visibility

    qs = (
        Material.objects.filter(is_deleted=False, visibility=Visibility.LISTED)
        .values_list("iri", "source", "ident", "updated_at")
    )
    for iri, source, ident, updated_at in qs.iterator():
        yield Resource(
            iri=iri,
            lastmod=updated_at,
            type=TYPE_MATERIAL,
            jsonld_url=f"/api/materials/{source}/{ident}",
        )


def _iter_courtcases() -> Iterator[Resource]:
    """Every NGM court case (all public).

    The courtcase ``@id`` IRI (``/courtcase/<court>/<case_number>``) identifies
    the court-case ROW; its schema.org JSON-LD lives under the corresponding
    MATERIAL IRI (``/material/court/<court>.<case_number>``), served at
    ``/api/materials/court/<court>.<case_number>`` — used for describedby.
    """
    from courts.models import CourtCase
    from materials.jsonld import court_case_material_iri
    from jawafdehi_shared.entities.ids import parse_material_iri

    # Exclude soft-deleted rows (mirrors ``courts.views`` list/retrieve, which
    # filter ``is_deleted=False``): a tombstoned court case must not linger as a
    # live ``<loc>`` in the public sitemap / ResourceSync.
    qs = CourtCase.objects.filter(is_deleted=False).values_list(
        "court", "case_number", "updated_at"
    )
    for court, case_number, updated_at in qs.iterator():
        try:
            # Both the courtcase @id IRI and the describedby material IRI can
            # raise ValueError (e.g. MAX_IRI_LENGTH on a concatenated ident), so
            # ALL of the IRI derivation must sit inside the guard — otherwise one
            # bad row 500s the whole sitemap/resourcesync surface.
            iri = _courtcase_iri(court, case_number)
            material_iri = court_case_material_iri(court, case_number)
            parsed = parse_material_iri(material_iri)
        except ValueError:
            # A row whose natural key can't form a valid IRI is not addressable;
            # skip it rather than emit a malformed loc / abort enumeration.
            logger.warning(
                "discovery: skipping courtcase row with un-addressable IRI "
                "(court=%r case_number=%r)",
                court,
                case_number,
            )
            continue
        yield Resource(
            iri=iri,
            lastmod=updated_at,
            type=TYPE_COURTCASE,
            jsonld_url=f"/api/materials/{parsed.source}/{parsed.ident}",
        )


def _courtcase_iri(court: str, case_number: str) -> str:
    from jawafdehi_shared.entities.ids import build_courtcase_iri

    return build_courtcase_iri(court, case_number)


def _iter_cases() -> Iterator[Resource]:
    """PUBLISHED Jawafdehi cases ONLY. Drafts/in-review/closed are NOT public.

    Filters ``state=PUBLISHED`` in the query, and additionally skips any row
    whose ``public_iri`` is ``None`` (the authoritative published gate on the
    model). Cases have no standalone JSON-LD endpoint, so ``jsonld_url`` is
    ``None``.
    """
    from cases.models import Case, CaseState

    qs = Case.objects.filter(state=CaseState.PUBLISHED).only(
        "slug", "state", "updated_at"
    )
    for case in qs.iterator():
        try:
            iri = case.public_iri  # None unless PUBLISHED + has slug
        except ValueError:
            # build_case_iri raises on a malformed slug; skip the single bad row
            # rather than aborting enumeration of every published case.
            logger.warning(
                "discovery: skipping case row with un-addressable IRI "
                "(pk=%r slug=%r)",
                case.pk,
                getattr(case, "slug", None),
            )
            continue
        if not iri:
            continue
        yield Resource(
            iri=iri,
            lastmod=getattr(case, "updated_at", None),
            type=TYPE_CASE,
            jsonld_url=None,
        )


# Registry: type token → enumerator. The order is the public-listing order.
_ENUMERATORS: dict[str, Callable[[], Iterator[Resource]]] = {
    TYPE_ENTITY: _iter_entities,
    TYPE_MATERIAL: _iter_materials,
    TYPE_COURTCASE: _iter_courtcases,
    TYPE_CASE: _iter_cases,
}


def iter_resources(types: tuple[str, ...] | None = None) -> Iterator[Resource]:
    """Stream every public :class:`Resource` across the requested types.

    ``types`` defaults to all four. This is the ONE function both the sitemap and
    ResourceSync generators consume, so the two surfaces always describe the same
    public corpus.
    """
    for t in types or ALL_TYPES:
        enumerator = _ENUMERATORS.get(t)
        if enumerator is None:
            continue
        yield from enumerator()


def count_resources(types: tuple[str, ...] | None = None) -> int:
    """Total count of public resources (used to decide sitemap-index paging).

    Counts at the DB level per type (cheap; no row materialization).
    """
    from cases.models import Case, CaseState
    from entities.models import StoredEntity
    from courts.models import CourtCase
    from materials.models import Material, Visibility

    counters: dict[str, Callable[[], int]] = {
        # Match _iter_entities: soft-deleted entities are not public.
        TYPE_ENTITY: lambda: StoredEntity.objects.filter(is_deleted=False).count(),
        # Must match _iter_materials: only LISTED, non-deleted materials are
        # public in the sitemap, so the paging count must exclude the rest.
        TYPE_MATERIAL: lambda: Material.objects.filter(
            is_deleted=False, visibility=Visibility.LISTED
        ).count(),
        # Match _iter_courtcases: soft-deleted court cases are not public.
        TYPE_COURTCASE: lambda: CourtCase.objects.filter(is_deleted=False).count(),
        TYPE_CASE: lambda: Case.objects.filter(state=CaseState.PUBLISHED).count(),
    }
    total = 0
    for t in types or ALL_TYPES:
        counter = counters.get(t)
        if counter is not None:
            total += counter()
    return total


def max_lastmod(types: tuple[str, ...] | None = None) -> datetime | None:
    """Cheapest ``MAX(updated_at)`` across the requested types (no row loads).

    Used to feed the sitemap's ``get_latest_lastmod()`` and to key the discovery
    caches on a corpus-version stamp — both WITHOUT materializing ``items()``.
    Returns ``None`` if there are no rows in the requested types.
    """
    from django.db.models import Max

    from cases.models import Case, CaseState
    from entities.models import StoredEntity
    from courts.models import CourtCase
    from materials.models import Material, Visibility

    def _agg(qs) -> datetime | None:
        return qs.aggregate(m=Max("updated_at"))["m"]

    aggregators: dict[str, Callable[[], datetime | None]] = {
        # Match _iter_entities so a soft-deleted entity's mtime can't drive the
        # public sitemap lastmod / corpus-version cache key.
        TYPE_ENTITY: lambda: _agg(StoredEntity.objects.filter(is_deleted=False)),
        # Match _iter_materials so a PRIVATE/soft-deleted material's mtime can't
        # drive the public sitemap lastmod / corpus-version cache key.
        TYPE_MATERIAL: lambda: _agg(
            Material.objects.filter(is_deleted=False, visibility=Visibility.LISTED)
        ),
        # Match _iter_courtcases so a soft-deleted court case's mtime can't drive
        # the public sitemap lastmod / corpus-version cache key.
        TYPE_COURTCASE: lambda: _agg(CourtCase.objects.filter(is_deleted=False)),
        TYPE_CASE: lambda: _agg(Case.objects.filter(state=CaseState.PUBLISHED)),
    }
    latest: datetime | None = None
    for t in types or ALL_TYPES:
        aggregator = aggregators.get(t)
        if aggregator is None:
            continue
        value = aggregator()
        if value is not None and (latest is None or value > latest):
            latest = value
    return latest
