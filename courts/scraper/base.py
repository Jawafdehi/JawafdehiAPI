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
from datetime import date, datetime, timedelta

from django.db import transaction
from django.utils import timezone

from courts import case_status as cs
from courts.models import CaseEntity, Court, CourtCase, CourtCaseHearing, ScrapedDate
from courts.scraper.rows import ParsedCase, ParsedEnrichment, ParsedHearing

NGM_DB = "ngm"

# Court-owned typed columns the enrichment may set. The cause-list upsert writes
# listing fields only, with ONE exception: a decisive sitting promotes
# case_status/verdict_* onto the case row (see _causelist_verdict). Enrichment
# runs once per case and the verdict usually lands later, so without that
# promotion a decided case reads as ongoing forever.
_ENRICH_COLUMNS = {
    "case_status", "verdict_type", "verdict_date_bs", "verdict_date_ad",
    "verdict_judge", "case_subject", "hearing_count", "registration_number",
}


def anchor(value: str | None) -> date:
    """Resolve an optional ``YYYY-MM-DD`` AD anchor date; default to today (KTM
    localdate). Raises ``ValueError`` on a malformed string. Shared by the
    scrape_courtcases command and the court_scrape job handler.
    """
    if not value:
        return timezone.localdate()
    return datetime.strptime(value, "%Y-%m-%d").date()


def iter_bs_dates(lookback_days: int, *, today: date, offset_days: int = 1) -> Iterator[tuple[date, str]]:
    """Yield ``(ad_date, bs_date_str)`` from ``today-offset`` back ``lookback_days``.

    ``today`` is injected (never ``date.today()``) so callers/tests are
    deterministic. BS conversion uses the ``nepali`` calendar tables.
    """
    from jawafdehi_shared.dates import ad_to_bs

    end = today - timedelta(days=offset_days)
    start = end - timedelta(days=lookback_days)
    current = end
    while current >= start:
        bs = ad_to_bs(current)
        if bs:
            yield current, bs
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


def _causelist_verdict(
    phearing: ParsedHearing, existing: CourtCase | None
) -> dict[str, object] | None:
    """Verdict columns to promote from a decisive cause-list sitting, else ``None``.

    The cause list already carries the disposition — for the Special Court it is
    cells 9 and 10 of the row, e.g. ``फैसला`` / ``सफाई``. We were writing that pair
    onto the hearing and dropping it from the case row, so a case decided after
    its one-shot enrichment kept ``case_status='चलिरहेको'`` with NULL verdicts
    permanently (measured 2026-08-20: 114 Special Court cases).

    Classification goes through :func:`courts.case_status.outcome_from_hearings`
    rather than a local substring test, so this inherits the shared vocabulary and
    its guarantees: only the terminal bucket counts (the portal writes
    ``अन्तिम आदेश`` on plainly interlocutory orders), an unrecognised decision
    yields nothing rather than a guess, and ``आंशिक`` is matched ahead of ``ठहर``
    so a PARTIAL conviction is never recorded as a full one.

    Returns ``None`` when the sitting decided nothing, and also when promoting it
    would REGRESS the row. That second guard matters because the lookback horizons
    reach back years (BS 2070 for special), so historical dates are re-crawled
    routinely — and a case can be decided, reopened on review and decided again,
    where only the latest disposition is operative. So an older sitting never
    overwrites a newer verdict, and an undated sitting is promoted only onto a row
    with no verdict date to lose.
    """
    outcome = cs.outcome_from_hearings(
        [{"case_status": phearing.case_status, "decision_type": phearing.decision_type}]
    )
    if outcome is None:
        return None

    # The sitting's date comes from the parser's typed fields, never from
    # re-parsing text. Both must be present: hearing_date_ad falls back to
    # 1900-01-01 on the hearing row (that column is NOT NULL) and that sentinel
    # must never become a verdict date.
    dated = bool(phearing.hearing_date_bs and phearing.hearing_date_ad)
    verdict_date_ad = phearing.hearing_date_ad if dated else None

    held = existing.verdict_date_ad if existing else None
    if held is not None and (verdict_date_ad is None or verdict_date_ad < held):
        return None

    promoted: dict[str, object] = {
        # The court's own label for the sitting ("फैसला"), which parse_case_status
        # reads as DECIDED. The typed columns below carry the structured outcome.
        "case_status": phearing.case_status,
        "verdict_type": outcome.verdict_type,
    }
    if dated:
        promoted["verdict_date_bs"] = phearing.hearing_date_bs
        promoted["verdict_date_ad"] = phearing.hearing_date_ad
    return promoted


