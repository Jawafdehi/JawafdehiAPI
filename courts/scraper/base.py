"""Shared crawl machinery for the court scrapers: date iteration, the crawl
frontier, and the court-agnostic ORM write path.

The per-court modules (special/district/high/supreme) are pure parsers; this
module turns their ``ParsedCase``/``ParsedHearing``/``ParsedEnrichment`` output
into ``courts`` rows in the ``ngm`` DB, normalising ``case_status`` at write time
via :mod:`courts.case_status`. All writes go through here so the extra_data-union
(never-clobber) rule and the normalization live in exactly one place.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from courts import case_status as cs
from courts.models import CaseEntity, Court, CourtCase, CourtCaseHearing, ScrapedDate
from courts.scraper.rows import ParsedCase, ParsedEnrichment, ParsedHearing

NGM_DB = "ngm"

# Court-owned typed columns the enrichment may set (never touched by the cause-list
# upsert, which only writes listing fields).
_ENRICH_COLUMNS = {
    "case_status", "verdict_type", "verdict_date_bs", "verdict_date_ad",
    "verdict_judge", "case_subject", "hearing_count", "registration_number",
}


def iter_bs_dates(lookback_days: int, *, today: date, offset_days: int = 1) -> Iterator[tuple[date, str]]:
    """Yield ``(ad_date, bs_date_str)`` from ``today-offset`` back ``lookback_days``.

    ``today`` is injected (never ``date.today()``) so callers/tests are
    deterministic. BS conversion uses the ``nepali`` calendar tables.
    """
    from nepali.datetime import nepalidate

    end = today - timedelta(days=offset_days)
    start = end - timedelta(days=lookback_days)
    current = end
    while current >= start:
        try:
            nd = nepalidate.from_date(current)
            yield current, f"{nd.year}-{nd.month:02d}-{nd.day:02d}"
        except Exception:
            pass
        current -= timedelta(days=1)


def scraped_dates_for(court_id: str, *, using: str = NGM_DB) -> set[str]:
    """The set of BS dates already crawled for a court (the frontier)."""
    return set(
        ScrapedDate.objects.using(using)
        .filter(court_id=court_id)
        .values_list("date_bs", flat=True)
    )


def mark_scraped(court_id: str, date_bs: str, note: str | None = None, *, using: str = NGM_DB) -> None:
    # Marking a date scraped for a court implies the court exists. Ensure it here
    # so a date with an EMPTY cause-list (no cases → upsert_causelist never ran
    # _ensure_court) doesn't fail the ScrapedDate → Court FK. Real courts already
    # exist (bulk-COPY'd); this only creates a stub for a court not yet in the DB.
    _ensure_court(court_id, using=using)
    ScrapedDate.objects.using(using).get_or_create(
        court_id=court_id, date_bs=date_bs, defaults={"note": note}
    )


def _ensure_court(court_id: str, *, using: str) -> None:
    Court.objects.using(using).get_or_create(
        identifier=court_id, defaults={"court_type": "", "full_name_nepali": ""}
    )


@transaction.atomic(using=NGM_DB)
def upsert_causelist(rows: list[tuple[ParsedCase, ParsedHearing]], *, using: str = NGM_DB) -> dict[str, int]:
    """Persist one date's cause-list ``(case, hearing)`` rows.

    Cases are upserted on the natural key ``(court, case_number)`` writing only
    listing fields; ``extra_data`` is UNIONed onto any existing row so an already
    enriched row's payload is never clobbered by a re-list. Hearings are appended
    (deduped on court/case/date/serial).
    """
    stats = {"cases": 0, "hearings": 0}
    for pcase, phearing in rows:
        _ensure_court(pcase.court_identifier, using=using)
        existing = (
            CourtCase.objects.using(using)
            .filter(court_id=pcase.court_identifier, case_number=pcase.case_number)
            .first()
        )
        # Union extra_data, but never let a re-list's null overwrite an existing
        # (enriched) value — a new key is added even if null; an existing key is
        # replaced only by a non-null listing value. Upholds the never-clobber rule.
        merged_extra = dict((existing.extra_data or {}) if existing else {})
        for key, value in (pcase.extra_data or {}).items():
            if value is not None or key not in merged_extra:
                merged_extra[key] = value
        listing = {
            "registration_date_bs": pcase.registration_date_bs,
            "registration_date_ad": pcase.registration_date_ad,
            "case_type": pcase.case_type,
            "plaintiff": pcase.plaintiff,
            "defendant": pcase.defendant,
            "extra_data": merged_extra or None,
        }
        CourtCase.objects.using(using).update_or_create(
            court_id=pcase.court_identifier,
            case_number=pcase.case_number,
            defaults=listing,
        )
        stats["cases"] += 1

        _, h_created = CourtCaseHearing.objects.using(using).get_or_create(
            court_id=phearing.court_identifier,
            case_number=phearing.case_number,
            hearing_date_bs=phearing.hearing_date_bs,
            serial_no=phearing.serial_no,
            defaults={
                "hearing_date_ad": phearing.hearing_date_ad or _fallback_date(),
                "bench": phearing.bench,
                "bench_type": phearing.bench_type,
                "judge_names": phearing.judge_names,
                "lawyer_names": phearing.lawyer_names,
                "case_status": phearing.case_status,
                "decision_type": phearing.decision_type,
                "remarks": phearing.remarks,
                "scraped_at": timezone.now(),
                "extra_data": phearing.extra_data or None,
            },
        )
        stats["hearings"] += int(h_created)
    return stats


def _fallback_date() -> date:
    # hearing_date_ad is NOT NULL; a BS date that won't convert still needs a value.
    return date(1900, 1, 1)


@transaction.atomic(using=NGM_DB)
def apply_enrichment(
    court_id: str, case_number: str, enrichment: ParsedEnrichment, *, using: str = NGM_DB
) -> bool:
    """Apply a detail-page enrichment to an existing case, normalising at write time.

    Returns ``False`` if the case row is absent. ``case_status`` is run through
    :mod:`courts.case_status`: header artifacts are dropped, and ``verdict_type`` /
    ``verdict_date_*`` are derived (from the status, else the final decisive
    hearing) when not already set. ``extra_data`` is UNIONed (never replaced).
    """
    case = (
        CourtCase.objects.using(using)
        .filter(court_id=court_id, case_number=case_number)
        .first()
    )
    if case is None:
        return False

    # A WAF-rejection / error / empty detail page parses into an empty
    # ParsedEnrichment. Applying it would flip status to "enriched" (so it never
    # retries) AND delete the existing parties — silent data loss. Require at
    # least one usable signal; otherwise leave the row untouched and report
    # not-enriched so the case is retried on the next run.
    if not (enrichment.core_fields or enrichment.extra_data or enrichment.entities):
        return False

    core = {k: v for k, v in enrichment.core_fields.items() if k in _ENRICH_COLUMNS}
    extra = dict(enrichment.extra_data or {})

    # --- write-time case_status normalization -------------------------------
    raw_status = core.get("case_status")
    parsed = cs.parse_case_status(raw_status)
    if cs.is_status_artifact(raw_status) or parsed.lifecycle_status == cs.UNKNOWN:
        core.pop("case_status", None)  # never store a header/blank as a status
    verdict = core.get("verdict_type") or parsed.verdict_type or cs.verdict_from_hearings(
        extra.get("enrichment_hearings")
    )
    if verdict:
        core["verdict_type"] = verdict
    if parsed.verdict_date_bs and not core.get("verdict_date_bs"):
        core["verdict_date_bs"] = parsed.verdict_date_bs
        core["verdict_date_ad"] = parsed.verdict_date_ad

    for key, value in core.items():
        setattr(case, key, value)
    case.extra_data = {**(case.extra_data or {}), **extra}
    case.status = "enriched"
    case.save(using=using)

    _replace_entities(court_id, case_number, enrichment.entities, using=using)
    return True


def _replace_entities(court_id: str, case_number: str, entities, *, using: str) -> None:
    CaseEntity.objects.using(using).filter(
        court_id=court_id, case_number=case_number
    ).delete()
    CaseEntity.objects.using(using).bulk_create(
        [
            CaseEntity(
                court_id=court_id,
                case_number=case_number,
                side=e.get("side", ""),
                name=e.get("name", ""),
                address=e.get("address"),
            )
            for e in (entities or [])
            if e.get("name")
        ]
    )
