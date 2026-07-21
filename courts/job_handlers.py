"""Worker-side job handlers owned by the courts app.

One kind so far: ``court_scrape`` — crawl one leaf court's recent cause-lists into
the ngm lake. Unlike the review/materials handlers (DB-free, run by the external
HTTP poller), the scrape handler WRITES court cases + hearings through the courts
ORM, so it runs in-process in a consumer that has ngm DB access (see
``courts/management/commands/scrape_worker.py``). It still rides the central job
queue (claim/lease/retry/backoff/dedup + the ``/api/jobs`` dashboard) for crash
safety and observability.
"""

from __future__ import annotations

from courts.scraper import registry
from courts.scraper.base import anchor
from courts.scraper.crawl import run_crawl
from courts.scraper.fetch import Fetcher


class BadCourtScrapePayload(ValueError):
    """The payload names a missing/unknown court (or a malformed ``today``).

    A permanent error: the worker treats it as non-retryable rather than
    re-queuing (a bad payload never succeeds on retry). Subclasses ``ValueError``
    so callers/tests catching ``ValueError`` still match. Everything raised from
    inside the crawl (portal flakes, parse hiccups, DB errors) is a DIFFERENT,
    retryable class — so a transient parser ValueError/KeyError is retried, not
    dead-lettered.
    """


def handle_court_scrape(payload, on_stage=None, fetch=None) -> dict:
    """Crawl the court in the payload into the ngm lake (write mode).

    Payload keys: ``court`` (required registry/tier key — special/district/high/
    supreme), ``court_id`` (optional leaf court to restrict to — the enqueuer
    posts one job per leaf court), ``lookback_days``, ``limit_dates``, ``enrich``,
    ``today``. Returns aggregate + per-court stats (also stored as ``job.result``).
    Raises ``BadCourtScrapePayload`` on a missing/unknown court or malformed
    ``today`` (non-retryable); propagates fetch/parse/DB errors (retryable).

    ``on_stage`` is called per crawled date so the consumer can heartbeat the job
    lease during a long crawl. ``fetch`` is injectable for tests; production
    passes ``None`` and a live ``Fetcher`` is built.
    """
    payload = payload or {}
    court = payload.get("court")
    if not court:
        raise BadCourtScrapePayload("court_scrape job payload is missing 'court'.")
    try:
        keys = registry.resolve(court)
    except KeyError as exc:
        raise BadCourtScrapePayload(str(exc)) from exc
    try:
        today = anchor(payload.get("today"))
    except ValueError as exc:
        raise BadCourtScrapePayload(f"bad 'today' in payload: {exc}") from exc

    court_id = payload.get("court_id")  # optional: restrict to one leaf court
    enrich = bool(payload.get("enrich"))
    lookback_days = payload.get("lookback_days")
    limit_dates = payload.get("limit_dates")

    # Heartbeat the job lease per crawled date so a long single-court crawl does
    # not outlive its lease and get reaped mid-run.
    on_progress = (
        (lambda cid, date_bs: on_stage(f"{cid} {date_bs}"))
        if on_stage is not None
        else None
    )

    fetch = fetch or Fetcher()
    totals = {"courts": 0, "dates": 0, "cases": 0, "hearings": 0, "enriched": 0}
    per_court: list[dict] = []
    for key in keys:
        spec = registry.REGISTRY[key]
        for s in run_crawl(
            spec,
            fetch=fetch,
            today=today,
            lookback_days=lookback_days,
            limit_dates=limit_dates,
            write=True,
            enrich=enrich,
            only_court_id=court_id,
            on_progress=on_progress,
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
