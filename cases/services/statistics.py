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

from django.db import connection
from django.db.models import Count
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
    StatisticsSnapshot,
)

# Primary key of the single StatisticsSnapshot row the platform maintains.
STATISTICS_SNAPSHOT_KEY = "statistics"


def _pct(part: int, whole: int) -> float:
    """Percentage of ``part`` over ``whole``, 1 dp; 0.0 when ``whole`` is 0."""
    return round((part / whole) * 100, 1) if whole else 0.0


def compute_statistics() -> dict:
    """Aggregate the full statistics payload. SLOW on prod — never call from a
    request path except the one-time bootstrap in ``refresh_statistics``."""
    return {
        "published_cases": Case.objects.filter(state=CaseState.PUBLISHED).count(),
        "cases_under_investigation": Case.objects.filter(
            state__in=[CaseState.DRAFT, CaseState.IN_REVIEW]
        ).count(),
        "cases_closed": Case.objects.filter(state=CaseState.CLOSED).count(),
        # Unique NES entities tracked across published cases (binds hold the
        # nes_id directly; NES owns the entity records).
        "entities_tracked": (
            CaseEntityRelationship.objects.filter(case__state=CaseState.PUBLISHED)
            .values("nes_id")
            .distinct()
            .count()
        ),
        # Cross-source coverage for the Data Quality dashboard. The DB router
        # sends each model below to its own database (nes / ngm).
        "nes": _nes_metrics(),
        "ngm": _ngm_metrics(),
        "materials": _materials_metrics(),
        "last_updated": timezone.now().isoformat(),
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
            )
        ],
        update_conflicts=True,
        unique_fields=["key"],
        update_fields=["data", "computed_at"],
    )
    return stats


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
    if connection.vendor == "postgresql":
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

    nes_resolved = (
        CourtCase.objects.exclude(nes_id__isnull=True).exclude(nes_id="").count()
    )
    with_registration_date = CourtCase.objects.exclude(
        registration_date_ad__isnull=True
    ).count()
    if connection.vendor == "postgresql":
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
    total = Material.objects.count()

    by_type = list(
        Material.objects.values("material_type")
        .annotate(count=Count("iri"))
        .order_by("-count")
    )
    by_source = list(
        Material.objects.values("source")
        .annotate(count=Count("iri"))
        .order_by("-count")
    )

    # Completeness signals over the stored schema.org JSON-LD doc. On Postgres
    # answered with JSON-key existence lookups; sqlite (empty local / test DB)
    # degrades to 0 without a full scan.
    if connection.vendor == "postgresql":
        with_description = Material.objects.filter(
            data__has_key="description"
        ).count()
        with_url = Material.objects.filter(data__has_key="url").count()
        with_date = Material.objects.filter(data__has_key="dateCreated").count()
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
