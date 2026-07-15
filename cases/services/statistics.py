"""Statistics snapshot service — computes and persists the /api/statistics/ payload.

The payload aggregates over the full NES/NGM datasets (millions of court-case and
entity rows; ~15-19s on prod), far too slow to compute inside a request. It is
therefore computed OUT-OF-BAND — the ``refresh_statistics`` management command,
run on a schedule — and persisted to the single-row ``cases.StatisticsSnapshot``
table on the default (Jawafdehi) database, which every worker process on every
replica shares. ``StatisticsView`` then serves the stored payload with one
primary-key lookup per request and never pays the aggregation cost.

(This replaced a per-process ``LocMemCache``: with N worker processes each held
its own 5-minute cache, so every process recomputed the aggregation on its own
clock and a cold hit stalled the public homepage for the full computation.)

The snapshot is a keyed row, NOT a TTL cache entry: if a refresh run fails, the
endpoint keeps serving the last good payload (stale-but-valid) instead of a
request-blocking recompute or an error.
"""

from __future__ import annotations

import json

from django.db import connections
from django.db.models import Count, Q
from django.db.models.functions import ExtractYear
from django.utils import timezone

# NES + NGM models live in sibling apps; the DB router (config.db_router) sends
# each to its own database on read, so this module can query them directly for
# the cross-source data-quality metrics surfaced by StatisticsView.
from courts.models import Court, CourtCase
from entities.models import StoredEntity
from materials.models import Material

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    StatisticsSnapshot,
)

# Primary key of the single StatisticsSnapshot row the platform maintains.
STATISTICS_SNAPSHOT_KEY = "statistics"


def _pct(part: int, whole: int) -> float:
    """Percentage of ``part`` over ``whole``, 1 dp; 0.0 when ``whole`` is 0."""
    return round((part / whole) * 100, 1) if whole else 0.0


def compute_statistics() -> dict:
    """Aggregate the full statistics payload. SLOW on prod — never call from a
    request path except the one-time bootstrap in ``StatisticsView``."""
    return {
        **_case_counts(),
        # Cross-source coverage for the Data Quality dashboard. The DB router
        # sends each model below to its own database (nes / ngm).
        "nes": _nes_metrics(),
        "ngm": _ngm_metrics(),
        "materials": _materials_metrics(),
        "last_updated": timezone.now().isoformat(),
    }


def bootstrap_placeholder() -> dict:
    """Cheap payload in the exact response shape, for the bootstrap window only.

    Served to requests that lose the ``StatisticsView`` bootstrap claim race
    while the winning request computes the real payload. The Jawafdehi-DB case
    counts are real (they are a handful of fast queries on a small table); the
    heavy NES/NGM/materials blocks are zero-valued stand-ins in the same shape
    (``test_bootstrap_placeholder_matches_payload_shape`` pins them to the real
    payload's structure).
    """
    return {
        **_case_counts(),
        "nes": {
            "total": 0,
            "by_prefix": [],
            "by_type": [],
            "persons_by_sector": [],
            "counts": {
                "with_identifier": 0,
                "with_provenance": 0,
                "with_bilingual_name": 0,
            },
            "completeness": {
                "with_identifier": 0.0,
                "with_provenance": 0.0,
                "with_bilingual_name": 0.0,
            },
        },
        "ngm": {
            "court_cases_total": 0,
            "courts_total": 0,
            "by_court_type": [],
            "by_year": [],
            "by_court_type_year": [],
            "counts": {
                "nes_resolved": 0,
                "with_registration_date": 0,
                "with_document_sources": 0,
            },
            "completeness": {
                "nes_resolved": 0.0,
                "with_registration_date": 0.0,
                "with_document_sources": 0.0,
            },
        },
        "materials": {
            "total": 0,
            "by_type": [],
            "by_source": [],
            "counts": {
                "with_description": 0,
                "with_url": 0,
                "with_date": 0,
            },
            "completeness": {
                "with_description": 0.0,
                "with_url": 0.0,
                "with_date": 0.0,
            },
        },
        "last_updated": timezone.now().isoformat(),
    }


