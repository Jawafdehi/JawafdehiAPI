"""
Management command to enrich CIAA DRAFT cases with key allegations extracted
via LLM from press release markdown content.

Usage::

    python manage.py enrich_ciaa_allegations --dry-run
    python manage.py enrich_ciaa_allegations --limit 10
    python manage.py enrich_ciaa_allegations --llm-model claude-sonnet-4-5 --verbose

Environment variables::

    ANTHROPIC_API_KEY  — API key for Anthropic (fallback for --api-key)
    LLM_PROXY_URL      — optional base URL for an OpenAI-compatible proxy
"""

import json
import logging
import os
import re
import sys
import tempfile
import time

import requests
from django.core.management.base import BaseCommand, CommandError

from cases.models import Case, CaseState, DocumentSource

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

NEPALI_ALLEGATION_PROMPT = """तपाईं एक नेपाली कानुनी सहायक हुनुहुन्छ। तलको CIAA (Commission for the Investigation of Abuse of Authority) प्रेस विज्ञप्तिको अध्ययन गरी मुख्य आरोपहरू निकाल्नुहोस्।

**निर्देशनहरू:**
1. प्रेस विज्ञप्तिको विषयवस्तुलाई ध्यानपूर्वक पढी मुख्य भ्रष्टाचार/अनियमितताका आरोपहरू पहिचान गर्नुहोस्।
2. २ देखि ५ वटासम्म मुख्य आरोपहरू लेख्नुहोस् — प्रत्येक एउटा वा दुई वाक्यमा।
3. आरोपहरू नेपाली भाषामा स्पष्ट, तथ्यपरक र विशिष्ट हुनुपर्छ।
4. बिगो रकम उल्लेख भएमा समावेश गर्नुहोस्।
5. काल्पनिक वा प्रेस विज्ञप्तिमा नभएका आरोपहरू नलेख्नुहोस्।

**केस शीर्षक:** {case_title}
**बिगो रकम:** {bigo}
**प्रेस विज्ञप्ति:** {press_release}

**प्रतिक्रिया ढाँचा (JSON मात्र):**
```json
{{
  "allegations": [
    "पहिलो मुख्य आरोप...",
    "दोस्रो मुख्य आरोप...",
    "तेस्रो मुख्य आरोप..."
  ]
}}
```

JSON बाहेक अन्य केही नलेख्नुहोस्।"""

SYSTEM_PROMPT = "You are a Nepali legal assistant that extracts key allegations from CIAA press releases. You respond ONLY with valid JSON."


