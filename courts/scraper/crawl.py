"""Crawl orchestration: iterate a court's un-scraped dates, fetch+parse each via
the court module, and persist through :mod:`courts.scraper.base`.

Kept separate from the management command so it's driven directly in tests with a
fake ``fetch`` (no network, no live portal). ``fetch(url, data=None) -> html``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from courts.scraper import base


@dataclass
class CrawlStats:
    court_id: str
    dates: int = 0
    cases: int = 0
    hearings: int = 0
    enriched: int = 0
    per_date: list[str] = field(default_factory=list)


def run_crawl(
    module,
    *,
    fetch,
    today: date,
    lookback_days: int | None = None,
    limit_dates: int | None = None,
    write: bool = False,
    enrich: bool = False,
) -> list[CrawlStats]:
    """Crawl every court the ``module`` covers. ``module`` exposes ``court_ids``,
    ``LOOKBACK_DAYS``, ``crawl_date`` and (optionally) ``crawl_detail``.

    Dry-run by default: fetch + parse run, but nothing is written unless
    ``write=True``. Returns per-court stats.
    """
    from nepali.datetime import nepalidate

    lookback = lookback_days if lookback_days is not None else module.LOOKBACK_DAYS
    results: list[CrawlStats] = []

    for court_id in module.court_ids(fetch):
        stats = CrawlStats(court_id=court_id)
        done = base.scraped_dates_for(court_id) if write else set()
        touched: set[str] = set()
        for ad_date, date_bs in base.iter_bs_dates(lookback, today=today):
            if date_bs in done:
                continue
            rows = module.crawl_date(fetch, court_id, date_bs, nepalidate.from_date(ad_date))
            stats.dates += 1
            stats.per_date.append(date_bs)
            if write:
                counts = base.upsert_causelist(rows)
                stats.cases += counts["cases"]
                stats.hearings += counts["hearings"]
                touched.update(c.case_number for c, _ in rows)
                base.mark_scraped(court_id, date_bs, note=f"{len(rows)} rows")
            else:
                stats.cases += len({(c.court_identifier, c.case_number) for c, _ in rows})
                stats.hearings += len(rows)
            if limit_dates and stats.dates >= limit_dates:
                break

        if enrich and write and hasattr(module, "crawl_detail"):
            stats.enriched = _enrich_pending(module, court_id, fetch, only=touched)
        results.append(stats)
    return results


def _enrich_pending(module, court_id: str, fetch, *, only=None) -> int:
    """Fetch + apply enrichment for this court's not-yet-enriched cases.

    ``only`` scopes enrichment to a set of case numbers (the ones this crawl
    touched) so a limited-lookback ``--enrich`` run never fans out over the whole
    historical corpus. ``None`` means every non-enriched case for the court.
    """
    from courts.models import CourtCase

    n = 0
    pending = (
        CourtCase.objects.using(base.NGM_DB)
        .filter(court_id=court_id)
        .exclude(status="enriched")
        .values_list("case_number", flat=True)
    )
    if only is not None:
        pending = pending.filter(case_number__in=only)
    for case_number in list(pending):
        enrichment = module.crawl_detail(fetch, court_id, case_number)
        if enrichment is not None and base.apply_enrichment(court_id, case_number, enrichment):
            n += 1
    return n