def _case_counts() -> dict:
    """Jawafdehi-DB case metrics — cheap (small tables on the default DB)."""
    by_state = Case.objects.aggregate(
        published=Count("pk", filter=Q(state=CaseState.PUBLISHED)),
        under_investigation=Count(
            "pk", filter=Q(state__in=[CaseState.DRAFT, CaseState.IN_REVIEW])
        ),
        # "In review" (being prepared for publication) split out of the broader
        # under-investigation bucket, which also covers DRAFT.
        in_review=Count("pk", filter=Q(state=CaseState.IN_REVIEW)),
        closed=Count("pk", filter=Q(state=CaseState.CLOSED)),
        # CIAA vs non-CIAA split. CIAA corruption cases are drafted with
        # case_type=CORRUPTION (see ciaa_draft_case_service); every other type
        # (bribery, forgery, embezzlement, ...) runs through other bodies.
        ciaa=Count("pk", filter=Q(case_type=CaseType.CORRUPTION)),
        non_ciaa=Count("pk", filter=~Q(case_type=CaseType.CORRUPTION)),
    )
    return {
        "published_cases": by_state["published"],
        "cases_under_investigation": by_state["under_investigation"],
        "cases_in_review": by_state["in_review"],
        "cases_closed": by_state["closed"],
        "cases_ciaa": by_state["ciaa"],
        "cases_non_ciaa": by_state["non_ciaa"],
        # Unique NES entities tracked across published cases (binds hold the
        # nes_id directly; NES owns the entity records).
        "entities_tracked": (
            CaseEntityRelationship.objects.filter(case__state=CaseState.PUBLISHED)
            .values("nes_id")
            .distinct()
            .count()
        ),
    }


def refresh_statistics() -> dict:
    """Compute the statistics payload and upsert the shared snapshot row.

    The upsert is a single ``INSERT .. ON CONFLICT DO UPDATE`` (no read-modify-
    write), so it is atomic under concurrent refreshes and — because it is a
    pure write — always routed to the primary database even when the calling
    context has opted its reads into the read replica.
    """
    stats = compute_statistics()
    StatisticsSnapshot.objects.bulk_create(
        [
            StatisticsSnapshot(
                key=STATISTICS_SNAPSHOT_KEY,
                data=stats,
                computed_at=timezone.now(),
                # A real payload always clears any bootstrap-placeholder flag.
                is_placeholder=False,
            )
        ],
        update_conflicts=True,
        unique_fields=["key"],
        update_fields=["data", "computed_at", "is_placeholder"],
    )
    return stats


def _sector_from_member_iri(iri: str) -> str | None:
    """Map an organization IRI (a person's ``memberOf``) to a person sector.

    The org IRI path encodes its kind (``.../organization/government/ward/...``),
    so the sector is read straight off the structured identifier — no free-text /
    keyword guessing. Only the org kinds that actually occur in the dataset are
    distinguished; any other resolvable org is ``other``. Returns None when the
    IRI carries no organization segment (so the caller can keep scanning roles).
    """
    if (
        "/organization/government/ward" in iri
        or "/organization/government/localunit" in iri
    ):
        return "local_gov"
    if "/organization/political_party" in iri:
        return "politicians"
    if "/organization/hospital" in iri:
        return "health"
    if "/organization" in iri:
        return "other"
    return None


def _sector_for_roles(roles) -> str:
    """A person's sector from their ``hasOccupation`` value (dict | list | None).

    Takes the first role whose ``memberOf`` resolves to a sector; a person with
    no role, or no resolvable office, is ``not_recorded`` (shown honestly rather
    than dropped). Defensive against the value arriving JSON-encoded (sqlite).
    """
    if isinstance(roles, str):
        try:
            roles = json.loads(roles)
        except (ValueError, TypeError):
            return "not_recorded"
    if isinstance(roles, dict):
        roles = [roles]
    if isinstance(roles, list):
        for role in roles:
            if not isinstance(role, dict):
                continue
            member = role.get("memberOf")
            if isinstance(member, dict):
                # ``@id`` should be a string; coerce anything else to "" so a single
                # malformed entity can't crash the whole refresh_statistics job.
                val = member.get("@id")
                iri = val if isinstance(val, str) else ""
            elif isinstance(member, str):
                iri = member
            else:
                iri = ""
            sector = _sector_from_member_iri(iri)
            if sector:
                return sector
    return "not_recorded"


