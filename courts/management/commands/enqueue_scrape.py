"""Enqueue ``court_scrape`` jobs — the thin recurring trigger for the scraper.

The central queue owns execution (lease/retry/backoff/dedup); this command only
posts jobs, so a small CronJob can drive recurring scrapes without the queue
growing its own scheduler (see ``docs/jobs-queue-design.md`` §6.3).

One job per LEAF court (not per tier): a `district` tier has ~77 courts, so
posting one job each keeps every job small (bounded dates, one lease, per-court
dedup/retry) instead of one giant job crawling all 77 under a single lease. The
``court_scrape:<tier>:<court_id>`` dedup key means re-enqueuing a court whose
scrape is still queued or running is a no-op — no pile-ups, no two overlapping
crawls of one court.

    manage.py enqueue_scrape --court all
    manage.py enqueue_scrape --court special --lookback-days 30 --enrich
    manage.py enqueue_scrape --court special --sweep --sweep-budget 200
    manage.py enqueue_scrape --court all --sweep --sweep-courts 4 --sweep-budget 150
    manage.py enqueue_scrape --court special --sweep --sweep-series CR --sweep-tail 100
"""

from django.core.management.base import BaseCommand, CommandError

from courts.scraper import registry
from jobs import queue as jobs_queue
from jobs.models import Job


def least_recently_swept(targets: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    """The ``limit`` courts whose last sweep finished longest ago, never-swept first.

    A recurring sweep cannot queue all 99 courts at once: ``scrape_worker --once``
    drains the queue sequentially in a single pod under one ``activeDeadlineSeconds``
    that the cause-list crawl also has to fit inside. Rotating a small slice per run
    keeps each tick bounded while still reaching every court eventually.

    The rotation cursor is the job history itself — no extra state to keep in sync,
    and it self-corrects: a court whose sweep failed keeps its old timestamp and so
    stays at the front of the queue rather than losing its turn.
    """
    swept_at: dict[str, object] = {}
    rows = (
        Job.objects.filter(kind="court_scrape", payload__sweep=True)
        .values_list("payload__court_id", "completed_at")
    )
    for court_id, completed_at in rows:
        if completed_at is None:
            continue
        known = swept_at.get(court_id)
        if known is None or completed_at > known:
            swept_at[court_id] = completed_at
    # Stable within a tier: courts never swept keep their registry order.
    ordered = sorted(
        enumerate(targets),
        key=lambda item: (swept_at.get(item[1][1]) is not None, swept_at.get(item[1][1]), item[0]),
    )
    return [target for _, target in ordered[:limit]]


class Command(BaseCommand):
    help = "Enqueue one court_scrape job per leaf court onto the central queue."

    def add_arguments(self, parser):
        parser.add_argument("--court", default="all",
                            help="special | district | high | supreme | all")
        parser.add_argument("--lookback-days", type=int, default=None,
                            help="override the court's default lookback window")
        parser.add_argument("--limit-dates", type=int, default=None,
                            help="cap dates per court (smoke/testing)")
        parser.add_argument("--enrich", action="store_true",
                            help="follow each touched case's detail page")
        parser.add_argument("--priority", type=int, default=100,
                            help="queue priority (lower runs sooner)")
        parser.add_argument("--sweep", action="store_true",
                            help="enqueue REGISTER-SWEEP jobs instead of cause-list "
                                 "ones: walk each court's docket numbering and fetch "
                                 "the cases that never reached a hearing list")
        parser.add_argument("--sweep-budget", type=int, default=None,
                            help="max probes per court per run "
                                 "(default: courts.scraper.sweep.DEFAULT_BUDGET)")
        parser.add_argument("--sweep-series", default=None,
                            help="restrict the sweep to named registers, comma separated "
                                 "(e.g. CR). Series differ sharply in value and cost — on "
                                 "the special court CR is 72 holes of corruption cases, OA "
                                 "is 575 mostly-procedural ones")
        parser.add_argument("--sweep-tail", type=int, default=None,
                            help="probe this far past each register's high-water mark "
                                 "(default: courts.scraper.registers.DEFAULT_TAIL_PROBE). "
                                 "Raise it for a one-off backfill; the default is sized for "
                                 "a recurring run across thousands of registers")
        parser.add_argument("--sweep-courts", type=int, default=None,
                            help="sweep only the N least-recently-swept courts, so a "
                                 "recurring run rotates through the fleet on a bounded "
                                 "budget instead of queueing all 99 at once")

    def handle(self, *args, **o):
        try:
            tiers = registry.resolve(o["court"])
        except KeyError as exc:
            raise CommandError(str(exc)) from exc
        if o["sweep_courts"] is not None and not o["sweep"]:
            raise CommandError("--sweep-courts only applies to --sweep runs.")

        # court_ids(None): every module enumerates its courts statically
        # (fetch-free), so the enqueuer never touches the network.
        targets = [
            (tier, court_id)
            for tier in tiers
            for court_id in registry.REGISTRY[tier].court_ids(None)
        ]
        total = len(targets)
        if o["sweep"] and o["sweep_courts"]:
            targets = least_recently_swept(targets, o["sweep_courts"])

        for tier, court_id in targets:
            payload = {"court": tier, "court_id": court_id, "enrich": o["enrich"]}
            if o["lookback_days"] is not None:
                payload["lookback_days"] = o["lookback_days"]
            if o["limit_dates"] is not None:
                payload["limit_dates"] = o["limit_dates"]
            # A sweep is the same job kind with the cause-list half off. It gets
            # its OWN dedup namespace so it can sit alongside a court's cause-list
            # job instead of being deduped away by it.
            suffix = ""
            if o["sweep"]:
                payload.update({"sweep": True, "causelist": False})
                if o["sweep_budget"] is not None:
                    payload["sweep_budget"] = o["sweep_budget"]
                if o["sweep_tail"] is not None:
                    payload["sweep_tail"] = o["sweep_tail"]
                if o["sweep_series"]:
                    series = [x.strip().upper() for x in o["sweep_series"].split(",") if x.strip()]
                    payload["sweep_series"] = series
                    # Its own dedup namespace: a CR sweep must not be deduped away
                    # by an in-flight all-series one, or vice versa.
                    suffix = ":sweep:" + "+".join(series)
                else:
                    suffix = ":sweep"
            job = jobs_queue.enqueue(
                kind="court_scrape",
                payload=payload,
                dedup_key=f"court_scrape:{tier}:{court_id}{suffix}",
                priority=o["priority"],
            )
            self.stdout.write(f"  {tier}/{court_id}{suffix}: job {job.pk} [{job.status}]")

        label = "register-sweep" if o["sweep"] else "cause-list"
        self.stdout.write(f"enqueued {len(targets)} {label} court_scrape job(s).")
        if len(targets) < total:
            # Never let a rotation read as fleet coverage.
            self.stdout.write(
                f"  rotation: {total - len(targets)} of {total} court(s) wait for a later run."
            )
