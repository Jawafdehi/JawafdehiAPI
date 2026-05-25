import asyncio
import json
import logging
import sys
import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from cases.models import Case, CaseState, CaseType
from cases.services.section_generation import (
    ALL_SECTION_KEYS,
    CORE_SECTION_KEYS,
    COURT_STAGE_KEYS,
    SECTION_SPECS,
    SectionGenerationService,
    build_readiness_check,
    extract_case_evidence,
)
from caseworker.services import LLMService

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

MAX_LIMIT = 500
SECTION_DELAY = 0.5
CASE_DELAY = 2.0

KA_KHA_GA_ORDER: tuple[str, ...] = ("ka", "kha", "ga")
GHA_TO_JA_ORDER: tuple[str, ...] = ("gha", "nga", "cha", "chha", "ja")


class DjangoLLMClient:
    def __init__(self, model: str | None = None):
        self.service = LLMService()
        self._model_override = model

    async def generate(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        def call():
            prompt = f"{system_prompt}\n\n{user_prompt}"
            text = self.service.invoke(prompt)
            json.loads(text)
            return text

        return await asyncio.to_thread(call)


class Command(BaseCommand):
    help = (
        "Generate CIAA case overview sections (क–ज) for DRAFT corruption cases, "
        "concatenate into Case.description, and generate missing_details."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--case-id",
            type=str,
            default=None,
            help="Process a single Case.case_id instead of batch.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help=f"Max cases to process in batch mode (1-{MAX_LIMIT}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Generate sections without saving Case fields.",
        )
        parser.add_argument(
            "--skip-existing",
            action="store_true",
            help="Skip cases that already have a populated description.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Clear section_generation_cache before generating.",
        )
        parser.add_argument(
            "--core-only",
            action="store_true",
            help="Generate only core sections (क, ख, ग); skip conditional court-stage.",
        )
        parser.add_argument(
            "--show-readiness",
            action="store_true",
            help="Print section readiness report and exit without generating (requires --case-id).",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help="LLM model override for this run.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable detailed per-case logging.",
        )
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Required when DEBUG=False to run this command in production.",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        if not settings.DEBUG and not options["allow_production"]:
            raise CommandError(
                "This command refuses to run in production unless --allow-production is provided."
            )

        limit = options["limit"]
        if limit < 1 or limit > MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}.")

        case_id = options.get("case_id")
        if options["show_readiness"] and not case_id:
            raise CommandError("--show-readiness requires --case-id.")

        if case_id:
            self._process_single(case_id, options)
        else:
            self._process_batch(options)

    def _process_single(self, case_id: str, options):
        case = Case.objects.get(case_id=case_id, case_type=CaseType.CORRUPTION)
        self._process_case(case, options)

    def _process_batch(self, options):
        queryset = (
            Case.objects.filter(state=CaseState.DRAFT, case_type=CaseType.CORRUPTION)
            .order_by("-created_at")
        )

        if options["skip_existing"]:
            queryset = queryset.filter(
                Q(description__isnull=True) | Q(description="")
            )

        cases = list(queryset[: options["limit"]])
        if not cases:
            self.stdout.write("No eligible DRAFT corruption cases found.")
            return

        stats = {
            "cases_processed": 0,
            "cases_skipped": 0,
            "sections_generated": 0,
            "errors": 0,
        }

        for i, case in enumerate(cases):
            try:
                result = self._process_case(case, options)
                if result is None:
                    stats["cases_skipped"] += 1
                else:
                    stats["cases_processed"] += 1
                    stats["sections_generated"] += result
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"[FAIL] {case.case_id}: {type(exc).__name__}: {exc}"
                    )
                )
            if i < len(cases) - 1:
                time.sleep(CASE_DELAY)

        self.stdout.write(
            f"Processed={stats['cases_processed']} "
            f"Skipped={stats['cases_skipped']} "
            f"Sections={stats['sections_generated']} "
            f"Errors={stats['errors']}"
        )

    def _process_case(self, case: Case, options) -> int | None:
        if options["skip_existing"] and case.description and case.description.strip():
            self.stdout.write(
                self.style.WARNING(f"[SKIP] {case.case_id}: description already populated.")
            )
            return None

        if options["force"]:
            version_info = dict(case.versionInfo or {})
            version_info.pop("section_generation_cache", None)
            case.versionInfo = version_info
            case.save(update_fields=["versionInfo", "updated_at"])

        evidence = extract_case_evidence(case)
        if not evidence:
            self.stdout.write(
                self.style.WARNING(f"[SKIP] {case.case_id}: no evidence found.")
            )
            return None

        readiness = build_readiness_check(case, evidence)

        if options["show_readiness"]:
            self._print_readiness_report(readiness, case.case_id)
            return None

        model = options.get("model")
        service = SectionGenerationService(DjangoLLMClient(model=model), model=model or "claude-opus-4-7")

        if options["core_only"]:
            results = asyncio.run(
                service.generate_core_sections(case, evidence, section_keys=CORE_SECTION_KEYS)
            )
        else:
            results = asyncio.run(
                service.generate_all_sections(
                    case, evidence, include_conditional=True, section_delay=SECTION_DELAY
                )
            )

        if options["dry_run"]:
            self._print_dry_run(case.case_id, results, readiness)
            return len(results)

        # Assemble description in fixed क→ज order
        short_html = results.get("short_description")
        if short_html:
            case.short_description = short_html.html

        description_parts: list[str] = []
        for key in KA_KHA_GA_ORDER:
            if key in results:
                description_parts.append(results[key].html)
        for key in GHA_TO_JA_ORDER:
            if key in results:
                description_parts.append(results[key].html)
        case.description = "\n\n".join(description_parts) if description_parts else ""

        # Build missing_details for skipped / empty / low-confidence sections
        missing = self._build_missing_details(results, readiness)
        case.missing_details = missing or ""

        case.save(update_fields=["short_description", "description", "missing_details", "updated_at"])

        n_core = len([k for k in CORE_SECTION_KEYS if k in results])
        n_court = len([k for k in COURT_STAGE_KEYS if k in results])
        logger.info(
            "Generated %d sections for %s (core=%d, court=%d)%s",
            len(results),
            case.case_id,
            n_core,
            n_court,
            " (missing_details populated)" if missing else "",
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"[OK] {case.case_id}: {len(results)} sections (short_desc + "
                f"{n_core - 1} core + {n_court} court){' with missing_details' if missing else ''}"
            )
        )
        return len(results)

    def _build_missing_details(self, results, readiness) -> str | None:
        generated = set(results.keys())
        all_active = set(readiness.all_active_keys())
        skipped = all_active - generated

        lines: list[str] = []
        for key in sorted(skipped):
            spec = SECTION_SPECS.get(key)
            title = spec.title if spec else key
            lines.append(f"{key} ({title}): section generation skipped or returned empty")

        for key, result in results.items():
            if result.confidence == "low":
                spec = SECTION_SPECS.get(key)
                title = spec.title if spec else key
                lines.append(f"{key} ({title}): low confidence generation")

        return "\n".join(lines) if lines else None

    def _print_dry_run(self, case_id: str, results, readiness) -> None:
        self.stdout.write(self.style.SUCCESS(f"[DRY-RUN] {case_id}"))
        for key in ALL_SECTION_KEYS:
            if key in results:
                result = results[key]
                spec_title = SECTION_SPECS.get(key, None)
                title = spec_title.title if spec_title else key
                self.stdout.write(f"  {key} ({title}): confidence={result.confidence} cache={result.from_cache}")
            else:
                check = readiness.check_section(key)
                if check.active:
                    self.stdout.write(self.style.WARNING(f"  {key}: active but not generated"))
                else:
                    self.stdout.write(f"  {key}: {check.reason}")
        self.stdout.write("---")

    def _print_readiness_report(self, readiness, case_id: str) -> None:
        self.stdout.write(self.style.SUCCESS(f"Section Readiness Report for {case_id}"))
        self.stdout.write(f"  court_cases: {readiness.court_cases or '[]'}")
        self.stdout.write(f"  evidence text length: {len(readiness.evidence_text)} chars")
        self.stdout.write("")
        for key in ALL_SECTION_KEYS:
            result = readiness.check_section(key)
            status = self.style.SUCCESS("ACTIVE  ") if result.active else self.style.WARNING("INACTIVE")
            stage = f" [{result.court_stage.value}]" if result.court_stage else ""
            self.stdout.write(f"  {status} {key}{stage} — {result.reason}")
