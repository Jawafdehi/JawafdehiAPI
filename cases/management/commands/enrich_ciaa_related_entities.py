"""
Management command to enrich CIAA DRAFT cases with related and location entities.

Extracts entities via LLM from press releases and court orders and creates JawafEntity
and CaseEntityRelationship records.

Usage::

    python manage.py enrich_ciaa_related_entities --dry-run
    python manage.py enrich_ciaa_related_entities --limit 10
    python manage.py enrich_ciaa_related_entities --llm-model claude-sonnet-4-5 --verbose
"""

import json
import logging
import os
import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cases.models import CIAACase, JawafEntity, CaseEntityRelationship
from cases.management.commands._enrich_utils import (
    resolve_api_key,
    call_llm,
    convert_to_markdown,
    parse_extraction_response,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert Nepali legal data extractor.
Analyze the provided corruption case documents (press releases and/or court order excerpts).
Extract two types of entities connected to the case:

1. Location Entities: The district and municipality where the corruption occurred.
2. Related Entities: Any person or organization materially connected to the case beyond the primary accused.
   Examples include:
   - The government body, department, ministry, or local government whose funds were misused.
   - Companies, cooperatives, contractors, or private firms involved as beneficiaries.
   - Family members in whose name illegal assets were held (e.g., spouses).
   - Co-defendants or associates playing a secondary role.
   - Witnesses named directly in the court order.
   - Any other person or institution directly involved.

Output exactly a JSON array of objects with the following schema:
[
  {
    "entity_name": "Name of the entity (Nepali if Nepali text, else English)",
    "relationship_type": "location" or "related",
    "notes": "One short phrase describing their connection (e.g., 'आरोपितको श्रीमती, सम्पत्ति हस्तान्तरण गरिएको', 'ठेक्का प्रदायक संस्था'). For locations, leave blank."
  }
]
No other text.
"""


class Command(BaseCommand):
    help = "Extract related and location entities from CIAA cases using LLM"

    def __init__(self):
        super().__init__()
        self.stats = {
            "cases_processed": 0,
            "cases_skipped": 0,
            "cases_enriched": 0,
            "cases_failed": 0,
            "entities_created": 0,
            "relationships_created": 0,
        }

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without saving to database",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process only N cases",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default=os.environ.get("JAWAFDEHI_ALLEGATION_MODEL", "claude-sonnet-4-5"),
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=os.environ.get("JAWAFDEHI_LLM_PROXY_URL", "http://localhost:11434/v1"),
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            help="Override API key from env",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-process cases that already have related entities",
        )
        parser.add_argument(
            "--priority",
            action="store_true",
            help="Only process priority cases from priority_cases.json",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Detailed logging",
        )

    def handle(self, *args, **options):
        is_dry_run = options["dry_run"]
        limit = options["limit"]
        force = options["force"]
        is_verbose = options["verbose"]

        if is_verbose:
            logging.getLogger().setLevel(logging.DEBUG)

        api_key = resolve_api_key(options["llm_api_key"])
        if not api_key:
            raise CommandError("LLM API key not found in env or args")

        qs = CIAACase.objects.filter(status="DRAFT")

        if options["priority"]:
            priority_path = os.path.join("priority_cases.json")
            if os.path.exists(priority_path):
                with open(priority_path) as f:
                    data = json.load(f)
                    reg_numbers = [c["registration_number"] for c in data]
                    qs = qs.filter(registration_number__in=reg_numbers)
            else:
                self.stdout.write(self.style.WARNING("priority_cases.json not found"))

        # Skip logic
        if not force:
            qs = qs.exclude(entities__caseentityrelationship__relationship_type="related")

        qs = qs.order_by("registration_number")

        cases = list(qs)
        if limit:
            cases = cases[:limit]

        self.stdout.write(f"Found {len(cases)} cases to process")

        session = requests.Session()

        for case in cases:
            self.stats["cases_processed"] += 1
            self.stdout.write(f"Processing case {case.registration_number}...")

            docs = case.documents.all()
            press_releases = [d for d in docs if d.source_type == "OFFICIAL_GOVERNMENT"]
            court_orders = [d for d in docs if d.source_type == "LEGAL_COURT_ORDER"]

            content_parts = []

            if press_releases:
                pr = press_releases[0]
                md = convert_to_markdown(pr.url, session)
                if md:
                    content_parts.append("--- PRESS RELEASE ---")
                    content_parts.append(md[:1200])
            
            if court_orders:
                co = court_orders[0]
                md = convert_to_markdown(co.url, session)
                if md:
                    lines = md.split('\n')
                    header = '\n'.join(lines[:100])
                    footer = '\n'.join(lines[-100:])
                    content_parts.append("--- COURT ORDER HEADER ---")
                    content_parts.append(header)
                    content_parts.append("--- COURT ORDER VERDICT SECTION ---")
                    content_parts.append(footer)

            if not content_parts:
                self.stats["cases_skipped"] += 1
                self.stdout.write(self.style.WARNING("No document content found, skipping"))
                continue

            user_prompt = "\n\n".join(content_parts)

            try:
                response = call_llm(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    model=options["llm_model"],
                    base_url=options["llm_base_url"],
                    api_key=api_key,
                    session=session,
                )
                
                entities_data = parse_extraction_response(response, {"entities", "related_entities", "location_entities"})
                
                if not entities_data:
                    self.stdout.write(self.style.WARNING("No entities extracted"))
                    continue
                    
                self._apply_entities(case, entities_data, is_dry_run, session)
                self.stats["cases_enriched"] += 1

            except Exception as e:
                self.stats["cases_failed"] += 1
                logger.exception(f"Failed processing case {case.registration_number}")
                self.stdout.write(self.style.ERROR(f"Error: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Finished. Stats: {self.stats}"))

    
    def _link_nes(self, name, session):
        # NES linking: Attempt a name search against the NES API at NES_API_URL from settings.
        # If a confident match is found, set nes_id. If not or if NES is unreachable, create with display_name only.
        from django.conf import settings
        nes_url = getattr(settings, "NES_API_URL", None)
        if not nes_url:
            return None
            
        try:
            res = session.get(f"{nes_url.rstrip('/')}/search", params={"q": name}, timeout=5)
            if res.status_code == 200:
                data = res.json()
                results = data.get("results", [])
                if results and len(results) > 0:
                    best_match = results[0]
                    if best_match.get("confidence", 0) > 0.8:
                        return best_match.get("id")
        except Exception as e:
            logger.debug(f"NES lookup failed for {name}: {e}")
        return None

    def _apply_entities(self, case, entities_data, is_dry_run, session):
        for item in entities_data:
            name = item.get("entity_name")
            rel_type = item.get("relationship_type")
            notes = item.get("notes", "")

            if not name or rel_type not in ("location", "related"):
                continue

            if is_dry_run:
                self.stdout.write(f"  [DRY RUN] Would create {rel_type} entity: {name} (notes: {notes})")
                continue

            with transaction.atomic():
                nes_id = self._link_nes(name, session)
                
                entity, created = JawafEntity.objects.get_or_create(
                    display_name=name,
                    defaults={
                        "entity_type": "ORGANIZATION" if rel_type == "location" else "UNKNOWN",
                        "nes_id": nes_id
                    }
                )
                if not created and not entity.nes_id and nes_id:
                    entity.nes_id = nes_id
                    entity.save(update_fields=["nes_id"])
                    
                if created:
                    self.stats["entities_created"] += 1

                rel, rel_created = CaseEntityRelationship.objects.get_or_create(
                    case=case,
                    entity=entity,
                    relationship_type=rel_type,
                    defaults={"notes": notes}
                )
                if rel_created:
                    self.stats["relationships_created"] += 1

