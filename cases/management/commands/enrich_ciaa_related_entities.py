"""
Management command to enrich CIAA DRAFT cases with related and location entities.

Extracts entities via LLM from press releases and court orders and creates JawafEntity
and CaseEntityRelationship records.

Usage::

    python manage.py enrich_ciaa_related_entities --dry-run
    python manage.py enrich_ciaa_related_entities --limit 10
    python manage.py enrich_ciaa_related_entities --llm-model claude-sonnet-4-5 --verbose
"""

import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cases.management.commands._enrich_utils import (
    call_llm,
    convert_to_markdown,
    parse_extraction_response,
    resolve_api_key,
)
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

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Slicing constants
# ------------------------------------------------------------------
COURT_ORDER_SMALL_THRESHOLD = 8_000
COURT_ORDER_MEDIUM_THRESHOLD = 80_000
COURT_ORDER_HEAD_TAIL_SMALL = 3_000
COURT_ORDER_LARGE_HEAD = 4_000
COURT_ORDER_LARGE_TAIL = 4_000
COURT_ORDER_LARGE_WINDOW = 2_000
COURT_ORDER_LARGE_WINDOW_COUNT = 3

PRESS_RELEASE_CHARS = 1_200

PROMPT_TARGET_MIN = 5_000
PROMPT_TARGET_MAX = 12_000
PROMPT_HARD_MAX = 20_000

DOCUMENT_FORMAT_PRIORITY = {".docx": 4, ".doc": 3, ".pdf": 2}


