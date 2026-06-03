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
import tempfile
from pathlib import Path

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    DocumentSource,
    JawafEntity,
    RelationshipType,
    SourceType,
)
from cases.services.priority_case_loader import filter_by_priority, load_priority_cases
from cases.management.commands._enrich_utils import (
    call_llm,
    convert_to_markdown,
    parse_extraction_response,
    resolve_api_key,
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

Output exactly a JSON object with an "entities" key containing an array:
{
  "entities": [
    {
      "entity_name": "Name of the entity (Nepali if Nepali text, else English)",
      "relationship_type": "location" or "related",
      "notes": "One short phrase describing their connection (e.g., 'आरोपितको श्रीमती, सम्पत्ति हस्तान्तरण गरिएको', 'ठेक्का प्रदायक संस्था'). For locations, leave blank."
    }
  ]
}
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
        self._source_lookup = {}

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

        qs = Case.objects.filter(state=CaseState.DRAFT)

        if options["priority"]:
            priority_list = load_priority_cases()
            logger.info(
                "Priority mode: loaded %d case numbers across all fiscal years",
                len(priority_list),
            )
            qs = filter_by_priority(qs, priority_list)

        # Skip cases that already have related entities
        if not force:
            already_enriched_ids = CaseEntityRelationship.objects.filter(
                relationship_type=RelationshipType.RELATED
            ).values_list("case_id", flat=True)
            qs = qs.exclude(id__in=already_enriched_ids)

        qs = qs.order_by("case_id")

        cases = list(qs)
        if limit:
            cases = cases[:limit]

        self.stdout.write(f"Found {len(cases)} cases to process")

        # Build source lookup from case evidence
        self._fetch_source_cache(cases)

        session = requests.Session()

        for idx, case in enumerate(cases, 1):
            self.stats["cases_processed"] += 1
            if is_verbose:
                self.stdout.write(
                    f"[{idx}/{len(cases)}] Processing case {case.case_id}..."
                )
            else:
                self.stdout.write(f"Processing case {case.case_id}...")
            self._process_case(case, options, api_key, session, is_verbose)

        self.stdout.write(self.style.SUCCESS(f"Finished. Stats: {self.stats}"))

    # ------------------------------------------------------------------
    # Source lookup
    # ------------------------------------------------------------------

    def _fetch_source_cache(self, cases):
        """Pre-fetch DocumentSource objects for all evidence references."""
        source_ids = set()
        for case in cases:
            for item in (case.evidence or []):
                if isinstance(item, dict) and isinstance(item.get("source_id"), str):
                    if item["source_id"].strip():
                        source_ids.add(item["source_id"])
        self._source_lookup = {
            source.source_id: source
            for source in DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            ).prefetch_related("uploaded_files")
        }
        logger.debug("Cached %d DocumentSource records", len(self._source_lookup))

    def _get_evidence_sources(self, case):
        """Return DocumentSource objects referenced in case.evidence."""
        sources = []
        for item in (case.evidence or []):
            if isinstance(item, dict) and isinstance(item.get("source_id"), str):
                source = self._source_lookup.get(item["source_id"])
                if source is not None:
                    sources.append(source)
        return sources

    def _get_press_release_source(self, case):
        """Return the best press release source for this case."""
        for source in self._get_evidence_sources(case):
            if source.source_type == SourceType.OFFICIAL_GOVERNMENT:
                return source
        return None

    def _get_court_order_source(self, case):
        """Return the best court order source for this case."""
        for source in self._get_evidence_sources(case):
            if source.source_type == SourceType.LEGAL_COURT_ORDER:
                return source
        return None

    # ------------------------------------------------------------------
    # Document conversion
    # ------------------------------------------------------------------

    def _convert_source_to_markdown(self, source, session):
        """Convert a DocumentSource to markdown text.

        Tries uploaded files first, then URLs via convert_to_markdown,
        then falls back to source.description.
        """
        # Try uploaded files
        uploaded_file = source.uploaded_file
        if not uploaded_file:
            uploaded = source.uploaded_files.first()
            if uploaded and uploaded.file:
                uploaded_file = uploaded.file

        if uploaded_file:
            try:
                return self._convert_uploaded_file(uploaded_file)
            except Exception as exc:
                logger.warning(
                    "Failed to convert uploaded file for %s: %s",
                    source.source_id,
                    exc,
                )

        # Try URLs
        urls = [
            url.strip()
            for url in (source.url or [])
            if isinstance(url, str) and url.strip()
        ]
        for url in urls:
            md = convert_to_markdown(url, session)
            if md:
                return md

        # Fallback to description
        if source.description and len(source.description.strip()) >= 500:
            return source.description

        return None

    def _convert_uploaded_file(self, file_field):
        """Download an uploaded file to temp and convert via markitdown/likhit."""
        try:
            import likhit  # noqa: F401
            from markitdown import MarkItDown
        except ImportError as exc:
            raise CommandError(
                "markitdown and likhit are required for document conversion."
            ) from exc

        suffix = Path(file_field.name).suffix or ""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            with file_field.open("rb") as in_file:
                while True:
                    chunk = in_file.read(8192)
                    if not chunk:
                        break
                    tmp.write(chunk)

        try:
            converter = MarkItDown(enable_plugins=True)
            result = converter.convert(tmp_path)
            if result and result.text_content and len(result.text_content.strip()) > 200:
                return result.text_content.strip()
            return None
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Case processing
    # ------------------------------------------------------------------

    def _process_case(self, case, options, api_key, session, is_verbose):
        is_dry_run = options["dry_run"]

        content_parts = []

        # Press release — short, always the first 1200 chars
        pr_source = self._get_press_release_source(case)
        if pr_source:
            pr_md = self._convert_source_to_markdown(pr_source, session)
            if pr_md:
                content_parts.append("--- PRESS RELEASE ---")
                content_parts.append(pr_md[:1200])
                if is_verbose:
                    self.stdout.write(
                        f"  Press release: {pr_source.source_id} ({len(pr_md)} chars)"
                    )
            else:
                if is_verbose:
                    self.stdout.write(
                        f"  Press release: {pr_source.source_id} — conversion failed"
                    )
        else:
            if is_verbose:
                self.stdout.write("  No press release source found")

        # Court order — character-based slicing
        co_source = self._get_court_order_source(case)
        if co_source:
            co_md = self._convert_source_to_markdown(co_source, session)
            if co_md:
                co_len = len(co_md)
                if is_verbose:
                    self.stdout.write(
                        f"  Court order: {co_source.source_id} ({co_len} chars)"
                    )
                if co_len < 5_000:
                    content_parts.append("--- COURT ORDER (FULL) ---")
                    content_parts.append(co_md)
                elif co_len > 15_000:
                    content_parts.append("--- COURT ORDER HEADER ---")
                    content_parts.append(co_md[:4000])
                    content_parts.append("--- COURT ORDER VERDICT SECTION ---")
                    content_parts.append(co_md[-3000:])
                else:
                    content_parts.append("--- COURT ORDER (FULL) ---")
                    content_parts.append(co_md)
            else:
                if is_verbose:
                    self.stdout.write(
                        f"  Court order: {co_source.source_id} — conversion failed"
                    )
        else:
            if is_verbose:
                self.stdout.write("  No court order source found")

        if not content_parts:
            self.stats["cases_skipped"] += 1
            self.stdout.write(
                self.style.WARNING("  SKIPPED: No document content found")
            )
            return

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

            entities_data = parse_extraction_response(response, {"entities"})

            if not entities_data:
                self.stats["cases_skipped"] += 1
                self.stdout.write(
                    self.style.WARNING("  SKIPPED: No entities extracted")
                )
                return

            self._apply_entities(case, entities_data, is_dry_run, session)
            self.stats["cases_enriched"] += 1

        except CommandError as e:
            self.stats["cases_skipped"] += 1
            self.stdout.write(
                self.style.WARNING(f"  SKIPPED: LLM call failed — {e}")
            )
        except Exception as e:
            self.stats["cases_failed"] += 1
            logger.exception("Failed processing case %s", case.case_id)
            self.stdout.write(self.style.ERROR(f"  ERROR: {e}"))

    # ------------------------------------------------------------------
    # NES linking
    # ------------------------------------------------------------------

    def _link_nes(self, name, session):
        """Attempt NES name search; return nes_id on confident match or None."""
        from django.conf import settings

        nes_url = getattr(settings, "NES_API_URL", None)
        if not nes_url:
            return None

        try:
            res = session.get(
                f"{nes_url.rstrip('/')}/search",
                params={"q": name},
                timeout=5,
            )
            if res.status_code == 200:
                data = res.json()
                results = data.get("results", [])
                if results:
                    best_match = results[0]
                    if best_match.get("confidence", 0) > 0.8:
                        return best_match.get("id")
        except Exception as e:
            logger.debug("NES lookup failed for %s: %s", name, e)
        return None

    # ------------------------------------------------------------------
    # Entity persistence
    # ------------------------------------------------------------------

    def _apply_entities(self, case, entities_data, is_dry_run, session):
        for item in entities_data:
            name = item.get("entity_name")
            rel_type = item.get("relationship_type")
            notes = item.get("notes", "")

            if not name or rel_type not in ("location", "related"):
                continue

            if is_dry_run:
                self.stdout.write(
                    f"  [DRY RUN] Would create {rel_type} entity: {name}"
                    + (f" (notes: {notes})" if notes else "")
                )
                continue

            with transaction.atomic():
                nes_id = self._link_nes(name, session)

                entity, created = JawafEntity.objects.get_or_create(
                    display_name=name,
                    defaults={"nes_id": nes_id},
                )
                if not created and not entity.nes_id and nes_id:
                    entity.nes_id = nes_id
                    entity.save(update_fields=["nes_id"])

                if created:
                    self.stats["entities_created"] += 1

                relationship_type_enum = (
                    RelationshipType.LOCATION
                    if rel_type == "location"
                    else RelationshipType.RELATED
                )
                rel, rel_created = CaseEntityRelationship.objects.get_or_create(
                    case=case,
                    entity=entity,
                    relationship_type=relationship_type_enum,
                    defaults={"notes": notes},
                )
                if rel_created:
                    self.stats["relationships_created"] += 1