def _persons_by_sector() -> list[dict]:
    """Tally Person entities by the sector of the office they hold.

    Reads only the ``hasOccupation`` sub-document per person (a JSON key
    projection, not the whole doc) and streams with a server-side cursor, so the
    scan stays cheap enough for the out-of-band refresh. Ordered largest-first.
    """
    tally: dict[str, int] = {}
    roles_iter = (
        StoredEntity.objects.filter(entity_type="Person")
        .values_list("data__hasOccupation", flat=True)
        .iterator(chunk_size=5000)
    )
    for roles in roles_iter:
        sector = _sector_for_roles(roles)
        tally[sector] = tally.get(sector, 0) + 1
    return [
        {"sector": sector, "count": count}
        for sector, count in sorted(tally.items(), key=lambda kv: -kv[1])
    ]


def _nes_metrics():
    """NES (entities) coverage — totals, breakdowns, completeness.

    Counts are server-side aggregates over indexed promoted columns. The
    completeness signals live inside the ``data`` JSON-LD column; on Postgres
    they are answered with JSON-key existence lookups, on sqlite (the empty
    local stores / test DB) they degrade to 0 without a full-table scan.
    """
    total = StoredEntity.objects.count()

    by_prefix = list(
        StoredEntity.objects.values("prefix")
        .annotate(count=Count("iri"))
        .order_by("-count")
    )
    by_type = list(
        StoredEntity.objects.values("entity_type")
        .annotate(count=Count("iri"))
        .order_by("-count")
    )

    # Completeness signals reflect what is ACTUALLY stored on the entity JSON-LD
    # doc. NOTE: source attributions are NOT carried on the published doc (they
    # live in the bulk-ingest envelope + the version provenance), so we measure
    # the real stored provenance instead: ``identifier`` (the stable external id
    # — ECN candidate-id / pcode / reg-no, present on ~all sourced entities) and
    # ``jawafdehi:version`` (the authored version/provenance block). ``name``
    # bilingualism is measured directly. (An earlier draft keyed on
    # ``jawafdehi:sources`` / ``description``, which no entity carries → always 0.)
    # Vendor check is against the DB the model actually routes to (nes), not
    # ``default`` — the aliases can run different engines (sqlite local stores).
    if connections[StoredEntity.objects.db].vendor == "postgresql":
        with_identifier = StoredEntity.objects.filter(
            data__has_key="identifier"
        ).count()
        with_provenance = StoredEntity.objects.filter(
            data__has_key="jawafdehi:version"
        ).count()
        with_bilingual_name = (
            StoredEntity.objects.filter(data__name__has_key="en")
            .filter(data__name__has_key="ne")
            .count()
        )
    else:
        with_identifier = with_provenance = with_bilingual_name = 0

    return {
        "total": total,
        "by_prefix": by_prefix,
        "by_type": by_type,
        "persons_by_sector": _persons_by_sector(),
        "counts": {
            "with_identifier": with_identifier,
            "with_provenance": with_provenance,
            "with_bilingual_name": with_bilingual_name,
        },
        "completeness": {
            "with_identifier": _pct(with_identifier, total),
            "with_provenance": _pct(with_provenance, total),
            "with_bilingual_name": _pct(with_bilingual_name, total),
        },
    }


