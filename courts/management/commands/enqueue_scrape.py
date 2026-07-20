"""Enqueue ``court_scrape`` jobs — the thin recurring trigger for the scraper.

The central queue owns execution (lease/retry/backoff/dedup); this command only
posts one job per court, so a small CronJob can drive recurring scrapes without
the queue growing its own scheduler (see ``docs/jobs-queue-design.md`` §6.3). The
``court_scrape:<court>`` dedup key means re-enqueuing a court whose scrape is
still queued or running is a no-op — no pile-ups, no two overlapping crawls of
one court.

    manage.py enqueue_scrape --court all
    manage.py enqueue_scrape --court special --lookback-days 30 --enrich
"""

from django.core.management.base import BaseCommand, CommandError

from courts.scraper import registry
from jobs import queue as jobs_queue


class Command(BaseCommand):
    help = "Enqueue one court_scrape job per court onto the central queue."

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
            keys = registry.resolve(o["court"])
        except KeyError as exc:
            raise CommandError(str(exc)) from exc

        for key in keys:
            payload = {"court": key, "enrich": o["enrich"]}
            if o["lookback_days"] is not None:
                payload["lookback_days"] = o["lookback_days"]
            if o["limit_dates"] is not None:
                payload["limit_dates"] = o["limit_dates"]
            job = jobs_queue.enqueue(
                kind="court_scrape",
                payload=payload,
                dedup_key=f"court_scrape:{key}",
                priority=o["priority"],
            )
            self.stdout.write(f"  {key}: job {job.pk} [{job.status}]")
