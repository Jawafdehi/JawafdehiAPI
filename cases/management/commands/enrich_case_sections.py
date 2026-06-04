import asyncio
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
            return self.service.invoke(prompt)

        return await asyncio.to_thread(call)


class Command(BaseCommand):
    help = "Generate CIAA case overview core sections: short_description, क, ख, ग"

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

        service = SectionGenerationService(DjangoLLMClient())
        results = asyncio.run(service.generate_core_sections(case, evidence))

        if options["dry_run"]:
            for key, result in results.items():
                logger.info("%s confidence=%s cache=%s", key, result.confidence, result.from_cache)
                self.stdout.write(result.html)
            return

        short_desc = results.get("short_description")
        if short_desc:
            case.short_description = short_desc.html
        case.description = "\n\n".join(
            results[key].html for key in ("ka", "kha", "ga") if key in results
        )
        case.save(update_fields=["short_description", "description", "updated_at"])
        logger.info("Generated core sections for %s", case.case_id)