def _ngm_metrics():
    """NGM (judicial) coverage — court-case / court totals, breakdowns, and
    completeness over court cases (all indexed columns). Materials are a
    distinct dataset with their own block — see ``_materials_metrics``."""
    court_cases_total = CourtCase.objects.count()

    by_court_type = list(
        CourtCase.objects.values("court__court_type")
        .annotate(count=Count("case_number"))
        .order_by("-count")
    )

    # Court cases filed per year, and per court level per year (the matrix). Both
    # key off the indexed registration_date_ad; cases with no registration date
    # are excluded so a null year never appears as a column. Capped to the most
    # recent N years so an outlier/dirty registration year cannot make the matrix
    # grow unbounded (the heatmap has one column per kept year).
    _MATRIX_YEARS = 25
    dated = CourtCase.objects.exclude(registration_date_ad__isnull=True).annotate(
        year=ExtractYear("registration_date_ad")
    )
    year_rows = list(
        dated.values("year").annotate(count=Count("case_number")).order_by("year")
    )
    # Keep the most-recent N years, but return them ascending (frontend order).
    kept_years = {row["year"] for row in year_rows[-_MATRIX_YEARS:]}
    by_year = [row for row in year_rows if row["year"] in kept_years]
    by_court_type_year = [
        row
        for row in dated.values("court__court_type", "year")
        .annotate(count=Count("case_number"))
        .order_by("court__court_type", "year")
        if row["year"] in kept_years
    ]

    nes_resolved = (
        CourtCase.objects.exclude(nes_id__isnull=True).exclude(nes_id="").count()
    )
    with_registration_date = CourtCase.objects.exclude(
        registration_date_ad__isnull=True
    ).count()
    if connections[CourtCase.objects.db].vendor == "postgresql":
        with_document_sources = (
            CourtCase.objects.filter(document_sources__isnull=False)
            .exclude(document_sources=[])
            .count()
        )
    else:
        with_document_sources = CourtCase.objects.exclude(
            document_sources__isnull=True
        ).count()

    return {
        "court_cases_total": court_cases_total,
        "courts_total": Court.objects.count(),
        "by_court_type": by_court_type,
        "by_year": by_year,
        "by_court_type_year": by_court_type_year,
        "counts": {
            "nes_resolved": nes_resolved,
            "with_registration_date": with_registration_date,
            "with_document_sources": with_document_sources,
        },
        "completeness": {
            "nes_resolved": _pct(nes_resolved, court_cases_total),
            "with_registration_date": _pct(
                with_registration_date, court_cases_total
            ),
            "with_document_sources": _pct(
                with_document_sources, court_cases_total
            ),
        },
    }


def _materials_metrics():
    """Materials (NGM development-project / document dataset) coverage —
    total, by-type / by-source breakdowns, and completeness measured over
    the material ``data`` JSON-LD doc. Materials are NOT judicial records;
    they get their own block separate from ``_ngm_metrics``."""
    # Soft-deleted materials are off every read plane (retrieve/search/sitemap
    # all filter ``is_deleted=False``); the coverage stats must match, else a
    # tombstoned pile (e.g. the retired ``court`` court_case shadows) lingers in
    # the by-source breakdown after it is gone everywhere else.
    live = Material.objects.filter(is_deleted=False)
    total = live.count()

    by_type = list(
        live.values("material_type").annotate(count=Count("iri")).order_by("-count")
    )
    by_source = list(
        live.values("source").annotate(count=Count("iri")).order_by("-count")
    )

    # Completeness signals over the stored schema.org JSON-LD doc. On Postgres
    # answered with JSON-key existence lookups; sqlite (empty local / test DB)
    # degrades to 0 without a full scan.
    if connections[Material.objects.db].vendor == "postgresql":
        with_description = live.filter(data__has_key="description").count()
        with_url = live.filter(data__has_key="url").count()
        with_date = live.filter(data__has_key="dateCreated").count()
    else:
        with_description = with_url = with_date = 0

    return {
        "total": total,
        "by_type": by_type,
        "by_source": by_source,
        "counts": {
            "with_description": with_description,
            "with_url": with_url,
            "with_date": with_date,
        },
        "completeness": {
            "with_description": _pct(with_description, total),
            "with_url": _pct(with_url, total),
            "with_date": _pct(with_date, total),
        },
    }
