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

import json
import logging
import os
import re
from typing import Optional
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cases.models import Case, DocumentSource, SourceType

logger = logging.getLogger(__name__)

_ALLOWED_HOSTS = frozenset({"ciaa.gov.np", "ngm-store.jawafdehi.org"})

EXTRACTION_SYSTEM_PROMPT = """\
You are a Nepali legal analyst extracting structured timeline entries from \
CIAA (Commission for the Investigation of Abuse of Authority) press releases, \
court orders, and charge sheets.

Your task is to reconstruct the chronological progression of a corruption case \
from available source documents.

TIMELINE ENTRY FORMAT:
Each entry must be a JSON object with:
- "date": ISO date string (YYYY-MM-DD) in AD (Gregorian calendar)
- "title": Brief label in Nepali (one line, 5-15 words) describing the event
- "description": Optional 1-3 sentence explanation in Nepali

All three fields must be written in Nepali (देवनागरी लिपि).

KEY EVENTS TO EXTRACT (when available in sources):
1. CIAA investigation initiation / filing decision date ("अख्तियारले अनुसन्धान शुरु गरेको" or "मुद्दा दायर गर्ने निर्णय")
2. Case filed to Special Court date ("विशेष अदालतमा मुद्दा दायर")
3. Court hearing dates ("पेशी / सुनुवाइ मिति")
4. Verdict / judgment date ("फैसला मिति")
5. Case registration at CIAA (if different from investigation start)
6. Any other significant dates mentioned in the source

DATE CONVERSION RULES (CRITICAL):
- The source documents use Bikram Sambat (BS) dates
- You MUST convert all BS dates to AD (Gregorian) before outputting
- BS to AD offset: subtract 56 years and 8 months 17 days as baseline
- Example: 2080-04-15 BS ≈ 2023-08-02 AD
- Example: 2081-01-01 BS ≈ 2024-04-13 AD
- When only a BS year is mentioned, use mid-year: BS 2080 → 2023-08, BS 2081 → 2024-04
- ALWAYS output in YYYY-MM-DD format

QUALITY RULES:
- Minimum 3 timeline entries when sufficient source material exists
- Entries must be in chronological order (earliest first)
- Each entry must be factually grounded in the provided source text
- Do NOT fabricate dates or events not mentioned in the sources
- If the source text is insufficient, return fewer entries or an empty array
"""

