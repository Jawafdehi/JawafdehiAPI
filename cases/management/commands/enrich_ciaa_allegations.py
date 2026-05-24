"""
Django management command to extract key allegations from CIAA press release
documents using LLM extraction.

Phase 1d of CIAA FY 080/081 Case Enrichment pipeline.
Populates ``Case.key_allegations`` with 2-5 structured allegation statements
in Nepali extracted from CIAA press release text.

Processes all DRAFT cases with empty ``key_allegations``, regardless of
court case naming conventions.

Idempotent: skips cases with non-empty ``key_allegations``.

Usage::

    python manage.py enrich_ciaa_allegations --dry-run
    python manage.py enrich_ciaa_allegations --case-id case-0123
    python manage.py enrich_ciaa_allegations --limit 10 --verbose
"""

import json
import logging
import os
from typing import Optional
from urllib.parse import urlparse

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction

from cases.models import Case, DocumentSource
from cases.services.priority_case_loader import filter_by_priority, load_priority_cases

logger = logging.getLogger(__name__)

_ALLOWED_HOSTS = frozenset({"ciaa.gov.np", "ngm-store.jawafdehi.org"})

EXTRACTION_SYSTEM_PROMPT = """\
You are a Nepali legal analyst extracting structured key allegations from \
CIAA (Commission for the Investigation of Abuse of Authority) press releases.

Every allegation MUST:
1. Be factually grounded in the provided press release — NO fabrication
2. Be written in professional, accessible Nepali
3. Name the persons involved and their official positions
4. Describe the specific misconduct mechanism (what was done and how)
5. Include the disputed amount when mentioned in the source
6. Include the time period (date range or fiscal year) when specified
7. Be self-contained — understandable without additional context
8. Follow the established Jawafdehi allegation style (see examples below)

Each allegation is 1-3 complete sentences. Use formal but clear Nepali.

DO NOT:
- Fabricate or embellish beyond the source text
- Use legal jargon without explanation
- State legal conclusions about guilt or innocence
- Write vague statements
- Mix multiple unrelated misconducts into one allegation

STRUCTURE each allegation as:
"Kasle — ke garyo — kasari — kati rakam — kun avadhima"
(Who — did what — how — what amount — during what period)

REFERENCE EXAMPLES from published Jawafdehi cases:

Example 1 (Illegal property):
"Kamal Raj Gautam le miti 2055/01/07 dekhi 2079/12/24 samma saarvajanik \
pad dharan garda vaidh aaybhanda ru. 2,51,78,687.71 badhi sampatti \
kharcha tatha lagani gari gairkanuni rupma sampatti aarjan gareko."

Example 2 (Procurement fraud):
"Prativadiharuko NCBW-KMC ko thekkama Pending Litigation nahune vishayalai \
Pending Litigation raheko bhani galat mulyankan prativedan khada gari \
saarvajanik sampatti badaniyatpurvak hani noksani puryayeko."

Example 3 (Bribery):
"Mohan Bahadur Basnet le nagar pramukh padko durupayog gari Padma Company \
haru ra Raju Prasad Kadel lai kar chhut ra jagga upalabdhata lagayat \
anuchit labh puryai so bapat karib ru. 9.22 karod ghus/rishwat liyeko."

Example 4 (Embezzlement):
"Prativadiharuko milemato ma Hulak Bachat Bank ma bachatkartaharuko \
nikshep rakam bank dakhila nagari apchalan gari hinamina gareko."
"""

EXTRACTION_USER_PROMPT = """\
Extract 2-5 key allegation statements from this CIAA press release.

Case title: {case_title}

Instructions:
- Each allegation must be a complete, self-contained statement in Nepali
- Follow the Jawafdehi allegation style shown in the system prompt
- Include names, positions, amounts, and time periods when available
- Extract distinct allegations, not variations of the same claim

IMPORTANT: Return ONLY a valid JSON array of strings.
Example: ["First allegation...", "Second allegation..."]
No explanations, no markdown, no text outside the JSON array.

Press release text:

{press_release_text}
"""


