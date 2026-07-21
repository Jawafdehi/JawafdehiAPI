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
"""

from django.core.management.base import BaseCommand, CommandError

from courts.scraper import registry
from jobs import queue as jobs_queue


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

    def handle(self, *args, **o):
        try:
            tiers = registry.resolve(o["court"])
        except KeyError as exc:
            raise CommandError(str(exc)) from exc

        enqueued = 0
        for tier in tiers:
            module = registry.REGISTRY[tier]
            # court_ids(None): every module enumerates its courts statically
            # (fetch-free), so the enqueuer never touches the network.
            for court_id in module.court_ids(None):
                payload = {"court": tier, "court_id": court_id, "enrich": o["enrich"]}
                if o["lookback_days"] is not None:
                    payload["lookback_days"] = o["lookback_days"]
                if o["limit_dates"] is not None:
                    payload["limit_dates"] = o["limit_dates"]
                job = jobs_queue.enqueue(
                    kind="court_scrape",
                    payload=payload,
                    dedup_key=f"court_scrape:{tier}:{court_id}",
                    priority=o["priority"],
                )
                self.stdout.write(f"  {tier}/{court_id}: job {job.pk} [{job.status}]")
                enqueued += 1
        self.stdout.write(f"enqueued {enqueued} court_scrape job(s).")
