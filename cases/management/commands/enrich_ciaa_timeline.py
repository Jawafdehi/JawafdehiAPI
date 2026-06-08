"""
Django management command to extract case timeline entries from CIAA source
documents using LLM extraction.

Phase 1 (A.3) of CIAA FY 080/081 Case Enrichment pipeline.
Populates ``Case.timeline`` with chronological entries covering case
progression from investigation through verdict.

Processes all DRAFT cases with empty ``timeline``, regardless of
court case naming conventions.

Idempotent: skips cases with non-empty ``timeline``.

Usage::

    python manage.py enrich_ciaa_timeline --dry-run
    python manage.py enrich_ciaa_timeline --case-id case-0123
    python manage.py enrich_ciaa_timeline --limit 10 --verbose
    python manage.py enrich_ciaa_timeline --fiscal-year 080 --dry-run
    python manage.py enrich_ciaa_timeline --force
"""

import datetime
import logging
import os
import re
import time
import unicodedata
from typing import Optional
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from nepali.datetime import nepalidate as _nepali_date

from cases.management.commands._enrich_utils import (
    ALLOWED_HOSTS,
    call_llm,
    convert_to_markdown,
    extract_court_case_number,
    is_valid_iso_date,
    parse_extraction_response,
    rank_source_urls,
    resolve_api_key,
)
from cases.models import Case, DocumentSource, SourceType
from cases.services.priority_case_loader import filter_by_priority, load_priority_cases
from ngm.services import get_court_case_details

logger = logging.getLogger(__name__)

_SOURCE_TYPE_LABELS = {
    SourceType.LEGAL_PROCEDURAL: "press_release",
    SourceType.LEGAL_COURT_ORDER: "court_order",
    SourceType.OFFICIAL_GOVERNMENT: "official_govt",
    SourceType.MEDIA_NEWS: "media_news",
}

MEDIA_NEWS_TOTAL_CAP = 3000
MEDIA_NEWS_PER_ARTICLE_CAP = 800
TIMELINE_CHUNK_SIZE = 10000
TIMELINE_CHUNK_OVERLAP = 1000
TIMELINE_MAX_ENTRIES = 30
TIMELINE_DISTINCT_EVENT_TERMS = (
    "आरोप",
    "दायर",
    "उजुरी",
    "दर्ता",
    "थुनछेक",
    "आदेश",
    "फैसला",
    "निर्णय",
    "बोलपत्र",
    "सम्झौता",
    "लागत",
)
TIMELINE_ROUTINE_HEARING_TERMS = ("सुनुवाइ", "सुनुवाई", "पेशी")

PUBLISHED_STYLE_EXAMPLES = """\
- 2022-07-19 — उजुरी दर्ता — अक्सिजन प्लान्ट खरिद अनियमितता
  Description: उ.द.नं. C-०००८०५ अन्तर्गत नगरपालिकाले सेटिङमा रु. १३,००,०००।– बढी हालेर अक्सिजन प्लान्ट खरिद गरेको भन्ने उजुरी CIAA मा दर्ता भएको। उजुरीमा खरिद प्रक्रियामा मिलेमतो गरी सार्वजनिक रकम हिनामिना गरिएको आरोप उल्लेख छ।
- 2014-01-26 — उजुरी दर्ता — अख्तियारमा पहिलो उजुरी
  Description: मिति २०७०/१०/१२ मा काठमाडौं महानगरपालिकामा कार्यरत इन्जिनियर रामबाबु महतो तथा निजको परिवारको नाममा रहेको सम्पत्ति वैधानिक आयस्रोतसँग मेल नखाने भनी अख्तियारमा उजुरी परेको। उजुरीमा अस्वाभाविक जीवनशैली र स्रोत नखुलेको सम्पत्ति आर्जनबारे छानबिन माग गरिएको थियो।
- 2025-06-09 — विशेष अदालतमा मुद्दा दर्ता
  Description: अख्तियारले अनुसन्धानबाट भ्रष्टाचारजन्य कसुर देखिएको निष्कर्षसहित विशेष अदालत, काठमाडौंमा आरोपपत्र दायर गरेको। आरोपपत्रमा प्रतिवादी, बिगो रकम, सम्बद्ध कार्यालय र मागदाबी स्रोतमा उल्लेख भए अनुसार समेटिएको थियो।"""

