"""
Management command to enrich CIAA DRAFT cases with structured case overview
sections via LLM extraction from evidence documents.

Usage::

    python manage.py enrich_case_overview --dry-run
    python manage.py enrich_case_overview --limit 10
    python manage.py enrich_case_overview --case-id case-XXXX
    python manage.py enrich_case_overview --verbose

Sections generated: short_description, क) अभियोगपत्रको सार, ख) आकर्षित कानुनी
व्यवस्था, ग) प्रमाणको सार संक्षेप. Idempotent — skips cases with populated
description by default.

Environment variables::

    ANTHROPIC_API_KEY          — API key for Anthropic (fallback)
    JAWAFDEHI_LLM_API_KEY      — API key for Jawafdehi LLM proxy
    JAWAFDEHI_LLM_PROXY_URL    — base URL for Jawafdehi LLM proxy
    OPENCODE_API_KEY           — API key for OpenCode Go
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand

from cases.models import Case, CaseState, CaseType
from cases.services.section_generation import (
    SYSTEM_PROMPT,
    SectionEvidence,
    SectionGenerationResult,
    SectionGenerationService,
    SectionQualityError,
    build_section_prompt,
    extract_case_evidence,
    parse_llm_response,
    validate_section_html,
    SECTION_SPECS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

SECTION_ORDER = ("ka", "kha", "ga")


class DjangoLLMClient:
    """Async wrapper around the sync Jawafdehi LLM proxy."""

    def __init__(self, model: str, timeout: int = 180):
        self.model = model
        self.timeout = timeout

    async def generate(
        self, *, system_prompt: str, user_prompt: str, max_tokens: int
    ) -> str:
        from caseworker.services import LLMService

        service = LLMService()

        def call() -> str:
            prompt = f"{system_prompt}\n\n{user_prompt}"
            return service.invoke(prompt)

        text = await asyncio.to_thread(call)
        json.loads(text)
        return text


class Command(BaseCommand):
    help = "Enrich CIAA DRAFT cases with structured case overview sections"

    def __init__(self):
        super().__init__()
        self.stats: dict[str, int | dict] = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_failed": 0,
            "cases_no_content": 0,
            "sections_generated": 0,
            "sections_cached": 0,
            "quality_warnings": 0,
        }

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Preview without saving to database"
        )
        parser.add_argument(
            "--limit", type=int, default=None, help="Process only N cases"
        )
        parser.add_argument(
            "--case-id", type=str, default=None, help="Process a single case by case_id"
        )
        parser.add_argument(
            "--verbose", action="store_true", help="Detailed per-section logging"
        )
        parser.add_argument(
            "--model",
            type=str,
            default="claude-sonnet-4-6",
            help="LLM model override (default: claude-sonnet-4-6)",
        )
        parser.add_argument(
            "--skip-existing",
            action="store_true",
            default=True,
            help="Skip cases with populated description (default: true)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-process cases that already have a description",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        case_id = options.get("case_id")
        verbose = options["verbose"]
        model = options["model"]
        force = options["force"]

        if verbose:
            logger.setLevel(logging.DEBUG)

        self.stdout.write(
            self.style.WARNING(
                f"{'[DRY RUN] ' if dry_run else ''}"
                "Starting case overview enrichment..."
            )
        )

        start_time = time.time()
        cases = self._get_eligible_cases(limit, case_id, force)
        self.stdout.write(f"Found {len(cases)} eligible DRAFT case(s) to process")

        llm_client = DjangoLLMClient(model=model)
        service = SectionGenerationService(llm_client, model=model)

        for idx, case in enumerate(cases, 1):
            try:
                self.stdout.write(
                    f"\n[{idx}/{len(cases)}] {case.case_id} — {case.title[:80]}"
                )
                self._process_case(case, service, dry_run, verbose)
            except Exception:
                self.stats["cases_failed"] += 1
                logger.exception("Error processing %s", case.case_id)
                self.stdout.write(
                    self.style.ERROR(f"FAILED: {case.case_id}")
                )

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)
        elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        self._print_summary(dry_run, elapsed_str)

    def _get_eligible_cases(
        self, limit: int | None, case_id: str | None, force: bool
    ) -> list[Case]:
        queryset = Case.objects.filter(
            state=CaseState.DRAFT, case_type=CaseType.CORRUPTION
        )

        if case_id:
            queryset = queryset.filter(case_id=case_id)

        cases = list(queryset)

        if not force:
            cases = [
                c for c in cases
                if not c.description or not c.description.strip()
            ]

        if limit is not None:
            if limit < 0:
                raise ValueError(f"--limit must be >= 0, got {limit}")
            cases = cases[:limit]

        return cases

    def _process_case(
        self,
        case: Case,
        service: SectionGenerationService,
        dry_run: bool,
        verbose: bool,
    ) -> None:
        self.stats["cases_processed"] += 1

        evidence = extract_case_evidence(case)
        if not evidence:
            self.stats["cases_no_content"] += 1
            self.stdout.write(
                self.style.WARNING("  SKIPPED: No evidence documents found")
            )
            return

        if verbose:
            for item in evidence:
                self.stdout.write(
                    f"  Evidence: {item.source_id} [{item.source_type}] "
                    f"{item.title[:60]} ({len(item.text)} chars)"
                )

        try:
            results = asyncio.run(
                service.generate_core_sections(case, evidence)
            )
        except Exception:
            self.stats["cases_failed"] += 1
            logger.exception("LLM generation failed for %s", case.case_id)
            self.stdout.write(self.style.ERROR("  FAILED: LLM generation error"))
            return

        self._report_results(results, verbose)

        for result in results.values():
            if result.from_cache:
                self.stats["sections_cached"] += 1
            else:
                self.stats["sections_generated"] += 1
            if result.quality_issues:
                self.stats["quality_warnings"] += len(result.quality_issues)

        if dry_run:
            self.stats["cases_enriched"] += 1
            self.stdout.write(
                self.style.SUCCESS(f"  [DRY RUN] Generated {len(results)} section(s)")
            )
            return

        try:
            self._save_results(case, results)
            self.stats["cases_enriched"] += 1
            self.stdout.write(
                self.style.SUCCESS(f"  ENRICHED: {len(results)} section(s) saved")
            )
        except Exception:
            self.stats["cases_failed"] += 1
            logger.exception("Save failed for %s", case.case_id)
            self.stdout.write(self.style.ERROR("  FAILED: Database save error"))

    def _report_results(
        self, results: dict[str, SectionGenerationResult], verbose: bool
    ) -> None:
        for key, result in results.items():
            source = "cache" if result.from_cache else "llm"
            issues = ""
            if result.quality_issues:
                issues = f" [issues: {', '.join(result.quality_issues[:2])}]"
            self.stdout.write(
                f"  {result.key}: confidence={result.confidence} "
                f"source={source}{issues}"
            )
            if verbose:
                preview = result.html[:150].replace("\n", " ")
                self.stdout.write(f"    Preview: {preview}...")

    def _save_results(
        self, case: Case, results: dict[str, SectionGenerationResult]
    ) -> None:
        if "short_description" in results:
            case.short_description = results["short_description"].html

        description_parts: list[str] = []
        for key in SECTION_ORDER:
            if key in results:
                description_parts.append(results[key].html)

        if description_parts:
            case.description = "\n\n".join(description_parts)

        case.save(update_fields=["short_description", "description", "updated_at"])

    def _print_summary(self, dry_run: bool, elapsed_str: str) -> None:
        self.stdout.write("\n" + "=" * 60)
        label = "[DRY RUN] " if dry_run else ""
        self.stdout.write(self.style.WARNING(f"{label}SUMMARY"))
        self.stdout.write("=" * 60)
        self.stdout.write(f"Total time:          {elapsed_str}")
        self.stdout.write(f"Cases processed:     {self.stats['cases_processed']}")
        self.stdout.write(
            self.style.SUCCESS(f"Cases enriched:      {self.stats['cases_enriched']}")
        )
        self.stdout.write(
            self.style.WARNING(f"Cases skipped:       {self.stats['cases_skipped']}")
        )
        self.stdout.write(
            self.style.WARNING(f"Cases no content:    {self.stats['cases_no_content']}")
        )
        if self.stats["cases_failed"]:
            self.stdout.write(
                self.style.ERROR(f"Cases failed:        {self.stats['cases_failed']}")
            )
        self.stdout.write(f"Sections generated:  {self.stats['sections_generated']}")
        self.stdout.write(f"Sections cached:     {self.stats['sections_cached']}")
        if self.stats["quality_warnings"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Quality warnings:     {self.stats['quality_warnings']}"
                )
            )
        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis was a dry run. No changes were made to the database."
                )
            )
            self.stdout.write("Run without --dry-run to apply changes.")
