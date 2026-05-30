"""Enrich CIAA draft cases with tags via rule-based + LLM classification."""

import logging
import sys

from django.core.management.base import BaseCommand
from django.db.models import Q

from cases.models import Case, CaseType, CaseState
from cases.services.priority_case_loader import filter_by_priority, load_priority_cases
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
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_cases",
            help="Enrich all DRAFT CIAA cases (explicit, same as default)",
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
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=None,
            help="OpenAI-compatible LLM base URL (bypasses DB LLMProvider)",
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            default=None,
            help="LLM API key (required when --llm-base-url is set)",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default="gpt-4.5",
            help="LLM model name (default: gpt-4.5)",
        )

    def handle(self, *args, **options):  # noqa
        dry_run = options["dry_run"]
        case_id = options.get("case_id")
        priority = options["priority"]
        all_cases_flag = options.get("all_cases")
        use_llm = not options["no_llm"]
        force = options["force"]
        limit = options.get("limit")
        llm_base_url = (options.get("llm_base_url") or "").strip() or None
        llm_api_key = (options.get("llm_api_key") or "").strip() or None
        llm_model = options.get("llm_model", "gpt-4.5")

        if priority and case_id:
            self.stderr.write(
                self.style.ERROR("--priority and --case-id are mutually exclusive")
            )
            return

        if dry_run:
            logger.warning("DRY-RUN MODE: No changes will be saved")

        llm_client = None
        if use_llm and llm_base_url:
            if not llm_api_key:
                self.stderr.write(
                    self.style.ERROR(
                        "--llm-api-key is required when --llm-base-url is set"
                    )
                )
                return
            llm_client = self._build_llm_client(llm_base_url, llm_api_key, llm_model)
            logger.info(f"LLM client configured: {llm_model} @ {llm_base_url}")
        elif use_llm:
            logger.info("Using DB LLMProvider (no --llm-base-url)")

        if case_id:
            cases = Case.objects.filter(case_id=case_id, case_type=CaseType.CORRUPTION)
        else:
            cases = Case.objects.filter(
                case_type=CaseType.CORRUPTION,
                state__in=[CaseState.DRAFT],
            ).order_by("created_at")

            if priority:
                priority_list = load_priority_cases()
                logger.info(
                    "Priority mode: loaded %d case numbers across all fiscal years",
                    len(priority_list),
                )
                cases = filter_by_priority(cases, priority_list)
            elif not all_cases_flag:
                logger.info(
                    "Processing all DRAFT CIAA cases (default). "
                    "Use --all to make this explicit or --priority to filter."
                )

            if not force:
                cases = cases.filter(Q(tags__isnull=True) | Q(tags=[]))

        if limit is not None:
            cases = cases[:limit]

        total = cases.count()
        if total == 0:
            logger.info("No cases to process")
            return

        logger.info(f"Processing {total} cases")
        if not use_llm:
            logger.info("LLM classification disabled, using rules only")

        auditlog_disabled = False
        try:
            if dry_run:
                from auditlog.registry import auditlog
                from cases.models import DocumentSource

                auditlog.unregister(Case)
                auditlog.unregister(DocumentSource)
                auditlog_disabled = True
                logger.info(
                    "Audit logging suppressed for dry-run (unregistered Case, DocumentSource)"
                )

            enricher = TagEnricher(use_llm=use_llm, llm_client=llm_client)
            stats = enricher.enrich_cases(
                cases.iterator(), force=force, dry_run=dry_run
            )
        finally:
            if auditlog_disabled:
                auditlog.register(Case)
                auditlog.register(DocumentSource)
                logger.debug("Audit logging re-registered")

        self._log_summary(stats, dry_run)

    def _build_llm_client(self, base_url: str, api_key: str, model: str):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            self.stderr.write(
                self.style.ERROR(
                    "langchain-openai not installed. Install with: "
                    "pip install langchain-openai"
                )
            )
            raise

        return ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=0.3,
            max_tokens=1024,
        )

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
                f"Enriched:      {stats['enriched']} ({stats['enriched'] / total * 100:.1f}%)"
            )
            logger.info(
                f"Skipped:       {stats['skipped']} ({stats['skipped'] / total * 100:.1f}%)"
            )
            logger.info(
                f"Failed:        {stats['failed']} ({stats['failed'] / total * 100:.1f}%)"
            )
            logger.info("")
            logger.info("By tier:")
            logger.info(f"  Source LLM:  {stats.get('source_llm', 0)}")
            logger.info(f"  Metadata LLM: {stats.get('metadata_llm', 0)}")
            logger.info(f"  Rule-based:  {stats.get('rule_based', 0)}")
        logger.info("=" * 60)