@transaction.atomic(using=NGM_DB)
def upsert_causelist(rows: list[tuple[ParsedCase, ParsedHearing]], *, using: str = NGM_DB) -> dict[str, int]:
    """Persist one date's cause-list ``(case, hearing)`` rows.

    Cases are upserted on the natural key ``(court, case_number)`` writing listing
    fields, plus ``case_status``/``verdict_*`` when the sitting disposed of the case
    (:func:`_causelist_verdict`); ``extra_data`` is UNIONed onto any existing row so
    an already enriched row's payload is never clobbered by a re-list. Hearings are
    appended (deduped on court/case/date/serial).
    """
    stats = {"cases": 0, "hearings": 0, "verdicts_promoted": 0}
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
        # A sitting that disposed of the case promotes its outcome onto the case
        # row — the one place the cause list may write enrichment-owned columns.
        promoted = _causelist_verdict(phearing, existing)
        if promoted is not None:
            listing.update(promoted)
            stats["verdicts_promoted"] += 1
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


def apply_enrichment(
    court_id: str, case_number: str, enrichment: ParsedEnrichment, *, using: str = NGM_DB
) -> bool:
    """Apply a detail-page enrichment to an existing case, normalising at write time.

    Returns ``False`` if the case row is absent. ``case_status`` is run through
    :mod:`courts.case_status`: header artifacts are dropped, and ``verdict_type`` /
    ``verdict_date_*`` are derived (from the status, else the final decisive
    hearing) when not already set. ``extra_data`` is UNIONed (never replaced).

    The atomic block is opened on ``using``. As an ``@transaction.atomic(using=NGM_DB)``
    decorator it bound to that one alias at import time, so any other alias ran the
    save + ``_replace_entities`` delete-and-recreate with no transaction at all.
    """
    case = (
        CourtCase.objects.using(using)
        .filter(court_id=court_id, case_number=case_number)
        .first()
    )
    if case is None:
        return False

    # A WAF-rejection / error / not-found detail page parses into an empty
    # ParsedEnrichment. Applying it would flip status to "enriched" (so it never
    # retries) AND delete the existing parties — silent data loss. Require at
    # least one usable signal; otherwise leave the row untouched and report
    # not-enriched so the case is retried on the next run.
    #
    # NB: the test is core_fields/entities only, NOT the truthiness of extra_data.
    # The supreme/district/high parsers always emit their enrichment_hearings /
    # enrichment_timeline keys, so an EMPTY parse still carries a truthy
    # extra_data — an ``or extra_data`` guard passes a not-found page on three of
    # the four courts and destroys the parties it exists to protect.
    if not enrichment.identifies_a_case():
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

    with transaction.atomic(using=using):
        for key, value in core.items():
            setattr(case, key, value)
        case.extra_data = {**(case.extra_data or {}), **extra}
        case.status = "enriched"
        case.save(using=using)

        _replace_entities(court_id, case_number, enrichment.entities, using=using)
    return True