PUBLISHED_STYLE_PROMPT_EXAMPLES = """\
Style examples:
- {"date": "2022-07-19", "title": "उजुरी दर्ता — अक्सिजन प्लान्ट खरिद अनियमितता", "description": "उ.द.नं. C-०००८०५ अन्तर्गत नगरपालिकाले सेटिङमा रु. १३,००,०००।– बढी हालेर अक्सिजन प्लान्ट खरिद गरेको भन्ने उजुरी CIAA मा दर्ता भएको। उजुरीमा खरिद प्रक्रियामा मिलेमतो गरी सार्वजनिक रकम हिनामिना गरिएको आरोप उल्लेख छ।"}}
- {"date": "2014-01-26", "title": "उजुरी दर्ता — अख्तियारमा पहिलो उजुरी", "description": "मिति २०७०/१०/१२ मा काठमाडौं महानगरपालिकामा कार्यरत इन्जिनियर रामबाबु महतो तथा निजको परिवारको नाममा रहेको सम्पत्ति वैधानिक आयस्रोतसँग मेल नखाने भनी अख्तियारमा उजुरी परेको। उजुरीमा अस्वाभाविक जीवनशैली र स्रोत नखुलेको सम्पत्ति आर्जनबारे छानबिन माग गरिएको थियो।"}}
- {"date": "2025-06-09", "title": "विशेष अदालतमा मुद्दा दर्ता", "description": "अख्तियारले अनुसन्धानबाट भ्रष्टाचारजन्य कसुर देखिएको निष्कर्षसहित विशेष अदालत, काठमाडौंमा आरोपपत्र दायर गरेको। आरोपपत्रमा प्रतिवादी, बिगो रकम, सम्बद्ध कार्यालय र मागदाबी स्रोतमा उल्लेख भए अनुसार समेटिएको थियो।"}}"""


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the last sentence boundary within max_chars."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    # Try Nepali full stop first, then double newline, then English period-space
    boundary = max(
        truncated.rfind("। "),
        truncated.rfind("।"),
        truncated.rfind("\n\n"),
        truncated.rfind(". "),
    )
    if boundary > 0:
        return text[: boundary + 1].strip()
    return text[:max_chars]


EXTRACTION_SYSTEM_PROMPT = """\
You are a Nepali legal analyst extracting structured timeline entries from \
CIAA (Commission for the Investigation of Abuse of Authority) press releases, \
court orders, charge sheets, and NGM court hearing records.

Your task is to reconstruct the chronological progression of a corruption case \
from available source documents.

TIMELINE ENTRY FORMAT:
Each entry must be a JSON object with:
- "date": ISO date string (YYYY-MM-DD) in AD (Gregorian calendar)
- "title": Brief label in Nepali (one line, 5-15 words) describing the event
- "description": Required Nepali explanation when source material provides detail

All three fields must be written in Nepali (देवनागरी लिपि).

KEY EVENTS TO EXTRACT (when available in sources):
1. CIAA investigation initiation / filing decision date
2. Case filed to Special Court date
3. Court hearing dates
4. Verdict / judgment date
5. Case registration at CIAA (if different from investigation start)
6. Any other significant dates mentioned in the source

DATE CONVERSION RULES (CRITICAL):
- Document text sources use Bikram Sambat (BS) dates — convert to AD
- NGM structured data contains reliable AD dates — use EXACTLY as-is
- BS to AD offset: subtract 56 years and 8 months 17 days as baseline
- ALWAYS output in YYYY-MM-DD format

NGM DATE PRIORITY (CRITICAL):
- NGM structured hearing data (if provided) contains ground-truth AD dates
- For any event that exists in BOTH NGM data and document text,
  use the NGM date exactly — do not convert or adjust it
- Use document text to add narrative context (title, description) to
  NGM-dated events, and to extract any additional events not in NGM
- NGM dates are already in AD format — treat them as authoritative

VERDICT DATE PRECISION (CRITICAL):
- Be very precise with BS→AD conversion for verdict dates
- If you see the same verdict mentioned multiple times in different document
  sections, emit it only ONCE — do not create separate entries with slightly
  different dates for the same verdict
- The same BS date must always produce the same AD date; do not vary the
  conversion per chunk

DESCRIPTION QUALITY RULES:
- Description is REQUIRED when the source includes facts beyond the date/title
- Target 2-4 concise Nepali sentences per description when detail exists
- Preserve material facts: amounts (रु.), complaint/file/case numbers, names of
  people/offices/companies, alleged acts, court decisions, verdicts, penalties,
  and final outcomes
- Do not output shallow descriptions like "अदालतमा सुनुवाइ भयो" when richer
  source context exists
- Do NOT emit a timeline entry for a hearing date unless you can write a non-empty
  description based on the document text. A bare "hearing took place" entry with no
  context is worse than no entry. If NGM remarks only say 'बृद्ध' or equivalent
  (adjourned), skip that hearing entirely

PUBLISHED STYLE EXAMPLES:
{PUBLISHED_STYLE_EXAMPLES}

QUALITY RULES:
- Minimum 3 timeline entries when sufficient source material exists
- Entries must be in chronological order (earliest first)
- Each entry must be factually grounded in the provided sources
- Do NOT fabricate dates or events not mentioned in the sources
- If the source text is insufficient, return fewer entries or an empty array
"""

