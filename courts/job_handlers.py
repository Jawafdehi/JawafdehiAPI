"""Worker-side job handlers owned by the courts app.

One kind so far: ``court_scrape`` — crawl one court's recent cause-lists into the
ngm lake. Unlike the review/materials handlers (DB-free, run by the external HTTP
poller), the scrape handler WRITES court cases + hearings through the courts ORM,
so it runs in-process in a consumer that has ngm DB access (see
``courts/management/commands/scrape_worker.py``). It still rides the central job
queue (claim/lease/retry/backoff/dedup + the ``/api/jobs`` dashboard) for crash
safety and observability.
"""

from __future__ import annotations

from datetime import date, datetime

from django.utils import timezone

from courts.scraper import registry
from courts.scraper.crawl import run_crawl
from courts.scraper.fetch import Fetcher


def _anchor(value) -> date:
    """Resolve the optional ``today`` payload key (YYYY-MM-DD) to a date."""
    if not value:
        return timezone.localdate()
    return datetime.strptime(value, "%Y-%m-%d").date()


def handle_court_scrape(payload, on_stage=None, fetch=None) -> dict:
    """Crawl the court in ``payload['court']`` into the ngm lake (write mode).

    Payload keys: ``court`` (required registry key — normally one leaf court, as
    the enqueuer posts one job per court), ``lookback_days``, ``limit_dates``,
    ``enrich``, ``today``. Returns aggregate + per-court stats (also stored as
    ``job.result``). Raises ``ValueError``/``KeyError`` on a missing/unknown court
    (the worker treats these as non-retryable) and propagates fetch/parse errors
    (retryable).

    ``fetch`` is injectable for tests; production passes ``None`` and a live
    ``Fetcher`` is built.
    """
    court = (payload or {}).get("court")
    if not court:
        raise ValueError("court_scrape job payload is missing 'court'.")

    keys = registry.resolve(court)  # KeyError on an unknown court
    today = _anchor((payload or {}).get("today"))
    enrich = bool((payload or {}).get("enrich"))
    lookback_days = (payload or {}).get("lookback_days")
    limit_dates = (payload or {}).get("limit_dates")

    fetch = fetch or Fetcher()
    totals = {"courts": 0, "dates": 0, "cases": 0, "hearings": 0, "enriched": 0}
    per_court: list[dict] = []
    for key in keys:
        if on_stage is not None:
            on_stage(f"scrape:{key}")
        spec = registry.REGISTRY[key]
        for s in run_crawl(
            spec,
            fetch=fetch,
            today=today,
            lookback_days=lookback_days,
            limit_dates=limit_dates,
            write=True,
            enrich=enrich,
        ):
            totals["courts"] += 1
            totals["dates"] += s.dates
            totals["cases"] += s.cases
            totals["hearings"] += s.hearings
            totals["enriched"] += s.enriched
            per_court.append({
                "court": key,
                "court_id": s.court_id,
                "dates": s.dates,
                "cases": s.cases,
                "hearings": s.hearings,
                "enriched": s.enriched,
            })
    return {**totals, "per_court": per_court}


#: Kind → worker handler, mirroring review/materials ``job_handlers.HANDLERS`` so
#: a consumer can dispatch by kind. The in-process ``scrape_worker`` reads this;
#: the DB-free HTTP poller deliberately does NOT aggregate it (``court_scrape``
#: needs ngm DB access, which that poller doesn't carry).
HANDLERS = {"court_scrape": handle_court_scrape}
