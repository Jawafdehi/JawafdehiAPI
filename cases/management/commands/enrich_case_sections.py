import asyncio
import json
import logging
import sys

from django.core.management.base import BaseCommand

from cases.models import Case, CaseType
from cases.services.section_generation import SectionGenerationService, extract_case_evidence
from caseworker.services import LLMService

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


class DjangoLLMClient:
    def __init__(self):
        self.service = LLMService()

    async def generate(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        def call():
            prompt = f"{system_prompt}\n\n{user_prompt}"
            text = self.service.invoke(prompt)
            json.loads(text)
            return text

        return await asyncio.to_thread(call)


class Command(BaseCommand):
    help = (
        "Generate CIAA case overview sections: core (क, ख, ग) + conditional court-stage "
        "sections (घ, ङ, च, छ, ज) based on evidence and court_cases field."
    )

    def add_arguments(self, parser):
        parser.add_argument("--case-id", required=True, help="Case.case_id to enrich")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Generate sections without saving Case fields",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Clear section_generation_cache before generating",
        )
        parser.add_argument(
            "--conditional",
            action="store_true",
            default=True,
            help="Enable conditional court-stage section generation (default: on)",
        )
        parser.add_argument(
            "--core-only",
            action="store_true",
            help="Generate only core sections (क, ख, ग), skip conditional",
        )
        parser.add_argument(
            "--show-readiness",
            action="store_true",
            help="Print section readiness report and exit without generating",
        )

    def handle(self, *args, **options):
        case = Case.objects.get(case_id=options["case_id"], case_type=CaseType.CORRUPTION)
        if options["force"]:
            version_info = dict(case.versionInfo or {})
            version_info.pop("section_generation_cache", None)
            case.versionInfo = version_info
            case.save(update_fields=["versionInfo", "updated_at"])

        evidence = extract_case_evidence(case)
        if not evidence:
            self.stderr.write(self.style.ERROR("No case evidence found"))
            return

        from cases.services.section_generation import (
            CORE_SECTION_KEYS,
            build_readiness_check,
        )

        readiness = build_readiness_check(case, evidence)

        if options["show_readiness"]:
            self._print_readiness_report(readiness)
            return

        service = SectionGenerationService(DjangoLLMClient())

        if options["core_only"]:
            results = asyncio.run(
                service.generate_core_sections(case, evidence, section_keys=CORE_SECTION_KEYS)
            )
        else:
            results = asyncio.run(
                service.generate_all_sections(
                    case, evidence, include_conditional=options["conditional"]
                )
            )

        if options["dry_run"]:
            for key, result in results.items():
                spec_title = results.get(key) and key
                logger.info(
                    "%s (%s) confidence=%s cache=%s",
                    key,
                    spec_title,
                    result.confidence,
                    result.from_cache,
                )
                self.stdout.write(result.html)
                self.stdout.write("\n---\n")
            return

        case.short_description = results["short_description"].html
        core_section_html = "\n\n".join(
            results[key].html for key in ("ka", "kha", "ga") if key in results
        )
        court_html_parts = [
            results[key].html
            for key in ("gha", "nga", "cha", "chha", "ja")
            if key in results
        ]
        if court_html_parts:
            case.description = "\n\n".join([core_section_html] + court_html_parts)
        else:
            case.description = core_section_html
        case.save(update_fields=["short_description", "description", "updated_at"])
        logger.info(
            "Generated %d sections for %s (core=%d, court=%d)",
            len(results),
            case.case_id,
            len([k for k in CORE_SECTION_KEYS if k in results]),
            len([k for k in ("gha", "nga", "cha", "chha", "ja") if k in results]),
        )

    def _print_readiness_report(self, readiness):
        from cases.services.section_generation import ALL_SECTION_KEYS

        self.stdout.write(self.style.SUCCESS("Section Readiness Report"))
        self.stdout.write(f"  court_cases: {readiness.court_cases or '[]'}")
        self.stdout.write(f"  evidence text length: {len(readiness.evidence_text)} chars")
        self.stdout.write("")
        for key in ALL_SECTION_KEYS:
            result = readiness.check_section(key)
            status = self.style.SUCCESS("ACTIVE  ") if result.active else self.style.WARNING("INACTIVE")
            stage = f" [{result.court_stage.value}]" if result.court_stage else ""
            self.stdout.write(f"  {status} {key}{stage} — {result.reason}")