EXTRACTION_USER_PROMPT = """\
Extract chronological timeline entries from the provided CIAA case source \
documents and NGM structured hearing data.

Case title: {case_title}

Instructions:
- Each entry must have "date" (YYYY-MM-DD in AD), "title", and "description"
- Write all title/description text in Nepali देवनागरी
- Description is required whenever source text or NGM remarks provide detail
- Target 2-4 concise sentences for descriptions with substantive source detail
- Include exact material facts when present: रु. amounts, उजुरी/file/case numbers,
  names of parties/offices/companies, alleged acts, decisions, verdicts, penalties,
  and outcomes
- For NGM dates: use them exactly as-is — they are authoritative ground-truth
- For document-text dates: convert from BS to AD before outputting
- Use document text to enrich NGM-dated hearing entries with narrative context
- Order entries chronologically from earliest to latest
- Only include events explicitly mentioned or clearly inferred from the sources
- If sources are insufficient, return fewer entries

{PUBLISHED_STYLE_PROMPT_EXAMPLES}

IMPORTANT: Return ONLY a valid JSON array of timeline entry objects.
Format: [{{"date": "YYYY-MM-DD", "title": "नेपाली शीर्षक", "description": "विवरण"}}]
No explanations, no markdown, no text outside the JSON array.

{ngm_section}

DOCUMENT TEXT (use for context, narrative, and any dates not in NGM):

{source_text}
"""