class Command(BaseCommand):
    help = "Extract key allegations from CIAA press release content using LLM"

    def __init__(self):
        super().__init__()
        self.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_failed": 0,
            "cases_no_content": 0,
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
            help="Process only N cases (useful for testing)",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default=os.environ.get("JAWAFDEHI_ALLEGATION_MODEL", "claude-sonnet-4-5"),
            help="Anthropic model name (default: claude-sonnet-4-5)",
        )
        parser.add_argument(
            "--api-key",
            type=str,
            default=None,
            help="Anthropic API key (env: ANTHROPIC_API_KEY)",
        )
        parser.add_argument(
            "--base-url",
            type=str,
            default=None,
            help="Base URL for LLM proxy (env: LLM_PROXY_URL)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable detailed debug logging",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-process cases that already have key_allegations",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            default=None,
            help="Process a specific case by case_id",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        model = options["llm_model"]
        api_key = options["api_key"]
        base_url = options["base_url"] or os.environ.get("LLM_PROXY_URL")
        verbose = options["verbose"]
        force = options["force"]
        case_id = options.get("case_id")

        if verbose:
            logger.setLevel(logging.DEBUG)

        self.stdout.write(
            self.style.WARNING(
                f"{'[DRY RUN] ' if dry_run else ''}Starting CIAA allegation enrichment..."
            )
        )

        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise CommandError(
                "No API key provided. Set ANTHROPIC_API_KEY environment variable "
                "or pass --api-key."
            )

        client = self._init_client(api_key, base_url, model)

        cases = self._get_eligible_cases(limit, force, case_id)
        self.stdout.write(f"Found {len(cases)} eligible CIAA DRAFT case(s) to process")

        self._fetch_source_cache(cases)

        for idx, case in enumerate(cases, 1):
            try:
                self.stdout.write(
                    f"\n[{idx}/{len(cases)}] {case.case_id} - {case.title[:80]}..."
                )
                self._process_case(case, client, model, dry_run)
            except Exception as e:
                self.stats["cases_failed"] += 1
                logger.exception(f"Error processing {case.case_id}: {e}")
                self.stdout.write(self.style.ERROR(f"FAILED: {case.case_id} - {e}"))

        self._print_summary(dry_run)

    def _init_client(self, api_key, base_url, model):
        import anthropic

        if base_url:
            return anthropic.Anthropic(
                api_key=api_key,
                base_url=base_url,
            )
        return anthropic.Anthropic(api_key=api_key)

    def _get_eligible_cases(self, limit, force, case_id):
        queryset = Case.objects.filter(state=CaseState.DRAFT)

        if case_id:
            queryset = queryset.filter(case_id=case_id)

        if not force:
            queryset = queryset.filter(key_allegations__exact=[])

        cases = list(queryset)
        eligible = []
        for case in cases:
            if case.court_cases and isinstance(case.court_cases, list):
                has_ciaa = any(ref.startswith("special:") for ref in case.court_cases)
                if has_ciaa:
                    eligible.append(case)
            else:
                eligible.append(case)

        if limit and limit > 0:
            eligible = eligible[:limit]

        return eligible

    def _fetch_source_cache(self, cases):
        source_ids = set()
        for case in cases:
            if case.evidence:
                for entry in case.evidence:
                    if sid := entry.get("source_id"):
                        source_ids.add(sid)

        self._source_lookup = {
            source.source_id: source
            for source in DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            )
        }
        logger.debug(f"Cached {len(self._source_lookup)} DocumentSource records")

    def _process_case(self, case, client, model, dry_run):
        self.stats["cases_processed"] += 1

        if not case.evidence:
            self.stats["cases_skipped"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No evidence"))
            return

        press_release_text = self._collect_press_release_content(case)
        if not press_release_text:
            self.stats["cases_no_content"] += 1
            if not dry_run:
                note = (
                    "enrich_ciaa_allegations: No press release markdown content "
                    "available for LLM extraction"
                )
                current = case.missing_details or ""
                if note not in current:
                    case.missing_details = f"{current}\n{note}" if current else note
                    case.save(update_fields=["missing_details"])
            self.stdout.write(
                self.style.WARNING("  SKIPPED: No press release markdown content")
            )
            return

        bigo = case.bigo
        bigo_display = f"रू {bigo:,}" if bigo else "उल्लेख छैन"

        prompt = NEPALI_ALLEGATION_PROMPT.format(
            case_title=case.title,
            bigo=bigo_display,
            press_release=press_release_text[:60000],
        )

        logger.debug(f"Prompt length: {len(prompt)} chars")
        self.stdout.write(
            f"  Sending to LLM ({len(press_release_text)} chars of content)..."
        )

        allegations = self._call_llm(client, model, prompt)
        if not allegations:
            self.stats["cases_failed"] += 1
            self.stdout.write(self.style.ERROR("  FAILED: No allegations extracted"))
            return

        if len(allegations) < 2:
            self.stdout.write(
                self.style.WARNING(
                    f"  WARNING: Only {len(allegations)} allegation(s) extracted (want 2-5)"
                )
            )
        if len(allegations) > 5:
            allegations = allegations[:5]
            self.stdout.write(self.style.WARNING("  Truncated to 5 allegations"))

        self.stdout.write(f"  Extracted {len(allegations)} allegation(s):")
        for a in allegations:
            self.stdout.write(f"    - {a}")

        if not dry_run:
            case.key_allegations = allegations
            case.save(update_fields=["key_allegations"])
            self.stats["cases_enriched"] += 1
            self.stdout.write(
                self.style.SUCCESS(f"  ENRICHED: {len(allegations)} allegation(s)")
            )
        else:
            self.stats["cases_enriched"] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"  [DRY RUN] Would save {len(allegations)} allegation(s)"
                )
            )

    def _collect_press_release_content(self, case):
        texts = []
        if not case.evidence:
            return ""

        for entry in case.evidence:
            source_id = entry.get("source_id")
            if not source_id:
                continue

            source = self._source_lookup.get(source_id)
            if not source:
                continue

            urls = (
                source.url
                if isinstance(source.url, list)
                else [source.url] if source.url else []
            )

            for url in urls:
                if not isinstance(url, str):
                    continue
                if "ngm-store.jawafdehi.org" in url or url.endswith(".md"):
                    content = self._fetch_and_convert_content(url)
                    if content:
                        texts.append(content)
                        break

        return "\n\n".join(texts)

    def _fetch_and_convert_content(self, url):
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout fetching {url}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            return None

        content_type = resp.headers.get("content-type", "").lower()

        if "text/plain" in content_type or url.endswith(".md"):
            content = resp.text
            if len(content) < 50:
                logger.debug(f"Content too short from {url}: {len(content)} chars")
                return None
            return content

        ext = ".tmp"
        if url.lower().endswith(".pdf"):
            ext = ".pdf"
        elif url.lower().endswith(".docx"):
            ext = ".docx"
        elif url.lower().endswith(".doc"):
            ext = ".doc"
        elif "text/html" in content_type:
            ext = ".html"

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name

        try:
            from markitdown import MarkItDown

            md = MarkItDown()
            result = md.convert(tmp_path)
            if result and result.text_content and len(result.text_content.strip()) >= 50:
                return result.text_content.strip()
            logger.debug(f"MarkItDown conversion returned short content from {url}")
            return None
        except ImportError:
            logger.warning("markitdown/likhit not installed, falling back to raw text")
            try:
                return resp.text
            except Exception:
                return None
        except Exception as e:
            logger.warning(f"likhit conversion failed for {url}: {e}")
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _call_llm(self, client, model, prompt):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                )
                raw = response.content[0].text
                logger.debug(f"LLM response: {raw[:500]}...")
                return self._parse_allegations(raw)
            except Exception as e:
                logger.warning(f"LLM call attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    wait = 2**attempt
                    self.stdout.write(self.style.WARNING(f"  Retrying in {wait}s..."))
                    time.sleep(wait)
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  LLM call failed after {max_retries} attempts: {e}"
                        )
                    )
                    return None

    def _parse_allegations(self, raw):
        raw = raw.strip()
        json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
        if json_match:
            raw = json_match.group(1).strip()
        else:
            brace_start = raw.find("{")
            brace_end = raw.rfind("}")
            if brace_start != -1 and brace_end != -1:
                raw = raw[brace_start : brace_end + 1]

        try:
            data = json.loads(raw)
            allegations = data.get("allegations", [])
        except json.JSONDecodeError:
            lines = [
                line.strip().lstrip("0123456789.-) ")
                for line in raw.split("\n")
                if line.strip()
            ]
            allegations = [line for line in lines if len(line) > 10]

        allegations = [
            a.strip() for a in allegations if isinstance(a, str) and a.strip()
        ]
        return allegations[:5]

    def _print_summary(self, dry_run):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.WARNING(f"{'[DRY RUN] ' if dry_run else ''}SUMMARY")
        )
        self.stdout.write("=" * 60)
        self.stdout.write(f"Cases processed:  {self.stats['cases_processed']}")
        self.stdout.write(
            self.style.SUCCESS(f"Cases enriched:   {self.stats['cases_enriched']}")
        )
        self.stdout.write(
            self.style.WARNING(f"Cases skipped:    {self.stats['cases_skipped']}")
        )
        self.stdout.write(
            self.style.WARNING(f"Cases no content:  {self.stats['cases_no_content']}")
        )
        if self.stats["cases_failed"] > 0:
            self.stdout.write(
                self.style.ERROR(f"Cases failed:     {self.stats['cases_failed']}")
            )
        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis was a dry run. No changes were made to the database."
                )
            )
            self.stdout.write("Run without --dry-run to apply changes.")