SYSTEM_PROMPT = """You are an expert Nepali legal data extractor.
Analyze the provided corruption case documents (press releases and/or court order excerpts).
Extract two types of entities connected to the case:

1. LOCATION ENTITIES (relationship_type="location"):
   The district, municipality, rural municipality, or province where the corruption
   occurred. For CIAA cases this is typically stated in the first line of the case
   title or press release. One entity per distinct location.

2. RELATED ENTITIES (relationship_type="related"):
   Any person or organization materially connected to the case beyond the primary
   accused. Prioritize these categories in order:
   - Government bodies: ministries, departments, municipalities, local government
     offices whose funds were misused or where the accused worked
   - Companies and contractors: private firms, cooperatives, contractors,
     suppliers, or consultants involved as beneficiaries of fraud or procurement
     irregularities
   - Family members: spouses, children, or relatives in whose name illegal assets
     were held (critical for CIAA illegal wealth cases — the spouse's name almost
     always appears as a co-holder of assets)
   - Co-defendants and associates: secondary actors not listed as primary accused
   - Beneficiaries: individuals or institutions that received misappropriated funds
     or advantages
   - Witnesses: key witnesses named directly in the court order or press release
   - Legal actors: judges, lawyers, advocates, or procedural staff named in the
     verdict or proceedings when they are materially connected to the case
   - Any other person or institution the source documents identify as directly
     involved

Output exactly a JSON object with an "entities" key containing an array:
{
  "entities": [
    {
      "entity_name": "Name of the entity (Nepali if Nepali text, else English)",
      "relationship_type": "location" or "related",
      "notes": "One short phrase describing their connection. For locations, leave blank. Examples: '\\u0906\\u0930\\u094b\\u092a\\u093f\\u0924\\u0915\\u094b \\u0936\\u094d\\u0930\\u0940\\u092e\\u0924\\u0940, \\u0938\\u092e\\u094d\\u092a\\u0924\\u094d\\u0924\\u093f \\u0939\\u0938\\u094d\\u0924\\u093e\\u0928\\u094d\\u0924\\u0930\\u0923 \\u0917\\u0930\\u093f\\u090f\\u0915\\u094b', '\\u0920\\u0947\\u0915\\u094d\\u0915\\u093e \\u092a\\u094d\\u0930\\u0926\\u093e\\u092f\\u0915 \\u0938\\u0902\\u0938\\u094d\\u0925\\u093e', '\\u092a\\u0940\\u0921\\u093f\\u0924 \\u0938\\u0930\\u0915\\u093e\\u0930\\u0940 \\u0928\\u093f\\u0915\\u093e\\u092f', '\\u0938\\u0939-\\u0906\\u0930\\u094b\\u092a\\u093f\\u0924'"
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
            default=os.environ.get(
                "JAWAFDEHI_LLM_PROXY_URL", "http://localhost:11434/v1"
            ),
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
        limit = options["limit"]
        force = options["force"]
        is_verbose = options["verbose"]

        if limit is not None and limit < 0:
            raise CommandError("--limit must be >= 0")

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

        if not force:
            already_enriched_ids = CaseEntityRelationship.objects.filter(
                relationship_type=RelationshipType.RELATED
            ).values_list("case_id", flat=True)
            qs = qs.exclude(id__in=already_enriched_ids)

        qs = qs.order_by("case_id")

        cases = list(qs)
        if limit is not None:
            cases = cases[:limit]

        self.stdout.write(f"Found {len(cases)} cases to process")

        self._fetch_source_cache(cases)

        with requests.Session() as session:
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
            for item in case.evidence or []:
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
        for item in case.evidence or []:
            if isinstance(item, dict) and isinstance(item.get("source_id"), str):
                source = self._source_lookup.get(item["source_id"])
                if source is not None:
                    sources.append(source)
        return sources

    # ------------------------------------------------------------------
    # Press release detection (Problem 1)
    # ------------------------------------------------------------------

    def _is_press_release_source(self, source):
        """Check if a source should be treated as a CIAA press release.

        Matches when:
        - source_type is OFFICIAL_GOVERNMENT, OR
        - description contains "CIAA Press Release", OR
        - any URL contains "ciaa.gov.np/pressrelease"
        """
        if source.source_type == SourceType.OFFICIAL_GOVERNMENT:
            return True

        description = (source.description or "").lower()
        if "ciaa press release" in description:
            return True

        urls = [
            url.strip()
            for url in (source.url or [])
            if isinstance(url, str) and url.strip()
        ]
        for url in urls:
            if "ciaa.gov.np/pressrelease" in url.lower():
                return True

        return False

    def _score_source_for_press_release(self, source):
        """Score a source for suitability as a CIAA press release.

        Higher score = better match. Returns 0 for non-press-release sources.
        """
        if not self._is_press_release_source(source):
            return 0

        corpus_parts = [
            source.title or "",
            source.description or "",
            source.uploaded_filename or "",
        ]
        urls = [
            url.strip()
            for url in (source.url or [])
            if isinstance(url, str) and url.strip()
        ]
        corpus_parts.append(" ".join(urls))

        for uploaded in source.uploaded_files.all():
            corpus_parts.append(uploaded.filename or Path(uploaded.file.name).name)

        corpus = " ".join(corpus_parts).lower()

        score = 0

        # Direct source_type match
        if source.source_type == SourceType.OFFICIAL_GOVERNMENT:
            score += 5

        # Press release keywords
        press_keywords = [
            "press release",
            "pressrelease",
            "press-release",
            "प्रेस विज्ञप्ति",
            "विज्ञप्ति",
        ]
        if any(kw in corpus for kw in press_keywords):
            score += 8

        # CIAA-specific keywords
        ciaa_keywords = ["ciaa", "अख्तियार"]
        if any(kw in corpus for kw in ciaa_keywords):
            score += 3

        # Direct CIAA press release URL
        if any("ciaa.gov.np/pressrelease" in u.lower() for u in urls):
            score += 10

        # Has DOC/DOCX URLs (editable formats)
        if any(u.lower().endswith(".docx") for u in urls):
            score += 4
        elif any(u.lower().endswith(".doc") for u in urls):
            score += 2
        elif any(u.lower().endswith(".pdf") for u in urls):
            score += 1

        return score

    def _get_press_release_source(self, case):
        """Return the best press release source for this case.

        Scores all evidence sources and returns the highest-scoring one.
        """
        sources = self._get_evidence_sources(case)
        if not sources:
            return None

        ranked = sorted(
            ((self._score_source_for_press_release(s), s) for s in sources),
            key=lambda row: row[0],
            reverse=True,
        )
        best_score, best_source = ranked[0]
        return best_source if best_score > 0 else None

    def _get_court_order_source(self, case):
        """Return the best court order source for this case."""
        for source in self._get_evidence_sources(case):
            if source.source_type == SourceType.LEGAL_COURT_ORDER:
                return source
        return None

    # ------------------------------------------------------------------
    # URL deduplication and ranking (Problems 2 & 3)
    # ------------------------------------------------------------------

    @staticmethod
    def _deduplicate_urls(urls):
        """Group URLs by filename stem, keeping only the best format per group.

        Priority: DOCX > DOC > PDF > other

        Example: foo.pdf, foo.doc → keeps foo.doc only
        """
        from urllib.parse import urlparse

        stems = {}  # stem -> [(priority, url)]
        for url in urls:
            parsed = urlparse(url)
            path = Path(parsed.path)
            stem = path.stem
            suffix = path.suffix.lower()
            priority = DOCUMENT_FORMAT_PRIORITY.get(suffix, 0)

            if stem not in stems:
                stems[stem] = []
            stems[stem].append((priority, url))

        result = []
        for _stem, entries in stems.items():
            entries.sort(key=lambda x: x[0], reverse=True)
            result.append(entries[0][1])

        return result

    @staticmethod
    def _ranked_press_release_urls(source):
        """Return URLs ranked by conversion preference for press releases.

        Priority: DOCX > DOC > PDF > other (e.g., CIAA webpage HTML)
        """
        urls = [
            url.strip()
            for url in (source.url or [])
            if isinstance(url, str) and url.strip()
        ]
        if not urls:
            return []

        scored = []
        for url in urls:
            parsed = urlparse(url)
            suffix = Path(parsed.path).suffix.lower()
            priority = DOCUMENT_FORMAT_PRIORITY.get(suffix, 0)
            # Non-document URLs (like webpage) get lowest priority
            scored.append((priority, url))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Return unique in priority order
        seen = set()
        result = []
        for _priority, url in scored:
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    # ------------------------------------------------------------------
    # Document conversion
    # ------------------------------------------------------------------

    def _convert_source_to_markdown(self, source, session, is_press_release=False):
        """Convert a DocumentSource to markdown text.

        For press releases: tries DOCX → DOC → uploaded_file → PDF → webpage
        For court orders: tries uploaded files first, then URLs.
        """
        if is_press_release:
            return self._convert_press_release_to_markdown(source, session)
        return self._convert_court_order_to_markdown(source, session)

    def _convert_press_release_to_markdown(self, source, session):
        """Convert press release source. Priority: DOCX > DOC > uploaded > PDF > HTML."""
        ranked_urls = self._ranked_press_release_urls(source)

        # Try each URL in priority order
        for url in ranked_urls:
            md = convert_to_markdown(url, session)
            if md:
                return md

        # Try uploaded files as fallback
        uploaded_md = self._convert_uploaded_file(source)
        if uploaded_md:
            return uploaded_md

        # Fallback to description
        if source.description and len(source.description.strip()) >= 500:
            return source.description

        return None

    def _convert_court_order_to_markdown(self, source, session):
        """Convert court order source. Uploaded files first, then URLs."""
        uploaded_md = self._convert_uploaded_file(source)
        if uploaded_md:
            return uploaded_md

        urls = [
            url.strip()
            for url in (source.url or [])
            if isinstance(url, str) and url.strip()
        ]
        for url in urls:
            md = convert_to_markdown(url, session)
            if md:
                return md

        if source.description and len(source.description.strip()) >= 500:
            return source.description

        return None

    def _convert_uploaded_file(self, source):
        """Download and convert the best uploaded file for a source via markitdown/likhit."""
        try:
            import likhit  # noqa: F401
            from markitdown import MarkItDown
        except ImportError as exc:
            raise CommandError(
                "markitdown and likhit are required for document conversion."
            ) from exc

        file_field = source.uploaded_file
        if not file_field:
            uploaded_files = list(source.uploaded_files.all())
            if uploaded_files and uploaded_files[0].file:
                file_field = uploaded_files[0].file

        if not file_field:
            return None

        suffix = Path(file_field.name).suffix or ""
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp_path = tmp.name
        try:
            with file_field.open("rb") as in_file:
                while True:
                    chunk = in_file.read(8192)
                    if not chunk:
                        break
                    tmp.write(chunk)
            tmp.close()

            converter = MarkItDown(enable_plugins=True)
            result = converter.convert(tmp_path)
            if (
                result
                and result.text_content
                and len(result.text_content.strip()) > 200
            ):
                return result.text_content.strip()
            return None
        finally:
            tmp.close()
            Path(tmp_path).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Intelligent truncation (Problems 4 & 5)
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_press_release(text):
        """Truncate press release to first 1200 chars."""
        if not text:
            return text
        return text[:PRESS_RELEASE_CHARS]

    @staticmethod
    def _truncate_court_order(text):
        """Intelligently truncate court order based on length.

        < 8k chars   → entire document
        8k-80k       → first 3000 + last 3000
        > 80k        → first 4000 + 3×2000 evenly-spaced windows + last 4000
        """
        if not text:
            return text

        text_len = len(text)

        if text_len < COURT_ORDER_SMALL_THRESHOLD:
            return text

        if text_len <= COURT_ORDER_MEDIUM_THRESHOLD:
            return (
                text[:COURT_ORDER_HEAD_TAIL_SMALL]
                + "\n\n[...middle section omitted...]\n\n"
                + text[-COURT_ORDER_HEAD_TAIL_SMALL:]
            )

        # > 100k: head + 3 evenly-spaced windows + tail
        window_spacing = max(
            1,
            (text_len - COURT_ORDER_LARGE_HEAD - COURT_ORDER_LARGE_TAIL)
            // (COURT_ORDER_LARGE_WINDOW_COUNT + 1),
        )
        parts = [text[:COURT_ORDER_LARGE_HEAD]]

        for i in range(1, COURT_ORDER_LARGE_WINDOW_COUNT + 1):
            center = COURT_ORDER_LARGE_HEAD + i * window_spacing
            start = max(0, center - COURT_ORDER_LARGE_WINDOW // 2)
            end = min(text_len, start + COURT_ORDER_LARGE_WINDOW)
            parts.append(f"\n\n[...window {i}...]\n\n")
            parts.append(text[start:end])

        parts.append("\n\n[...final section...]\n\n")
        parts.append(text[-COURT_ORDER_LARGE_TAIL:])

        return "".join(parts)

    @staticmethod
    def _enforce_prompt_budget(parts, is_verbose=False):
        """Ensure combined prompt stays within budget. Truncates largest part if over."""
        combined = "\n\n".join(parts)
        total = len(combined)

        if total <= PROMPT_HARD_MAX:
            return combined

        # Find the largest part and truncate it further
        largest_idx = max(range(len(parts)), key=lambda i: len(parts[i]))
        current_overage = total - PROMPT_HARD_MAX

        original = parts[largest_idx]
        if len(original) > current_overage + 1000:
            parts[largest_idx] = original[: len(original) - current_overage - 100]
            if is_verbose:
                logger.debug(
                    "Prompt over budget (%d chars). Truncated part %d from %d to %d.",
                    total,
                    largest_idx,
                    len(original),
                    len(parts[largest_idx]),
                )

        combined = "\n\n".join(parts)
        return combined[:PROMPT_HARD_MAX]

    # ------------------------------------------------------------------
    # Case processing
    # ------------------------------------------------------------------

    @staticmethod
    def _format_case_number(case):
        """Extract case number from court_cases JSON field."""
        court_cases = getattr(case, "court_cases", None)
        if isinstance(court_cases, list) and court_cases:
            first = court_cases[0]
            if isinstance(first, str) and ":" in first:
                return first.split(":", 1)[1]
        return ""

    def _process_case(self, case, options, api_key, session, is_verbose):
        is_dry_run = options["dry_run"]
        case_number = self._format_case_number(case)
        title_trunc = (case.title or "")[:120]

        self.stdout.write(
            f"[CASE START] case_number={case_number} "
            f"case_id={case.case_id} title={title_trunc}"
        )

        content_parts = []

        # Press release — first 1200 chars
        pr_source = self._get_press_release_source(case)
        if pr_source:
            pr_md = self._convert_source_to_markdown(
                pr_source, session, is_press_release=True
            )
            if pr_md:
                truncated = self._truncate_press_release(pr_md)
                content_parts.append("--- PRESS RELEASE ---")
                content_parts.append(truncated)
                self.stdout.write(
                    f"press_release={pr_source.source_id} "
                    f"chars={len(pr_md)} used={len(truncated)}"
                )
            elif is_verbose:
                self.stdout.write(
                    f"  Press release: {pr_source.source_id} — conversion failed"
                )
        elif is_verbose:
            self.stdout.write("  No press release source found")

        # Court order — intelligent truncation
        co_source = self._get_court_order_source(case)
        if co_source:
            co_md = self._convert_source_to_markdown(
                co_source, session, is_press_release=False
            )
            if co_md:
                co_len = len(co_md)
                truncated = self._truncate_court_order(co_md)
                self.stdout.write(
                    f"court_order={co_source.source_id} "
                    f"chars={co_len} used={len(truncated)}"
                )
                content_parts.append("--- COURT ORDER ---")
                content_parts.append(truncated)
            elif is_verbose:
                self.stdout.write(
                    f"  Court order: {co_source.source_id} — conversion failed"
                )
        elif is_verbose:
            self.stdout.write("  No court order source found")

        if not content_parts:
            self.stats["cases_skipped"] += 1
            self.stdout.write(
                self.style.WARNING("  SKIPPED: No document content found")
            )
            return

        user_prompt = self._enforce_prompt_budget(content_parts, is_verbose)

        if is_verbose:
            self.stdout.write(f"  Prompt size: {len(user_prompt)} chars")

        if user_prompt.strip() == "":
            self.stats["cases_skipped"] += 1
            self.stdout.write(
                self.style.WARNING("  SKIPPED: Empty prompt after truncation")
            )
            return

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
            self.stdout.write(self.style.WARNING(f"  SKIPPED: LLM call failed — {e}"))
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

            nes_id = self._link_nes(name, session)
            with transaction.atomic():
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
                _rel, rel_created = CaseEntityRelationship.objects.get_or_create(
                    case=case,
                    entity=entity,
                    relationship_type=relationship_type_enum,
                    defaults={"notes": notes},
                )
                if rel_created:
                    self.stats["relationships_created"] += 1
