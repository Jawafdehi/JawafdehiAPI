"""Enrich CIAA draft cases with tags via rule-based + LLM classification."""

import logging
import sys

from django.core.management.base import BaseCommand
from django.db.models import Q

from cases.models import Case, CaseType, CaseState
from cases.services.tag_enricher import TagEnricher

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Enrich CIAA draft cases with tags using rule-based + LLM classification"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Classify tags without saving to database",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            help="Process a specific case by case_id",
        )
        parser.add_argument(
            "--no-llm",
            action="store_true",
            help="Skip LLM classification, use rules only",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-tag cases that already have tags",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limit number of cases to process",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        case_id = options.get("case_id")
        use_llm = not options["no_llm"]
        force = options["force"]
        limit = options.get("limit")

        if dry_run:
            logger.warning("DRY-RUN MODE: No changes will be saved")

        if case_id:
            cases = Case.objects.filter(case_id=case_id, case_type=CaseType.CORRUPTION)
        else:
            cases = Case.objects.filter(
                case_type=CaseType.CORRUPTION,
                state__in=[CaseState.DRAFT],
            ).order_by("created_at")

            if not force:
                cases = cases.filter(Q(tags__isnull=True) | Q(tags=[]))

        if limit:
            cases = cases[:limit]

        total = cases.count()
        if total == 0:
            logger.info("No cases to process")
            return

        logger.info(f"Processing {total} cases")
        if not use_llm:
            logger.info("LLM classification disabled, using rules only")

        enricher = TagEnricher(use_llm=use_llm)
        stats = enricher.enrich_cases(cases.iterator(), force=force, dry_run=dry_run)

        self._log_summary(stats, dry_run)

    def _log_summary(self, stats: dict, dry_run: bool):
        logger.info("")
        logger.info("=" * 60)
        logger.info("ENRICHMENT SUMMARY")
        logger.info("=" * 60)
        if dry_run:
            logger.warning("DRY-RUN MODE (no changes saved)")
        total = stats["total"]
        logger.info(f"Total processed: {total}")
        if total > 0:
            logger.info(
                f"Enriched:      {stats['enriched']} ({stats['enriched']/total*100:.1f}%)"
            )
            logger.info(
                f"Skipped:       {stats['skipped']} ({stats['skipped']/total*100:.1f}%)"
            )
            logger.info(
                f"Failed:        {stats['failed']} ({stats['failed']/total*100:.1f}%)"
            )
        logger.info("=" * 60)
