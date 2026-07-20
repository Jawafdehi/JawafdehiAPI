"""Crawl Nepal court cause-lists into the ngm court_cases lake.

Replaces the retired standalone NGM Scrapy service: date-driven, frontier-aware,
writing via the courts ORM with write-time case_status normalization. The parse +
write paths are unit/DB-tested; this command adds the live HTTP fetch.

Dry-run by default (fetch + parse, no writes). ``--write`` persists; ``--enrich``
also follows each case's detail page.

    manage.py scrape_courtcases --court special --limit-dates 5            # dry-run
    manage.py scrape_courtcases --court special --write --enrich           # persist
    manage.py scrape_courtcases --court all --lookback-days 30 --write
"""

from __future__ import annotations

from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from courts.scraper import registry
from courts.scraper.crawl import run_crawl
from courts.scraper.fetch import Fetcher


class Command(BaseCommand):
    help = "Crawl court cause-lists (and optionally enrich) into the ngm lake."

    def add_arguments(self, parser):
        parser.add_argument("--court", default="all",
                            help="special | district | high | supreme | all")
        parser.add_argument("--lookback-days", type=int, default=None,
                            help="override the court's default lookback")
        parser.add_argument("--limit-dates", type=int, default=None,
                            help="cap dates per court (smoke/testing)")
        parser.add_argument("--today", default=None,
                            help="AD anchor date YYYY-MM-DD (default: today, KTM)")
        parser.add_argument("--write", action="store_true",
                            help="persist to the ngm DB (default: dry-run)")
        parser.add_argument("--enrich", action="store_true",
                            help="also fetch+apply each case's detail page (implies --write)")

    def handle(self, *args, **o):
        try:
            keys = registry.resolve(o["court"])
        except KeyError as exc:
            raise CommandError(str(exc)) from exc

        today = self._anchor(o["today"])
        enrich = o["enrich"]
        write = o["write"] or enrich
        fetch = Fetcher()
        mode = "WRITE" if write else "DRY-RUN"
        self.stdout.write(f"scrape_courtcases [{mode}] courts={keys} today={today}")

        for key in keys:
            spec = registry.REGISTRY[key]
            stats = run_crawl(
                spec, fetch=fetch, today=today,
                lookback_days=o["lookback_days"], limit_dates=o["limit_dates"],
                write=write, enrich=enrich,
            )
            for s in stats:
                self.stdout.write(
                    f"  {key}/{s.court_id}: dates={s.dates} cases={s.cases} "
                    f"hearings={s.hearings} enriched={s.enriched}"
                )

    @staticmethod
    def _anchor(value: str | None) -> date:
        if not value:
            return timezone.localdate()
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise CommandError(f"--today must be YYYY-MM-DD, got {value!r}") from exc
