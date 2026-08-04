"""Register sweep — fetch the dockets the cause-list crawler structurally cannot see.

``crawl_date`` reads a court's daily hearing list, so a case only enters the mirror
if it was listed. Cases registered and disposed without a public listing never
appear; the ``ScrapedDate`` frontier can't detect them either, because it records
which dates were visited, not which dockets were absent from them.

This module closes that hole: :func:`courts.scraper.registers.register_gaps` derives
the missing docket numbers from the register's own sequence, and each one is probed
against the court's ``crawl_detail`` endpoint. Found → created. Not found → recorded
in the negative cache so it isn't re-probed forever.

Bounded by design. It shares the ``court_scrape`` job's lease and the CronJob's
``activeDeadlineSeconds``, so an unbounded sweep would take the cause-list crawl
down with it. Every run takes a probe ``budget`` and reports what it deferred —
a silent cap reads as "we covered everything" when it didn't.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

from django.utils import timezone

from courts.scraper.base import NGM_DB, upsert_from_detail
from courts.scraper.registers import DEFAULT_TAIL_PROBE, register_gaps

logger = logging.getLogger(__name__)

#: Seconds between probes. The court-orders capture measured ~1,200 cases/hr
#: off-peak with no per-IP wall; 3s is deliberately below that ceiling.
DEFAULT_DELAY = 3.0

#: Probes per run. The sweep rides a CronJob with a 4h deadline that also has to
#: finish the cause-list crawl, so this stays small and the work resumes next run.
DEFAULT_BUDGET = 200

#: How long before a known-absent number becomes eligible for re-probing. A court
#: can issue a number later than its neighbours, so "missing" is provisional.
#:
#: Eligible is not the same as scheduled: re-checks are strictly lower priority
#: than never-probed candidates (see :func:`_prioritise`). Without that rule a
#: large court livelocks — ``kathmandudc`` implies ~84,000 candidates and a run
#: covers ``budget`` of them, so the earliest absences age back in long before the
#: list is exhausted and every run re-walks the same prefix forever.
RECHECK_AFTER_DAYS = 90

#: Consecutive transport failures before abandoning the run. A WAF block or an
#: outage otherwise burns the whole budget writing nothing.
ERROR_ABORT_THRESHOLD = 10


@dataclass
class SweepStats:
    court_id: str
    #: Candidates the portal answered for — ``created + missing + existing``.
    probed: int = 0
    created: int = 0
    missing: int = 0
    existing: int = 0
    errors: int = 0
    #: Candidates this run did not resolve, whether it ran out of budget, aborted,
    #: or errored on them. ``probed + errors + deferred == len(candidates)``, so a
    #: capped run can never read as full coverage.
    deferred: int = 0
    aborted: bool = False

    @property
    def attempts(self) -> int:
        """Portal round-trips spent. What the budget is actually denominated in."""
        return self.probed + self.errors

    def as_dict(self) -> dict:
        return {**asdict(self), "attempts": self.attempts}


def _prioritise(candidates: list[str], court_id: str, *, using: str) -> list[str]:
    """Order candidates for a budgeted run: never-probed first, then re-checks.

    ``candidates`` arrives in register priority order (newest year, tail first).
    This layers the negative cache on top, and the ordering — not the filtering —
    is the important part. A number inside the re-check horizon is dropped; one
    past it goes to the BACK, behind everything never probed.

    That is what makes progress monotone. Filtering alone doesn't: a court with
    more candidates than ``budget × runs-per-horizon`` sees its earliest absences
    become eligible again before the list is exhausted, and since they sort first
    they crowd out every candidate the sweep has yet to reach.
    """
    from datetime import timedelta

    from courts.models import RegisterProbe

    probed_at = dict(
        RegisterProbe.objects.using(using)
        .filter(court_id=court_id)
        .values_list("case_number", "last_probed_at")
    )
    if not probed_at:
        return candidates

    cutoff = timezone.now() - timedelta(days=RECHECK_AFTER_DAYS)
    fresh, recheck = [], []
    for number in candidates:
        seen = probed_at.get(number)
        if seen is None:
            fresh.append(number)
        elif seen < cutoff:
            recheck.append(number)
    return fresh + recheck


def _record_absence(court_id: str, case_number: str, *, using: str) -> None:
    from django.db import IntegrityError
    from django.db.models import F

    from courts.models import RegisterProbe

    # UPDATE first so an existing row's miss_count advances without a read; the
    # explicit last_probed_at is required because QuerySet.update() does not fire
    # auto_now. The create is racy against uniq_register_probe — two workers on
    # one court would collide — so a lost race just re-runs the update.
    def bump():
        return (
            RegisterProbe.objects.using(using)
            .filter(court_id=court_id, case_number=case_number)
            .update(miss_count=F("miss_count") + 1, last_probed_at=timezone.now())
        )

    if bump():
        return
    try:
        RegisterProbe.objects.using(using).create(court_id=court_id, case_number=case_number)
    except IntegrityError:
        bump()


def run_sweep(
    spec,
    *,
    fetch,
    court_id: str,
    budget: int = DEFAULT_BUDGET,
    delay: float = DEFAULT_DELAY,
    tail_probe: int = DEFAULT_TAIL_PROBE,
    series=None,
    write: bool = False,
    using: str = NGM_DB,
    on_progress=None,
    sleep=time.sleep,
) -> SweepStats:
    """Probe this court's missing register slots, creating whatever the court has.

    ``write=False`` (the default) probes and reports without touching the DB —
    the dry run. ``spec`` is a registry entry; a tier without ``crawl_detail``
    cannot be swept and returns empty stats. ``series`` narrows the walk to named
    registers, which is how a targeted backfill stays proportionate: sweeping the
    special court's ``CR`` alone is 72 probes, against 647 for every series.
    """
    stats = SweepStats(court_id=court_id)
    crawl_detail = getattr(spec, "crawl_detail", None)
    if crawl_detail is None:
        logger.info("sweep: %s has no crawl_detail; skipping", court_id)
        return stats
    if budget <= 0:
        return stats

    candidates = register_gaps(
        court_id, using=using, tail_probe=tail_probe, series=series
    )
    if write:
        candidates = _prioritise(candidates, court_id, using=using)
    if not candidates:
        return stats

    consecutive_errors = 0
    for index, case_number in enumerate(candidates):
        # Budget is denominated in portal round-trips, NOT in successful probes:
        # counting only successes lets a flaky court issue many times the budgeted
        # requests (a 9-in-10 failure rate would spend 10x) inside a cron window
        # it shares with the cause-list crawl.
        if stats.attempts >= budget:
            stats.deferred = len(candidates) - index
            break
        try:
            enrichment = crawl_detail(fetch, court_id, case_number)
        except Exception as exc:  # noqa: BLE001 - transport/parse flake, never a "missing"
            stats.errors += 1
            consecutive_errors += 1
            logger.warning("sweep: %s %s probe failed: %s", court_id, case_number, exc)
            if consecutive_errors >= ERROR_ABORT_THRESHOLD:
                stats.aborted = True
                stats.deferred = len(candidates) - index - 1
                logger.error(
                    "sweep: %s aborting after %d consecutive errors (likely blocked)",
                    court_id, consecutive_errors,
                )
                break
            if delay:
                sleep(delay)
            continue

        consecutive_errors = 0
        stats.probed += 1
        found = enrichment is not None and enrichment.identifies_a_case()
        try:
            if not found:
                stats.missing += 1
                if write:
                    _record_absence(court_id, case_number, using=using)
            elif write:
                if upsert_from_detail(court_id, case_number, enrichment, using=using):
                    stats.created += 1
                else:
                    # Raced, or already present — never re-enriched (that would
                    # drop resolved nes_id links).
                    stats.existing += 1
        except Exception:
            # A DB failure on one candidate must not lose the rest of the run's
            # work; the job's own retry covers a genuinely broken database.
            logger.exception("sweep: %s %s could not be recorded", court_id, case_number)

        if on_progress is not None:
            on_progress(court_id, case_number)
        if delay:
            sleep(delay)

    logger.info("sweep: %s %s", court_id, stats.as_dict())
    return stats