EXTRACTION_USER_PROMPT = """\
Extract chronological timeline entries from the provided CIAA case source \
documents.

Case title: {case_title}

Instructions:
- Each entry must have "date" (YYYY-MM-DD in AD) and "title" in Nepali
- "description" is optional but encouraged when source provides details
- Convert all BS (Bikram Sambat) dates to AD (Gregorian) before outputting
- Order entries chronologically from earliest to latest
- Only include events explicitly mentioned or clearly inferred from the source
- If sources are insufficient, return fewer entries

IMPORTANT: Return ONLY a valid JSON array of timeline entry objects.
Format: [{{"date": "YYYY-MM-DD", "title": "नेपाली शीर्षक", "description": "विवरण"}}]
No explanations, no markdown, no text outside the JSON array.

Source documents:

{source_text}
"""

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_iso_date(date_str: str) -> bool:
    """Validate that a string is in YYYY-MM-DD ISO date format."""
    if not isinstance(date_str, str):
        return False
    return bool(ISO_DATE_RE.match(date_str.strip()))


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
        }

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        case_id = options.get("case_id")
        limit = options.get("limit")
        llm_model = options["llm_model"]
        llm_base_url = options["llm_base_url"]
        llm_api_key = options.get("llm_api_key")
        force = options.get("force")
        fiscal_year = options.get("fiscal_year")
        verbose = options.get("verbose")

        if verbose:
            logger.setLevel(logging.DEBUG)

        if not logger.handlers:
            handler = logging.StreamHandler(self.stdout)
            handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            logger.addHandler(handler)
            logger.propagate = False

        if dry_run:
            self.stdout.write(self.style.WARNING("[DRY RUN] No changes will be saved."))

        api_key = self._resolve_api_key(llm_api_key)
        if not dry_run and not api_key:
            raise CommandError(
                "No LLM API key provided. Set JAWAFDEHI_LLM_API_KEY or "
                "ANTHROPIC_API_KEY environment variable, or use --llm-api-key."
            )

        if fiscal_year:
            if not re.match(r"^\d{3}$", fiscal_year):
                raise CommandError(
                    f"Invalid fiscal year: {fiscal_year}. "
                    "Use 3-digit format, e.g., '080' or '081'."
                )

        cases = self._get_ciaa_cases(
            case_id=case_id, limit=limit, force=force, fiscal_year=fiscal_year
        )
        total = len(cases)

        self.stdout.write(
            f"Found {total} CIAA draft cases to process. " f"Model: {llm_model}"
        )
        if force:
            self.stdout.write(
                self.style.WARNING("  --force: re-generating even for populated cases")
            )
        if fiscal_year:
            self.stdout.write(f"  Fiscal year filter: {fiscal_year}")

        for idx, case in enumerate(cases, 1):
            self._process_case(
                case=case,
                idx=idx,
                total=total,
                dry_run=dry_run,
                llm_model=llm_model,
                llm_base_url=llm_base_url,
                llm_api_key=api_key,
            )

        self._print_summary(dry_run)

    # ── helpers ──────────────────────────────────────────────────────────

    def _resolve_api_key(self, cli_key: Optional[str]) -> Optional[str]:
        if cli_key:
            return cli_key
        return os.environ.get("JAWAFDEHI_LLM_API_KEY") or os.environ.get(
            "ANTHROPIC_API_KEY"
        )

    def _get_ciaa_cases(
        self,
        case_id: Optional[str] = None,
        limit: Optional[int] = None,
        force: bool = False,
        fiscal_year: Optional[str] = None,
    ) -> list[Case]:
        """Return DRAFT cases with empty timeline that are candidates for enrichment."""
        all_cases = []
        queryset = Case.objects.filter(state="DRAFT")

        if case_id:
            queryset = queryset.filter(case_id=case_id)

        for case in queryset.order_by("case_id"):
            if fiscal_year and not self._matches_fiscal_year(case, fiscal_year):
                continue
            if not force and case.timeline:
                self.stats["cases_already_populated"] += 1
                continue
            all_cases.append(case)
            if limit and len(all_cases) >= limit:
                break

        return all_cases

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
    ):
        self.stats["cases_processed"] += 1
        self.stdout.write(f"\n[{idx}/{total}] {case.case_id} — {case.title[:80]}")

        source_text = self._get_source_content(case)
        if not source_text:
            self.stats["cases_no_content"] += 1
            self.stdout.write(
                self.style.WARNING("  No source content found — skipping")
            )
            return

        self.stdout.write(f"  Source content: {len(source_text)} chars")

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
            )
        except (
            requests.RequestException,
            CommandError,
            ValueError,
            json.JSONDecodeError,
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
        invalid_count = sum(
            1 for e in timeline_entries if not _is_valid_iso_date(e.get("date", ""))
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"  Extracted {entry_count} entry(s)"
                + (f" ({invalid_count} invalid date(s))" if invalid_count else "")
            )
        )
        for i, entry in enumerate(timeline_entries, 1):
            date_flag = ""
            if not _is_valid_iso_date(entry.get("date", "")):
                date_flag = " [INVALID DATE]"
            self.stdout.write(
                f"    {i}. {entry.get('date', '?')} — "
                f"{entry.get('title', '?')[:80]}{date_flag}"
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("  [DRY RUN] Would save but --dry-run is set")
            )
        else:
            self._save_timeline(case, timeline_entries)
            self.stats["cases_enriched"] += 1

    # ── source acquisition with tiered fallback ──────────────────────────

    def _get_source_content(self, case: Case) -> Optional[str]:
        """Acquire source document text for timeline extraction.

        Priority order:
        1. LEGAL_PROCEDURAL description (already extracted) — use if len > 200
        2. LEGAL_PROCEDURAL URLs — download + likhit/markitdown convert
        3. LEGAL_COURT_ORDER URLs — supplement with court order data
        4. OFFICIAL_GOVERNMENT description/URLs — use if available
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

        # Tier 1: LEGAL_PROCEDURAL sources
        self._append_source_content(
            source_ids, source_by_id, SourceType.LEGAL_PROCEDURAL, content_parts
        )

        # Tier 2: LEGAL_COURT_ORDER sources
        self._append_source_content(
            source_ids, source_by_id, SourceType.LEGAL_COURT_ORDER, content_parts
        )

        # Tier 3: OFFICIAL_GOVERNMENT sources
        self._append_source_content(
            source_ids, source_by_id, SourceType.OFFICIAL_GOVERNMENT, content_parts
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
    ):
        """Try to get content from sources of a specific type and append to parts."""
        for sid in source_ids:
            source = source_by_id.get(sid)
            if source is None:
                continue
            if source.source_type != source_type:
                continue

            description = (source.description or "").strip()
            if len(description) > 200:
                content_parts.append(description)
                continue

            if isinstance(source.url, list):
                for url in source.url:
                    parsed = urlparse(url)
                    if parsed.hostname and parsed.hostname in _ALLOWED_HOSTS:
                        content = self._convert_to_markdown(url)
                        if content and len(content) > 200:
                            content_parts.append(content)
                            break

    # ── URL download + markdown conversion ───────────────────────────────

    def _convert_to_markdown(self, url: str) -> Optional[str]:
        """Download file from URL and convert to markdown using likhit.

        Pipeline: URL download -> temp file -> likhit/markitdown -> markdown.
        Returns None when conversion fails or produces insufficient content.
        """
        import tempfile
        from pathlib import Path

        try:
            response = requests.get(url, timeout=120, stream=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("  Failed to download %s: %s", url, exc)
            return None

        final_hostname = urlparse(response.url).hostname
        if final_hostname not in _ALLOWED_HOSTS:
            logger.warning("  Redirected to untrusted host: %s", response.url)
            return None

        content_type = response.headers.get("content-type", "").lower()

        if "text/plain" in content_type or "application/json" in content_type:
            response.encoding = "utf-8"
            text = response.text
            if len(text) > 200:
                return text
            return None

        suffix = ""
        if "pdf" in content_type:
            suffix = ".pdf"
        elif "html" in content_type:
            suffix = ".html"
        elif any(kw in content_type for kw in ("document", "word", "docx", "msword")):
            suffix = ".docx"

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
                for chunk in response.iter_content(chunk_size=8192):
                    tmp.write(chunk)

            import likhit  # noqa: F401
            from markitdown import MarkItDown

            md = MarkItDown(enable_plugins=True)
            result = md.convert(tmp_path)

            if (
                result
                and result.text_content
                and len(result.text_content.strip()) > 200
            ):
                return result.text_content.strip()

            logger.warning(
                "  Likhit conversion produced insufficient content for %s", url
            )
            return None
        except Exception as exc:
            logger.warning("  Likhit conversion failed for %s: %s", url, exc)
            return None
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    # ── LLM extraction ───────────────────────────────────────────────────

    def _extract_timeline(
        self,
        source_text: str,
        case_title: str,
        llm_model: str,
        llm_base_url: str,
        llm_api_key: Optional[str],
    ) -> Optional[list[dict]]:
        """Call LLM to extract timeline entries from source text."""
        prompt = EXTRACTION_USER_PROMPT.format(
            case_title=case_title,
            source_text=source_text[:40000],
        )

        response_text = self._call_llm(
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            user_prompt=prompt,
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
        )

        return self._parse_timeline_response(response_text)

    def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        base_url: str,
        api_key: Optional[str],
    ) -> str:
        """Call LLM API via OpenAI-compatible chat completions endpoint."""
        url = f"{base_url.rstrip('/')}/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 4000,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"LLM API request failed: {exc}") from exc

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise CommandError("LLM API returned no choices")

        return choices[0]["message"]["content"]

    def _parse_timeline_response(self, response_text: str) -> Optional[list[dict]]:
        """Parse the LLM response to extract the JSON array of timeline entries."""
        text = response_text.strip()

        json_start = text.find("[")
        json_end = text.rfind("]")

        if json_start == -1 or json_end == -1 or json_end <= json_start:
            logger.warning("  Could not find JSON array in LLM response")
            logger.debug("  Response: %s", text[:500])
            return None

        json_str = text[json_start : json_end + 1]

        try:
            entries = json.loads(json_str)
        except json.JSONDecodeError as exc:
            logger.warning("  Failed to parse JSON from LLM response: %s", exc)
            logger.debug("  JSON string: %s", json_str[:500])
            return None

        if isinstance(entries, dict) and isinstance(entries.get("timeline"), list):
            entries = entries["timeline"]
        if isinstance(entries, dict) and isinstance(entries.get("entries"), list):
            entries = entries["entries"]
        if not isinstance(entries, list):
            logger.warning("  LLM returned non-list: %s", type(entries).__name__)
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

            clean.append(entry)

        if not clean:
            return None

        # Warn but don't block on date format (LLM may still produce BS dates
        # despite prompt instructions; we capture the flag for human review)
        for entry in clean:
            if not _is_valid_iso_date(entry.get("date", "")):
                logger.warning(
                    "  Non-ISO date format: %s — may need manual review",
                    entry.get("date"),
                )

        return clean

    # ── persistence ─────────────────────────────────────────────────────

    def _save_timeline(self, case: Case, entries: list[dict]):
        """Persist timeline entries to the database."""
        with transaction.atomic():
            case.timeline = entries
            case.save(update_fields=["timeline", "updated_at"])
        logger.info("  Saved %d timeline entries to %s", len(entries), case.case_id)

    # ── summary ──────────────────────────────────────────────────────────

    def _print_summary(self, dry_run: bool):
        """Print final statistics table summarizing the enrichment run."""
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
        self.stdout.write(
            f"  Already populated:      {self.stats['cases_already_populated']}"
        )