class Command(BaseCommand):
    help = (
        "Extract key allegations from CIAA press release documents via LLM. "
        "Populates key_allegations for CIAA Special Court draft cases."
    )

    def add_arguments(self, parser):
        """Register CLI flags for dry-run, case selection, LLM config, and verbosity."""
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
            "--limit",
            type=int,
            help="Maximum number of cases to process",
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
        """Orchestrate the enrichment pipeline: discover cases, extract press release content, call LLM, persist results."""
        dry_run = options["dry_run"]
        case_id = options.get("case_id")
        priority = options["priority"]
        all_cases_flag = options.get("all_cases")
        limit = options.get("limit")
        llm_model = options["llm_model"]
        llm_base_url = options["llm_base_url"]
        llm_api_key = options.get("llm_api_key")
        verbose = options.get("verbose")

        if priority and case_id:
            raise CommandError("--priority and --case-id are mutually exclusive")

        if not any([priority, all_cases_flag, case_id]):
            self.stdout.write(
                self.style.NOTICE(
                    "Processing all DRAFT CIAA cases (default). "
                    "Use --all to make this explicit or --priority to filter."
                )
            )

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

        cases = self._get_ciaa_cases(case_id=case_id, limit=limit, priority=priority)
        total = len(cases)

        self.stdout.write(
            f"Found {total} CIAA draft cases to process. " f"Model: {llm_model}"
        )

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

    def _resolve_api_key(self, cli_key: Optional[str]) -> Optional[str]:
        """Resolve LLM API key from CLI argument or environment variables."""
        if cli_key:
            return cli_key
        return os.environ.get("JAWAFDEHI_LLM_API_KEY") or os.environ.get(
            "ANTHROPIC_API_KEY"
        )

    def _get_ciaa_cases(
        self,
        case_id: Optional[str] = None,
        limit: Optional[int] = None,
        priority: bool = False,
    ) -> list[Case]:
        """Return DRAFT cases with empty key_allegations that are candidates for enrichment."""
        all_cases = []
        queryset = Case.objects.filter(state="DRAFT")

        if case_id:
            queryset = queryset.filter(case_id=case_id)

        if priority:
            priority_list = load_priority_cases()
            logger.info(
                "Priority mode: loaded %d case numbers across all fiscal years",
                len(priority_list),
            )
            queryset = filter_by_priority(queryset, priority_list)

        for case in queryset.order_by("case_id"):
            if case.key_allegations:
                self.stats["cases_already_populated"] += 1
                continue
            all_cases.append(case)
            if limit and len(all_cases) >= limit:
                break

        return all_cases

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
        """Process a single case: acquire content, extract allegations via LLM, persist or dry-run."""
        self.stats["cases_processed"] += 1
        self.stdout.write(f"\n[{idx}/{total}] {case.case_id} — {case.title[:80]}")

        press_release_text = self._get_press_release_content(case)
        if not press_release_text:
            self.stats["cases_no_content"] += 1
            self.stdout.write(
                self.style.WARNING("  No press release content found — skipping")
            )
            return

        self.stdout.write(f"  Press release content: {len(press_release_text)} chars")

        if dry_run and not llm_api_key:
            self.stdout.write(
                self.style.WARNING("  [DRY RUN] No API key — skipping LLM extraction")
            )
            return

        try:
            allegations = self._extract_allegations(
                press_release_text=press_release_text,
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

        if not allegations:
            self.stats["cases_skipped"] += 1
            self.stdout.write(
                self.style.WARNING("  LLM returned no allegations — skipping")
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"  Extracted {len(allegations)} allegation(s):")
        )
        for i, allegation in enumerate(allegations, 1):
            self.stdout.write(f"    {i}. {allegation[:120]}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("  [DRY RUN] Would save but --dry-run is set")
            )
        else:
            self._save_allegations(case, allegations)
            self.stats["cases_enriched"] += 1

    def _get_press_release_content(self, case: Case) -> Optional[str]:
        """Extract press release text for a CIAA case.

        Strategy:
        1. Look for DocumentSource records linked via evidence with press release content
           in the description field (populated by Phase 1b).
        2. If description is insufficient, try downloading the file from NGM store URLs
           or direct CIAA website URLs (ciaa.gov.np) and extracting text via
           likhit/markitdown.
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
            ).only("source_id", "description", "title", "url")
        )
        if not sources:
            logger.debug("  No DocumentSource records found")
            return None

        source_by_id = {s.source_id: s for s in sources}

        press_release_parts = []

        for sid in source_ids:
            source = source_by_id.get(sid)
            if source is None:
                continue
            if not self._is_press_release_source(source):
                continue

            description = (source.description or "").strip()
            if len(description) > 200:
                press_release_parts.append(description)
                break

            if isinstance(source.url, list):
                for url in source.url:
                    parsed = urlparse(url)
                    if parsed.hostname and parsed.hostname in _ALLOWED_HOSTS:
                        content = self._convert_to_markdown(url)
                        if content and len(content) > 200:
                            press_release_parts.append(content)
                            break

            if press_release_parts:
                break

        if not press_release_parts:
            logger.debug("  No usable press release content in any source")
            return None

        return "\n\n".join(press_release_parts)

    def _is_press_release_source(self, source: DocumentSource) -> bool:
        """Check if a DocumentSource is a CIAA press release."""
        title_lower = (source.title or "").lower()
        if "press release" in title_lower or "ciaa" in title_lower:
            return True

        if isinstance(source.url, list):
            for url in source.url:
                parsed = urlparse(url)
                if parsed.hostname and parsed.hostname in _ALLOWED_HOSTS:
                    return True
        return False

    def _convert_to_markdown(self, url: str) -> Optional[str]:
        """Download file from URL and convert to markdown using likhit.

        Pipeline: URL download -> temp file -> likhit/markitdown -> Nepali markdown.
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
            close_old_connections()
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
                for chunk in response.iter_content(chunk_size=8192):
                    tmp.write(chunk)

            import likhit  # noqa: F401 — registers Nepali converters
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
            close_old_connections()

    def _extract_allegations(
        self,
        press_release_text: str,
        case_title: str,
        llm_model: str,
        llm_base_url: str,
        llm_api_key: Optional[str],
    ) -> Optional[list[str]]:
        """Call LLM to extract key allegations from press release text."""
        prompt = EXTRACTION_USER_PROMPT.format(
            case_title=case_title,
            press_release_text=press_release_text[:30000],
        )

        response_text = self._call_llm(
            system_prompt=EXTRACTION_SYSTEM_PROMPT,
            user_prompt=prompt,
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
        )

        return self._parse_allegations_response(response_text)

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
            "max_tokens": 3000,
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

    def _parse_allegations_response(self, response_text: str) -> Optional[list[str]]:
        """Parse the LLM response to extract the JSON array of allegations."""
        text = response_text.strip()

        json_start = text.find("[")
        json_end = text.rfind("]")

        if json_start == -1 or json_end == -1 or json_end <= json_start:
            logger.warning("  Could not find JSON array in LLM response")
            logger.debug("  Response: %s", text[:500])
            return None

        json_str = text[json_start : json_end + 1]

        try:
            allegations = json.loads(json_str)
        except json.JSONDecodeError as exc:
            logger.warning("  Failed to parse JSON from LLM response: %s", exc)
            logger.debug("  JSON string: %s", json_str[:500])
            return None

        if isinstance(allegations, dict) and isinstance(
            allegations.get("allegations"), list
        ):
            allegations = allegations["allegations"]
        if not isinstance(allegations, list):
            logger.warning("  LLM returned non-list: %s", type(allegations).__name__)
            return None

        clean = []
        for item in allegations:
            if isinstance(item, str) and item.strip():
                clean.append(item.strip())
            elif isinstance(item, dict):
                combined = " ".join(
                    str(v).strip() for v in item.values() if v and str(v).strip()
                )
                if combined:
                    clean.append(combined)

        if not clean:
            return None

        if len(clean) < 2:
            logger.error(
                "  Only %d allegation(s) extracted, minimum is 2 — aborting", len(clean)
            )
            return None

        max_count = 5
        if len(clean) > max_count:
            clean = clean[:max_count]

        return clean

    def _save_allegations(self, case: Case, allegations: list[str]):
        """Persist key allegations to the database."""
        with transaction.atomic():
            case.key_allegations = allegations
            case.save(update_fields=["key_allegations", "updated_at"])
        logger.info("  Saved %d allegations to %s", len(allegations), case.case_id)

    def _print_summary(self, dry_run: bool):
        """Print final statistics table summarizing the enrichment run results."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.SUCCESS(
                f"{'[DRY RUN] ' if dry_run else ''}Allegation extraction complete."
            )
        )
        self.stdout.write(f"  Cases processed:        {self.stats['cases_processed']}")
        self.stdout.write(f"  Cases enriched:         {self.stats['cases_enriched']}")
        self.stdout.write(f"  Cases skipped:          {self.stats['cases_skipped']}")
        self.stdout.write(
            f"  No press release content: {self.stats['cases_no_content']}"
        )
        self.stdout.write(f"  LLM errors:             {self.stats['cases_llm_error']}")
        self.stdout.write(
            f"  Already populated:      {self.stats['cases_already_populated']}"
        )
