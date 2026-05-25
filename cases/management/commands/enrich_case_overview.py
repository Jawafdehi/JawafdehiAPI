"""
Management command to enrich DRAFT cases with structured Nepali overviews
generated from evidence documents via LLM.

Phase 1: Foundation — cmd skeleton, likhit conversion, evidence gathering & routing.

Usage::

    python manage.py enrich_case_overview --dry-run
    python manage.py enrich_case_overview --limit 10
    python manage.py enrich_case_overview --case-id case-XXXX
    python manage.py enrich_case_overview --verbose

Environment variables::

    ANTHROPIC_API_KEY         — API key for Anthropic (fallback)
    JAWAFDEHI_LLM_API_KEY     — API key for Jawafdehi LLM proxy
    JAWAFDEHI_LLM_PROXY_URL   — base URL for Jawafdehi LLM proxy
    JAWAFDEHI_LLM_TIMEOUT_SECONDS — timeout in seconds (default 300)
    OPENCODE_API_KEY          — API key for OpenCode Go
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from cases.models import Case, CaseState
from cases.services.case_overview_enricher import (
    OverviewEnricher,
    build_stats_summary,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
RATE_LIMIT_CASE_DELAY = 2.0
RATE_LIMIT_SECTION_DELAY = 0.5


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


class Command(BaseCommand):
    help = (
        "Enrich DRAFT cases with structured Nepali case overviews "
        "(short_description, sectioned description, missing_details)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without saving to database",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            default=None,
            help="Process a single case by case_id",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process only N cases",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable detailed per-case logging",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=_first_env(
                "JAWAFDEHI_CASEWORK_MODEL",
                default=DEFAULT_MODEL,
            ),
            help=f"LLM model override (default: {DEFAULT_MODEL})",
        )
        parser.add_argument(
            "--skip-existing",
            action="store_true",
            default=True,
            help="Skip cases with populated description (default: true)",
        )
        parser.add_argument(
            "--no-skip-existing",
            action="store_true",
            help="Process cases even if description is already populated",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        case_id = options.get("case_id")
        verbose = options["verbose"]
        model = options["model"]
        skip_existing = options["skip_existing"] and not options["no_skip_existing"]

        if verbose:
            logger.setLevel(logging.DEBUG)

        self.stdout.write(
            self.style.WARNING(
                f"{'[DRY RUN] ' if dry_run else ''}"
                "Starting case overview enrichment (Phase 1: Foundation)..."
            )
        )

        start_time = time.time()

        cases = self._get_eligible_cases(limit, case_id, skip_existing)
        self.stdout.write(
            f"Found {len(cases)} eligible DRAFT case(s) to process"
        )

        enricher = OverviewEnricher(
            model=model,
            dry_run=dry_run,
            verbose=verbose,
            stdout=self.stdout,
            style=self.style,
        )

        for idx, case in enumerate(cases, 1):
            try:
                self.stdout.write(
                    f"\n[{idx}/{len(cases)}] {case.case_id} - {case.title[:80]}..."
                )
                enricher.process_case(case)
            except Exception as exc:
                enricher.stats["cases_failed"] += 1
                logger.exception(f"Error processing {case.case_id}: {exc}")
                self.stdout.write(
                    self.style.ERROR(f"FAILED: {case.case_id} - {exc}")
                )

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)
        elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        self._print_summary(enricher.stats, dry_run, elapsed_str)

    def _get_eligible_cases(self, limit, case_id, skip_existing):
        queryset = Case.objects.filter(state=CaseState.DRAFT)

        if case_id:
            queryset = queryset.filter(case_id=case_id)

        cases = list(queryset)

        if skip_existing:
            cases = [
                c for c in cases
                if not (c.description and c.description.strip())
            ]

        if limit is not None:
            if limit < 0:
                raise CommandError(f"--limit must be >= 0, got {limit}")
            cases = cases[:limit]

        return cases

    def _print_summary(self, stats, dry_run, elapsed_str):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.WARNING(f"{'[DRY RUN] ' if dry_run else ''}SUMMARY")
        )
        self.stdout.write("=" * 60)
        self.stdout.write(f"Total time:          {elapsed_str}")
        self.stdout.write(f"Cases processed:     {stats['cases_processed']}")
        self.stdout.write(
            self.style.SUCCESS(f"Cases enriched:      {stats['cases_enriched']}")
        )
        self.stdout.write(
            self.style.WARNING(f"Cases skipped:       {stats['cases_skipped']}")
        )
        self.stdout.write(
            self.style.WARNING(f"Cases no content:    {stats['cases_no_content']}")
        )
        if stats["cases_failed"] > 0:
            self.stdout.write(
                self.style.ERROR(f"Cases failed:        {stats['cases_failed']}")
            )

        build_stats_summary(stats, self.stdout, self.style)

        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis was a dry run. No changes were made to the database."
                )
            )
            self.stdout.write("Run without --dry-run to apply changes.")