def upsert_from_detail(
    court_id: str, case_number: str, enrichment: ParsedEnrichment, *, using: str = NGM_DB
) -> bool:
    """Create a case the register sweep discovered, then enrich it.

    The cause-list crawler only sees cases that reached a published hearing list;
    this is the path for the ones that never did. ``apply_enrichment`` alone can't
    do it — it returns ``False`` when the row is absent, which is every swept case.

    Returns ``False`` (writing nothing) when the parse identifies no case, or when
    the case **already exists**. That second guard is not an optimisation: re-running
    enrichment over a known case calls ``_replace_entities``, which drops every party
    row and recreates it without ``nes_id`` — silently destroying entity resolution.
    The sweep only ever adds what is missing.

    ``registration_date_bs``/``_ad``/``case_type`` are set here rather than by
    ``apply_enrichment``, which by design only writes ``_ENRICH_COLUMNS`` (the
    cause-list owns the listing fields — but for a swept case there was no listing).

    The transaction is opened on ``using`` rather than by an ``@transaction.atomic``
    decorator, which would bind to ``NGM_DB`` at import time and leave every write
    here — including ``_replace_entities``' delete-then-recreate — unprotected on
    any other alias.
    """
    if not enrichment.identifies_a_case():
        return False

    with transaction.atomic(using=using):
        if (
            CourtCase.objects.using(using)
            .filter(court_id=court_id, case_number=case_number)
            .exists()
        ):
            return False

        _ensure_court(court_id, using=using)
        core = enrichment.core_fields
        CourtCase.objects.using(using).create(
            court_id=court_id,
            case_number=case_number,
            registration_date_bs=core.get("registration_date_bs"),
            registration_date_ad=core.get("registration_date_ad"),
            case_type=core.get("case_type"),
            status="pending",
            extra_data={"source": "register_sweep"},
        )
        apply_enrichment(court_id, case_number, enrichment, using=using)
        materialise_detail_hearings(court_id, case_number, enrichment, using=using)
    return True


def materialise_detail_hearings(
    court_id: str, case_number: str, enrichment: ParsedEnrichment, *, using: str = NGM_DB
) -> int:
    """Turn a detail page's hearing list into ``CourtCaseHearing`` rows.

    Without this a swept case exists only in ``court_cases`` and is invisible to
    every hearing-level query — including the deciding-hearing analysis that the
    conviction figures rest on. ``apply_enrichment`` keeps the same list in
    ``extra_data.enrichment_hearings``, so the JSON stays the full-fidelity record
    and these rows are the relational projection of it.

    Two deliberate limits, both recorded rather than papered over:

    * **No ``serial_no``.** A detail page doesn't publish one, and inventing an
      ordinal would fabricate court data. Rows are therefore deduped on the date
      alone, so two hearings sharing a date collapse into one relational row (the
      JSON still lists both).
    * **Unconvertible dates are skipped, not sentinelled.** ``hearing_date_ad`` is
      NOT NULL and the cause-list path falls back to ``1900-01-01``; emitting that
      here would seed a clean column with fake dates for rows nobody asked for.

    ``judge_names``/``bench`` stay null — the detail page carries neither.
    """
    from jawafdehi_shared.dates import bs_to_ad

    rows = (enrichment.extra_data or {}).get("enrichment_hearings") or []
    written = 0
    for hearing in rows:
        date_bs = (hearing or {}).get("hearing_date")
        if not date_bs:
            continue
        date_ad = bs_to_ad(date_bs)
        if date_ad is None:
            continue
        # exists() rather than get_or_create(): CourtCaseHearing carries no unique
        # constraint and the cause-list path dedupes on a 4-tuple that includes
        # serial_no, so a case can already hold two rows for one date. get_or_create
        # would raise MultipleObjectsReturned on exactly those cases.
        if (
            CourtCaseHearing.objects.using(using)
            .filter(court_id=court_id, case_number=case_number, hearing_date_bs=date_bs)
            .exists()
        ):
            continue
        CourtCaseHearing.objects.using(using).create(
            court_id=court_id,
            case_number=case_number,
            hearing_date_bs=date_bs,
            hearing_date_ad=date_ad,
            case_status=hearing.get("case_status") or None,
            decision_type=hearing.get("decision_type") or None,
            scraped_at=timezone.now(),
            extra_data={"source": "register_sweep"},
        )
        written += 1
    return written


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