class Command(BaseCommand):
    help = (
        "Extract case timeline entries from CIAA source documents via LLM. "
        "Populates timeline for CIAA Special Court draft cases."
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
            help="Process a specific case by case_id",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of cases to process",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-generate timeline even if timeline already exists",
        )
        parser.add_argument(
            "--fiscal-year",
            type=str,
            help="Filter by fiscal year (e.g., '080' or '081')",
        )
        parser.add_argument(
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default="claude-sonnet-4-20250514",
            help="LLM model identifier (default: claude-sonnet-4-20250514)",
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=os.environ.get(
                "JAWAFDEHI_LLM_PROXY_URL", "https://llm-proxy.jawafdehi.org/v1"
            ),
            help="LLM API base URL (OpenAI-compatible endpoint)",
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            default=None,
            help="LLM API key (defaults to JAWAFDEHI_LLM_API_KEY or ANTHROPIC_API_KEY env var)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose debug logging",
        )

    def __init__(self):
        super().__init__()
        self.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_content": 0,
            "cases_llm_error": 0,
            "cases_already_populated": 0,
            "cases_ngm_used": 0,
        }
        self._http_session: Optional[requests.Session] = None

    def _get_session(self) -> requests.Session:
        if self._http_session is None:
            self._http_session = requests.Session()
        return self._http_session

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        case_id = options.get("case_id")
        limit = options.get("limit")

        if limit is not None:
            try:
                limit_int = int(limit)
            except (ValueError, TypeError):
                raise CommandError(
                    f"Invalid --limit value: {limit}. Must be a positive integer."
                )
            if limit_int <= 0:
                raise CommandError(
                    f"Invalid --limit: {limit_int}. Must be a positive integer."
                )
            limit = limit_int
        llm_model = options["llm_model"]
        llm_base_url = options["llm_base_url"]
        llm_api_key = options.get("llm_api_key")
        force = options.get("force")
        fiscal_year = options.get("fiscal_year")
        priority = options.get("priority")
        verbose = options.get("verbose")

        if priority and case_id:
            raise CommandError("--priority and --case-id are mutually exclusive")

        if verbose:
            logger.setLevel(logging.DEBUG)

        if not logger.handlers:
            handler = logging.StreamHandler(self.stdout)
            handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            logger.addHandler(handler)
            logger.propagate = False

        if dry_run:
            self.stdout.write(self.style.WARNING("[DRY RUN] No changes will be saved."))

        api_key = resolve_api_key(llm_api_key)
        if not dry_run and not api_key:
            raise CommandError(
                "No LLM API key provided. Set JAWAFDEHI_LLM_API_KEY or "
                "ANTHROPIC_API_KEY environment variable, or use --llm-api-key."
            )

        if fiscal_year and not re.match(r"^\d{2,3}$", fiscal_year):
            raise CommandError(
                f"Invalid fiscal year: {fiscal_year}. "
                "Use 2- or 3-digit format, e.g., '80' or '080'."
            )

        cases = self._get_ciaa_cases(
            case_id=case_id,
            limit=limit,
            force=force,
            fiscal_year=fiscal_year,
            priority=priority,
        )
        total = len(cases)

        self.stdout.write(
            f"Found {total} CIAA draft cases to process. Model: {llm_model}"
        )
        if force:
            self.stdout.write(
                self.style.WARNING("  --force: re-generating even for populated cases")
            )
        if fiscal_year:
            self.stdout.write(f"  Fiscal year filter: {fiscal_year}")
        if priority:
            priority_list = load_priority_cases()
            self.stdout.write(f"  Priority mode: {len(priority_list)} cases")

        session = self._get_session()
        for idx, case in enumerate(cases, 1):
            self._process_case(
                case=case,
                idx=idx,
                total=total,
                dry_run=dry_run,
                llm_model=llm_model,
                llm_base_url=llm_base_url,
                llm_api_key=api_key,
                session=session,
                force=force,
            )

        self._print_summary(dry_run)

    # ── helpers ──────────────────────────────────────────────────────────

    def _get_ciaa_cases(
        self,
        case_id: Optional[str] = None,
        limit: Optional[int] = None,
        force: bool = False,
        fiscal_year: Optional[str] = None,
        priority: bool = False,
    ) -> list[Case]:
        """Return DRAFT cases with empty timeline that are candidates for enrichment."""
        queryset = Case.objects.filter(state="DRAFT")
        if case_id:
            queryset = queryset.filter(case_id=case_id)

        if priority:
            priority_list = load_priority_cases()
            queryset = filter_by_priority(queryset, priority_list)

        all_cases = []
        candidate_count = 0
        for case in queryset.order_by("case_id"):
            if not self._is_ciaa_special_court_case(case):
                continue
            if fiscal_year and not self._matches_fiscal_year(case, fiscal_year):
                continue
            candidate_count += 1
            if not force and case.timeline:
                continue
            all_cases.append(case)
            if limit and len(all_cases) >= limit:
                break

        if not force:
            self.stats["cases_already_populated"] = sum(
                1
                for c in queryset
                if self._is_ciaa_special_court_case(c)
                and (not fiscal_year or self._matches_fiscal_year(c, fiscal_year))
                and c.timeline
            )
        return all_cases

    @staticmethod
    def _is_ciaa_special_court_case(case: Case) -> bool:
        """Return True if the case references Special Court in court_cases."""
        if case.court_cases and isinstance(case.court_cases, list):
            return any(
                isinstance(ref, str) and ref.startswith("special:")
                for ref in case.court_cases
            )
        return False

    def _matches_fiscal_year(self, case: Case, fiscal_year: str) -> bool:
        """Check if a case's court_cases reference matches the given fiscal year."""
        fy_normalized = fiscal_year.lstrip("0") or "0"
        if case.court_cases and isinstance(case.court_cases, list):
            for entry in case.court_cases:
                if isinstance(entry, str):
                    parts = entry.split(":")
                    case_number = parts[-1] if ":" in entry else entry
                    if "-CR-" in case_number:
                        prefix = case_number.split("-CR-")[0].lstrip("0") or "0"
                        if prefix == fy_normalized:
                            return True
        return False

    # ── core pipeline ────────────────────────────────────────────────────

    def _process_case(
        self,
        case: Case,
        idx: int,
        total: int,
        dry_run: bool,
        llm_model: str,
        llm_base_url: str,
        llm_api_key: Optional[str],
        session: requests.Session,
        force: bool = False,
    ):
        self.stats["cases_processed"] += 1
        case_number = extract_court_case_number(case)
        self.stdout.write(
            f"\n[{idx}/{total}] Processing {case.case_id}"
            f" ({case_number}) — {case.title[:80]}"
        )

        source_text = self._get_source_content(case, session)
        ngm_data = self._get_ngm_data(case)

        if not source_text and not ngm_data:
            self.stats["cases_no_content"] += 1
            self.stdout.write(
                self.style.WARNING("  No source content found — skipping")
            )
            return

        if source_text:
            self.stdout.write(f"  Source content: {len(source_text)} chars")

        if ngm_data:
            self.stats["cases_ngm_used"] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"  NGM data: {len(ngm_data.get('hearings', []))} hearing(s)"
                )
            )
        else:
            self.stdout.write("  NGM data: none")

        if dry_run and not llm_api_key:
            self.stdout.write(
                self.style.WARNING("  [DRY RUN] No API key — skipping LLM extraction")
            )
            return

        try:
            timeline_entries = self._extract_timeline(
                source_text=source_text,
                case_title=case.title,
                llm_model=llm_model,
                llm_base_url=llm_base_url,
                llm_api_key=llm_api_key,
                session=session,
                ngm_data=ngm_data,
            )
        except (
            requests.RequestException,
            CommandError,
            ValueError,
        ) as exc:
            self.stats["cases_llm_error"] += 1
            self.stdout.write(self.style.ERROR(f"  LLM extraction failed: {exc}"))
            return

        if not timeline_entries:
            self.stats["cases_skipped"] += 1
            self.stdout.write(
                self.style.WARNING("  LLM returned no timeline entries — skipping")
            )
            return

        entry_count = len(timeline_entries)
        self.stdout.write(self.style.SUCCESS(f"  Extracted {entry_count} entry(s)"))
        for i, entry in enumerate(timeline_entries, 1):
            desc = entry.get("description", "")
            desc_preview = (
                (desc[:100] + "…")
                if len(desc) > 100
                else (desc if desc else "⚠ no desc")
            )
            self.stdout.write(
                f"    {i}. {entry.get('date', '?')} — "
                f"{entry.get('title', '?')[:60]}"
            )
            self.stdout.write(f"       {desc_preview}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("  [DRY RUN] Would save but --dry-run is set")
            )
        else:
            try:
                self._save_timeline(case, timeline_entries, force=force)
                self.stats["cases_enriched"] += 1
            except CommandError as exc:
                self.stats["cases_llm_error"] += 1
                self.stdout.write(self.style.ERROR(f"  Failed to save timeline: {exc}"))

    # ── source acquisition with tiered fallback ──────────────────────────

    def _get_source_content(
        self, case: Case, session: requests.Session
    ) -> Optional[str]:
        """Acquire source document text for timeline extraction.

        Priority order:
        1. LEGAL_PROCEDURAL description (already extracted) — use if len > 200
        2. LEGAL_PROCEDURAL URLs — download + likhit/markitdown convert
        3. LEGAL_COURT_ORDER URLs — supplement with court order data
        4. OFFICIAL_GOVERNMENT description/URLs — use if available
        5. MEDIA_NEWS articles — supplement with news coverage (capped at ~3k total)
        """
        if not case.evidence:
            logger.debug("  No evidence entries on case")
            return None

        source_ids = [
            entry["source_id"]
            for entry in case.evidence
            if isinstance(entry, dict) and entry.get("source_id")
        ]
        if not source_ids:
            logger.debug("  No source_ids in evidence")
            return None

        sources = list(
            DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            ).only("source_id", "description", "title", "url", "source_type")
        )
        if not sources:
            logger.debug("  No DocumentSource records found")
            return None

        source_by_id = {s.source_id: s for s in sources}

        content_parts = []

        self._append_source_content(
            source_ids,
            source_by_id,
            SourceType.LEGAL_PROCEDURAL,
            content_parts,
            session,
        )
        self._append_source_content(
            source_ids,
            source_by_id,
            SourceType.LEGAL_COURT_ORDER,
            content_parts,
            session,
        )
        self._append_source_content(
            source_ids,
            source_by_id,
            SourceType.OFFICIAL_GOVERNMENT,
            content_parts,
            session,
        )
        self._append_media_news_content(
            source_ids,
            source_by_id,
            content_parts,
            session,
        )

        if not content_parts:
            logger.debug("  No usable content from any source type")
            return None

        return "\n\n---\n\n".join(content_parts)

    def _append_source_content(
        self,
        source_ids: list[str],
        source_by_id: dict,
        source_type: str,
        content_parts: list[str],
        session: requests.Session,
    ):
        """Try to get content from sources of a specific type and append to parts."""
        label = _SOURCE_TYPE_LABELS.get(source_type, source_type)
        for sid in source_ids:
            source = source_by_id.get(sid)
            if source is None:
                continue
            if source.source_type != source_type:
                continue

            description = (source.description or "").strip()
            if len(description) > 200:
                content_parts.append(description)
                logger.debug(
                    "  %s=source:%s  chars=%d  used=%d (from description)",
                    label,
                    source.source_id,
                    len(description),
                    len(description),
                )
                continue

            ranked_urls = rank_source_urls(source)
            if not ranked_urls:
                logger.debug(
                    "  %s=source:%s — skipped (no URLs)",
                    label,
                    source.source_id,
                )
                continue

            skipped_reason = None
            for url in ranked_urls:
                parsed = urlparse(url)
                if parsed.hostname and parsed.hostname in ALLOWED_HOSTS:
                    content = convert_to_markdown(url, session)
                    if content and len(content) > 200:
                        content_parts.append(content)
                        logger.debug(
                            "  %s=source:%s  chars=%d  used=%d",
                            label,
                            source.source_id,
                            len(content),
                            len(content),
                        )
                        break
                    else:
                        skipped_reason = "fetch failed" if not content else "too short"
                else:
                    skipped_reason = "disallowed host"
            else:
                logger.debug(
                    "  %s=source:%s — skipped (%s)",
                    label,
                    source.source_id,
                    skipped_reason or "no URLs succeeded",
                )

    def _append_media_news_content(
        self,
        source_ids: list[str],
        source_by_id: dict,
        content_parts: list[str],
        session: requests.Session,
    ):
        """Fetch news article URLs, cap per-article at ~500-800 chars and total at 3000."""
        total_used = 0
        news_parts = []
        for sid in source_ids:
            source = source_by_id.get(sid)
            if source is None:
                continue
            if source.source_type != SourceType.MEDIA_NEWS:
                continue

            if MEDIA_NEWS_TOTAL_CAP - total_used <= 200:
                break

            portion = self._get_media_news_description(source, total_used)
            if portion is not None:
                news_parts.append(portion)
                logger.debug(
                    "  media_news=source:%s  chars=%d  used=%d (from description)",
                    source.source_id,
                    len((source.description or "").strip()),
                    len(portion),
                )
                total_used += len(portion)
                continue

            portion = self._get_media_news_from_url(source, session, total_used)
            if portion is not None:
                news_parts.append(portion)
                logger.debug(
                    "  media_news=source:%s  chars=%d  used=%d",
                    source.source_id,
                    len(portion),
                    len(portion),
                )
                total_used += len(portion)

        if news_parts:
            content_parts.extend(news_parts)

    def _get_media_news_description(
        self, source: DocumentSource, total_used: int
    ) -> Optional[str]:
        """Try to use source description as MEDIA_NEWS content. Returns None if not viable."""
        description = (source.description or "").strip()
        if len(description) <= 200:
            return None
        portion = _truncate_at_sentence(
            description,
            min(MEDIA_NEWS_PER_ARTICLE_CAP, MEDIA_NEWS_TOTAL_CAP - total_used),
        )
        if len(portion) <= 200:
            return None
        return portion

    def _get_media_news_from_url(
        self, source: DocumentSource, session: requests.Session, total_used: int
    ) -> Optional[str]:
        """Fetch and truncate MEDIA_NEWS content from URLs. Returns None if all URLs fail."""
        urls = source.url
        if not isinstance(urls, list):
            logger.debug(
                "  media_news=source:%s — skipped (url field is not a list)",
                source.source_id,
            )
            return None
        if not urls:
            logger.debug(
                "  media_news=source:%s — skipped (no URLs)",
                source.source_id,
            )
            return None
        for url in urls:
            parsed = urlparse(url)
            if not parsed.hostname or parsed.hostname not in ALLOWED_HOSTS:
                continue
            content = convert_to_markdown(url, session)
            if not content or len(content) <= 200:
                continue
            portion = _truncate_at_sentence(
                content,
                min(MEDIA_NEWS_PER_ARTICLE_CAP, MEDIA_NEWS_TOTAL_CAP - total_used),
            )
            if len(portion) > 200:
                return portion
        logger.debug(
            "  media_news=source:%s — skipped (no usable content from URLs)",
            source.source_id,
        )
        return None

    # ── NGM structured hearing data ──────────────────────────────────────

    def _get_ngm_data(self, case: Case) -> Optional[dict]:
        """Query NGM database for structured hearing records.

        Extracts the special court case number from case.court_cases
        and fetches ground-truth dates, hearing records, and verdict info.
        Returns None if no special court reference or NGM query fails.
        """
        if not case.court_cases:
            return None

        special_ref = next(
            (
                ref.split(":", 1)[1]
                for ref in case.court_cases
                if isinstance(ref, str) and ref.startswith("special:")
            ),
            None,
        )
        if not special_ref:
            return None

        try:
            ngm_data = get_court_case_details("special", special_ref)
            if ngm_data is None:
                logger.debug("  NGM: no case found for %s", special_ref)
                return None
            return ngm_data
        except ValueError as exc:
            logger.warning("  NGM query failed for %s: %s", special_ref, exc)
            return None

    def _format_ngm_section(self, ngm_data: Optional[dict]) -> str:
        """Format NGM hearing data as a structured section for the LLM prompt.

        Returns an empty string if no NGM data available.
        """
        if not ngm_data:
            return ""

        lines = [
            "NGM STRUCTURED HEARING DATA (ground-truth dates — use these dates EXACTLY as-is):",
            "",
        ]

        case_data = ngm_data.get("case") or {}
        reg_date = case_data.get("registration_date_ad")
        verdict_date = case_data.get("verdict_date_ad")
        case_status = case_data.get("case_status", "")

        if reg_date:
            lines.append(f"- Case registration: {reg_date}")
        if case_status:
            lines.append(f"- Case status: {case_status}")

        hearings = ngm_data.get("hearings") or []
        if hearings:
            lines.append(f"- Hearings ({len(hearings)} records):")
            for h in hearings:
                h_date = h.get("hearing_date_ad", "")
                h_decision = h.get("decision_type") or ""
                h_remarks = (h.get("remarks") or "")[:200]
                line = f"  * {h_date}"
                if h_decision:
                    line += f" — {h_decision}"
                if h_remarks:
                    line += f" — {h_remarks}"
                lines.append(line)

        if verdict_date:
            lines.append(f"- Verdict date: {verdict_date}")
            verdict_judge = case_data.get("verdict_judge")
            if verdict_judge:
                lines.append(f"  Judge: {verdict_judge}")

        return "\n".join(lines) + "\n"

    # ── LLM extraction ───────────────────────────────────────────────────

    def _extract_timeline(
        self,
        source_text: str,
        case_title: str,
        llm_model: str,
        llm_base_url: str,
        llm_api_key: Optional[str],
        session: requests.Session,
        ngm_data: Optional[dict] = None,
    ) -> Optional[list[dict]]:
        """Call LLM to extract timeline entries from source text and NGM data."""
        chunks = self._chunk_source_text(source_text)
        if not chunks:
            if not ngm_data:
                return None
            chunks = [""]
        all_entries = []

        for idx, chunk in enumerate(chunks, 1):
            if idx > 1:
                time.sleep(0.5)
            response_text = self._extract_timeline_chunk(
                source_text=chunk,
                case_title=case_title,
                llm_model=llm_model,
                llm_base_url=llm_base_url,
                llm_api_key=llm_api_key,
                session=session,
                ngm_data=ngm_data,
            )
            entries = self._parse_timeline_response(response_text) or []
            all_entries.extend(entries)
            logger.debug(
                "  chunk %d/%d: %d entries extracted",
                idx,
                len(chunks),
                len(entries),
            )
            for entry in entries:
                description = entry.get("description", "")
                logger.debug(
                    "    → %s — %s | %s",
                    entry.get("date", "?"),
                    entry.get("title", "?"),
                    (
                        (description[:120] + "…")
                        if len(description) > 120
                        else (description if description else "⚠ no desc")
                    ),
                )

        unique_entries = self._deduplicate_timeline_entries(all_entries)
        logger.info(
            "  source_text: %d chunks processed, %d entries extracted, %d unique after dedup",
            len(chunks),
            len(all_entries),
            len(unique_entries),
        )
        return unique_entries or None

    def _extract_timeline_chunk(
        self,
        source_text: str,
        case_title: str,
        llm_model: str,
        llm_base_url: str,
        llm_api_key: Optional[str],
        session: requests.Session,
        ngm_data: Optional[dict] = None,
    ) -> str:
        ngm_section = self._format_ngm_section(ngm_data)
        prompt = EXTRACTION_USER_PROMPT.format(
            case_title=case_title,
            ngm_section=ngm_section,
            source_text=source_text,
            PUBLISHED_STYLE_PROMPT_EXAMPLES=PUBLISHED_STYLE_PROMPT_EXAMPLES,
        )

        return call_llm(
            system_prompt=EXTRACTION_SYSTEM_PROMPT.replace(
                "{PUBLISHED_STYLE_EXAMPLES}", PUBLISHED_STYLE_EXAMPLES
            ),
            user_prompt=prompt,
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            session=session,
        )

    def _chunk_source_text(self, source_text: str) -> list[str]:
        text = (source_text or "").strip()
        if not text:
            return []
        if len(text) <= TIMELINE_CHUNK_SIZE:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = min(start + TIMELINE_CHUNK_SIZE, len(text))
            chunks.append(text[start:end])
            if end >= len(text):
                break
            start = end - TIMELINE_CHUNK_OVERLAP
        return chunks

    def _deduplicate_timeline_entries(self, entries: list[dict]) -> list[dict]:
        best_by_key = {}
        for entry in entries:
            key = (
                entry.get("date"),
                self._normalized_title_key(entry.get("title", "")),
            )
            current = best_by_key.get(key)
            if current is None or self._entry_score(entry) > self._entry_score(current):
                best_by_key[key] = entry

        deduped = list(best_by_key.values())
        collapsed = self._collapse_same_date_entries(deduped)
        self._warn_verdict_date_cluster(collapsed)
        capped = self._cap_timeline_entries(collapsed)
        capped.sort(key=lambda entry: entry["date"])
        return capped

    def _normalized_title_key(self, title: str) -> str:
        normalized = unicodedata.normalize("NFC", title or "")
        normalized = re.sub(r"\s+", "", normalized)
        return normalized.replace("सुनुवाई", "सुनुवाइ").replace("ब्यवसायी", "व्यवसायी")

    def _entry_score(self, entry: dict) -> tuple[int, int, int]:
        text = f"{entry.get('title', '')} {entry.get('description', '')}"
        milestone_score = sum(
            1 for term in TIMELINE_DISTINCT_EVENT_TERMS if term in text
        )
        return (
            milestone_score,
            len(entry.get("description", "")),
            len(entry.get("title", "")),
        )

    def _collapse_same_date_entries(self, entries: list[dict]) -> list[dict]:
        by_date = {}
        for entry in entries:
            by_date.setdefault(entry["date"], []).append(entry)

        collapsed = []
        for date, date_entries in by_date.items():
            if len(date_entries) == 1:
                collapsed.extend(date_entries)
                continue

            date_entries.sort(key=self._entry_score, reverse=True)
            kept = [date_entries[0]]
            for candidate in date_entries[1:]:
                if len(kept) >= 2:
                    break
                if self._is_clearly_distinct_event(candidate, kept[0]):
                    kept.append(candidate)

            collapsed.extend(kept)
            logger.debug(
                "  timeline date %s: collapsed %d entries to %d",
                date,
                len(date_entries),
                len(kept),
            )
        return collapsed

    def _is_clearly_distinct_event(self, candidate: dict, kept: dict) -> bool:
        candidate_terms = self._distinct_event_terms(candidate)
        kept_terms = self._distinct_event_terms(kept)
        return bool(
            candidate_terms and kept_terms and candidate_terms.isdisjoint(kept_terms)
        )

    def _warn_verdict_date_cluster(self, entries: list[dict]) -> None:
        """Log warning when 3+ entries within 180 days share verdict-like terms.

        Does not auto-collapse — some may genuinely be different events
        (verdict, appeal, Supreme Court). Flags for manual review.
        """
        if len(entries) < 3:
            return
        verdict_terms = {"फैसला", "निर्णय", "ठहर"}
        verdict_entries = [
            e
            for e in entries
            if verdict_terms & set(e.get("title", "") + e.get("description", ""))
        ]
        if len(verdict_entries) < 3:
            return
        from datetime import datetime, timedelta

        try:
            dated = [
                (datetime.strptime(e["date"], "%Y-%m-%d"), e) for e in verdict_entries
            ]
        except (ValueError, KeyError):
            return
        dated.sort(key=lambda p: p[0])
        window = timedelta(days=180)
        for i in range(len(dated) - 2):
            if dated[i + 2][0] - dated[i][0] <= window:
                logger.warning(
                    "  ⚠ %d verdict-like entries within %d-day window (%s → %s) — "
                    "may indicate BS→AD conversion inconsistency or genuine multi-stage "
                    "verdict process; manual review recommended",
                    len(verdict_entries),
                    window.days,
                    dated[i][0].strftime("%Y-%m-%d"),
                    dated[-1][0].strftime("%Y-%m-%d"),
                )
                return

    def _distinct_event_terms(self, entry: dict) -> set[str]:
        text = f"{entry.get('title', '')} {entry.get('description', '')}"
        return {term for term in TIMELINE_DISTINCT_EVENT_TERMS if term in text}

    def _cap_timeline_entries(self, entries: list[dict]) -> list[dict]:
        if len(entries) <= TIMELINE_MAX_ENTRIES:
            return entries

        substantive = [
            entry for entry in entries if not self._is_redundant_hearing(entry)
        ]
        if len(substantive) >= 3:
            entries = substantive
        if len(entries) <= TIMELINE_MAX_ENTRIES:
            return entries

        entries = sorted(entries, key=self._entry_score, reverse=True)[
            :TIMELINE_MAX_ENTRIES
        ]
        logger.debug("  timeline capped to %d entries", len(entries))
        return entries

    def _is_redundant_hearing(self, entry: dict) -> bool:
        title = entry.get("title", "")
        description = entry.get("description", "")
        if not any(term in title for term in TIMELINE_ROUTINE_HEARING_TERMS):
            return False
        # Drop bare hearings with no description — useless entries
        if not description or not description.strip():
            return True
        if any(
            term in f"{title} {description}" for term in TIMELINE_DISTINCT_EVENT_TERMS
        ):
            return False
        return len(description) < 80

    def _parse_timeline_response(self, response_text: str) -> Optional[list[dict]]:
        """Parse the LLM response to extract timeline entries with field mapping."""
        entries = parse_extraction_response(
            response_text, wrapper_keys={"timeline", "entries"}
        )
        if entries is None:
            return None

        clean = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            date_val = (
                item.get("date") or item.get("date_bs") or item.get("date_ad") or ""
            )
            title_val = item.get("title") or item.get("event") or item.get("name") or ""
            desc_val = (
                item.get("description") or item.get("desc") or item.get("detail") or ""
            )

            if not date_val or not title_val:
                continue

            entry = {
                "date": str(date_val).strip(),
                "title": str(title_val).strip(),
            }
            if desc_val and str(desc_val).strip():
                entry["description"] = str(desc_val).strip()

            if not is_valid_iso_date(entry["date"]):
                logger.warning(
                    "  Dropping non-ISO date format: %s",
                    entry["date"],
                )
                continue

            # Catch BS dates slipping through as syntactically valid ISO
            year = int(entry["date"].split("-")[0])
            if year > 2100:
                try:
                    bs_parts = entry["date"].split("-")
                    bs_date = _nepali_date(
                        int(bs_parts[0]), int(bs_parts[1]), int(bs_parts[2])
                    )
                    ad_date = bs_date.to_datetime_date()
                    entry["date_bs"] = entry["date"]
                    entry["date"] = ad_date.isoformat()
                    logger.info(
                        "  Converted BS date %s → AD %s",
                        entry["date_bs"],
                        entry["date"],
                    )
                except (ValueError, IndexError, OverflowError) as exc:
                    logger.warning(
                        "  Dropping BS date %s — conversion failed: %s",
                        entry["date"],
                        exc,
                    )
                    continue

            # Compute date_bs from AD date
            if "date_bs" not in entry:
                try:
                    ad_date = datetime.date.fromisoformat(entry["date"])
                    bs_obj = _nepali_date.from_date(ad_date)
                    entry["date_bs"] = bs_obj.strftime("%Y-%m-%d")
                except (ValueError, OverflowError) as exc:
                    logger.warning(
                        "  Dropping entry — date_bs conversion failed for %s: %s",
                        entry["date"],
                        exc,
                    )
                    continue

            clean.append(entry)

        if not clean:
            return None

        clean.sort(key=lambda entry: entry["date"])
        return clean

    # ── persistence ─────────────────────────────────────────────────────

    def _save_timeline(self, case: Case, entries: list[dict], force: bool = False):
        """Persist timeline entries to the database.

        Uses select_for_update to guard against concurrent writes.
        When force=False, skips cases whose timeline was populated
        by another process since the initial read.
        """
        with transaction.atomic():
            locked = (
                Case.objects.select_for_update().filter(pk=case.pk).only("timeline")
            )
            if not force:
                locked = locked.filter(timeline=[])
            updated = locked.update(
                timeline=entries,
                updated_at=timezone.now(),
            )
            if not updated:
                raise CommandError(
                    f"Case {case.case_id} was populated concurrently; skipping save."
                )
        logger.info("  Saved %d timeline entries to %s", len(entries), case.case_id)

    # ── summary ──────────────────────────────────────────────────────────

    def _print_summary(self, dry_run: bool):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.SUCCESS(
                f"{'[DRY RUN] ' if dry_run else ''}Timeline extraction complete."
            )
        )
        self.stdout.write(f"  Cases processed:        {self.stats['cases_processed']}")
        self.stdout.write(f"  Cases enriched:         {self.stats['cases_enriched']}")
        self.stdout.write(f"  Cases skipped:          {self.stats['cases_skipped']}")
        self.stdout.write(f"  No source content:      {self.stats['cases_no_content']}")
        self.stdout.write(f"  LLM errors:             {self.stats['cases_llm_error']}")
        self.stdout.write(f"  NGM data used:          {self.stats['cases_ngm_used']}")
        self.stdout.write(
            f"  Already populated:      {self.stats['cases_already_populated']}"
        )
