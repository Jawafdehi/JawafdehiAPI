"""
Management command to enrich CIAA DRAFT cases with key allegations extracted
via LLM from press release markdown content.

Usage::

    python manage.py enrich_ciaa_allegations --dry-run
    python manage.py enrich_ciaa_allegations --limit 10
    python manage.py enrich_ciaa_allegations --llm-model claude-sonnet-4-5 --verbose

Environment variables::

    ANTHROPIC_API_KEY  — API key for Anthropic (required)
    LLM_PROXY_URL      — optional base URL for an OpenAI-compatible proxy
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from cases.models import Case, CaseState, DocumentSource, SourceType

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 16 * 1024

SYSTEM_PROMPT = """You are a Nepali legal analyst extracting structured key allegations
from CIAA (Commission for the Investigation of Abuse of Authority) press releases.

Every allegation MUST:
1. Be factually grounded in the provided press release — NO fabrication
2. Be written in professional, accessible Nepali (नेपाली)
3. Name the persons involved and their official positions
4. Describe the specific misconduct mechanism (what was done and how)
5. Include the disputed amount (बिगो) when mentioned in the source
6. Include the time period (date range or fiscal year) when specified
7. Be self-contained — understandable without additional context
8. Follow the established Jawafdehi allegation style (see examples below)

Each allegation is 1-3 complete sentences. Use formal but clear Nepali.

DO NOT:
- Fabricate or embellish beyond the source text
- Use legal jargon without explanation
- State legal conclusions about guilt or innocence
- Write vague statements like "भ्रष्टाचार गरेको"
- Mix multiple unrelated misconducts into one allegation

STRUCTURE each allegation in Nepali as:
"कसले — के गर्यो — कसरी — कति रकम — कुन अवधिमा"
(Who — did what — how — what amount — during what period)

REFERENCE EXAMPLES from published Jawafdehi cases:

Example 1 (Illegal property accumulation):
"कमल राज गौतमले मिति २०५५/०१/०७ देखि २०७९/१२/२४ सम्म सार्वजनिक पद धारण
गर्दा वैध आयभन्दा रु. २,५१,७८,६८७.७१ बढी सम्पत्ति खर्च तथा लगानी गरी
गैरकानूनी रूपमा सम्पत्ति आर्जन गरेको।"

Example 2 (Procurement fraud):
"प्रतिवादीहरूको मिलेमतोमा काठमाडौं महानगरपालिकाको NCBW-KMC को ठेक्कामा
Pending Litigation नहुने विषयलाई Pending Litigation रहेको भनी गलत मूल्याङ्कन
प्रतिवेदन खडा गरी सार्वजनिक सम्पत्ति बदनियतपूर्वक हानि नोक्सानी पुर्याएको।"

Example 3 (Bribery and money laundering):
"मोहनबहादुर बस्नेतले नगर प्रमुख पदको दुरुपयोग गरी पद्मा कम्पनीहरू र राजु
प्रसाद कँडेललाई कर छुट र जग्गा उपलब्धता लगायत अनुचित लाभ पुर्याई सो बापत
करिब रु. ९.२२ करोड घुस/रिसवत लिएको।"

Example 4 (Embezzlement):
"प्रतिवादीहरूको मिलेमतोमा हुलाक बचत बैङ्कमा बचतकर्ताहरूको निक्षेप रकम
बैङ्क दाखिला नगरी अपचलन गरी हिनामिना गरेको।"
"""

USER_PROMPT_TEMPLATE = """Extract 2-5 key allegation statements from this CIAA press release.

Case title: {case_title}
Bigo amount: {bigo}

Instructions:
- Each allegation must be a complete, self-contained statement in Nepali
- Follow the Jawafdehi allegation style shown in the system prompt
- Include names, positions, amounts, and time periods when available
- Extract distinct allegations, not variations of the same claim

Press release text:

{press_release}

IMPORTANT: Return ONLY a valid JSON object with an "allegations" key.
Example:
{{"allegations": ["पहिलो मुख्य आरोप...", "दोस्रो मुख्य आरोप..."]}}
No explanations, no markdown, no text outside the JSON object."""


def _sanitize_download_filename(filename: str | None, source_id: str) -> str:
    raw = (filename or "").strip()
    if not raw:
        return f"{source_id}.bin"

    decoded = urllib.parse.unquote(raw)
    candidate = Path(decoded).name.strip()

    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"

    candidate = candidate.replace("\x00", "")
    candidate = re.sub(r"[<>:\"/\\|?*]+", "_", candidate).rstrip(" .")
    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"

    max_len = 200
    if len(candidate) <= max_len:
        return candidate

    suffix = "".join(Path(candidate).suffixes)
    stem = candidate[: -len(suffix)] if suffix else candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]

    stem_budget = max_len - len(suffix) - len(digest) - 1
    if stem_budget < 1:
        return f"{source_id}-{digest}{suffix}"[:max_len]

    truncated_stem = stem[:stem_budget].rstrip(" .-_")
    if not truncated_stem:
        truncated_stem = source_id

    return f"{truncated_stem}-{digest}{suffix}"


def _confined_output_path(output_dir: Path, filename: str) -> Path:
    output_dir_resolved = output_dir.resolve()
    out_path = (output_dir / filename).resolve()
    if output_dir_resolved not in out_path.parents:
        raise CommandError(
            f"Refusing to write outside output directory: '{filename}'"
        )
    return out_path


def _copy_stream_to_path_with_limit(in_file: Any, out_path: Path) -> None:
    total_bytes = 0
    try:
        with out_path.open("wb") as out_file:
            while True:
                chunk = in_file.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_DOWNLOAD_BYTES:
                    raise CommandError(
                        f"Downloaded source exceeds max size of {MAX_DOWNLOAD_BYTES} bytes."
                    )
                out_file.write(chunk)
    except OSError:
        out_path.unlink(missing_ok=True)
        raise
    except CommandError:
        out_path.unlink(missing_ok=True)
        raise


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

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise CommandError(
                "No API key provided. Set the ANTHROPIC_API_KEY environment variable."
            )

        client = self._init_client(api_key, base_url)

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

    def _init_client(self, api_key, base_url):
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

        eligible = self._filter_eligible(list(queryset), force)

        if limit is not None:
            if limit < 0:
                raise CommandError(f"--limit must be >= 0, got {limit}")
            eligible = eligible[:limit] if limit > 0 else []

        return eligible

    def _filter_eligible(self, cases, force):
        result = []
        for case in cases:
            if not force and case.key_allegations:
                continue
            if case.court_cases and isinstance(case.court_cases, list):
                if any(ref.startswith("special:") for ref in case.court_cases):
                    result.append(case)
            else:
                result.append(case)
        return result

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
            ).prefetch_related("uploaded_files")
        }
        logger.debug(f"Cached {len(self._source_lookup)} DocumentSource records")

    def _process_case(self, case, client, model, dry_run):
        self.stats["cases_processed"] += 1

        if not case.evidence:
            self.stats["cases_skipped"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No evidence"))
            return

        press_release_text = self._acquire_press_release_text(case, dry_run)
        if press_release_text is None:
            return

        bigo = case.bigo
        bigo_display = f"रू {bigo:,}" if bigo else "उल्लेख छैन"

        prompt = USER_PROMPT_TEMPLATE.format(
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

        allegations = self._trim_allegations(allegations)
        self._report_allegations(allegations)

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

    def _record_missing_details(self, case, note):
        if not note:
            return
        current = case.missing_details or ""
        if note not in current:
            case.missing_details = f"{current}\n{note}" if current else note
            case.save(update_fields=["missing_details"])

    def _acquire_press_release_text(self, case, dry_run):
        source = self._select_press_release_source(case)
        if not source:
            self.stats["cases_no_content"] += 1
            if not dry_run:
                self._record_missing_details(
                    case,
                    "enrich_ciaa_allegations: No press release source found in evidence",
                )
            self.stdout.write(
                self.style.WARNING("  SKIPPED: No press release source found")
            )
            return None

        try:
            press_release_text = self._convert_source_to_markdown(source)
        except Exception as e:
            self.stats["cases_no_content"] += 1
            if not dry_run:
                self._record_missing_details(
                    case,
                    f"enrich_ciaa_allegations: Failed to convert source to markdown: {e!s}",
                )
            self.stdout.write(
                self.style.WARNING(
                    f"  SKIPPED: Failed to convert source to markdown: {e!s}"
                )
            )
            return None

        if not press_release_text or len(press_release_text.strip()) < 50:
            self.stats["cases_no_content"] += 1
            if not dry_run:
                self._record_missing_details(
                    case,
                    "enrich_ciaa_allegations: No press release markdown content "
                    "available for LLM extraction",
                )
            self.stdout.write(
                self.style.WARNING("  SKIPPED: No press release markdown content")
            )
            return None

        return press_release_text

    def _trim_allegations(self, allegations):
        if len(allegations) < 2:
            self.stdout.write(
                self.style.WARNING(
                    f"  WARNING: Only {len(allegations)} allegation(s) extracted (want 2-5)"
                )
            )
        if len(allegations) > 5:
            allegations = allegations[:5]
            self.stdout.write(self.style.WARNING("  Truncated to 5 allegations"))
        return allegations

    def _report_allegations(self, allegations):
        self.stdout.write(f"  Extracted {len(allegations)} allegation(s):")
        for a in allegations:
            self.stdout.write(f"    - {a}")

    def _select_press_release_source(self, case: Case) -> DocumentSource | None:
        source_ids = [
            item["source_id"]
            for item in (case.evidence or [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
        if not source_ids:
            return None

        sources = [
            s
            for s in (self._source_lookup.get(sid) for sid in source_ids)
            if s is not None
        ]
        if not sources:
            return None

        ranked = sorted(
            ((self._score_source_for_press_release(s), s) for s in sources),
            key=lambda row: row[0],
            reverse=True,
        )
        best_score, best_source = ranked[0]
        return best_source if best_score > 0 else None

    def _score_source_for_press_release(self, source: DocumentSource) -> int:
        upload_names = [
            file.filename or Path(file.file.name).name
            for file in source.uploaded_files.all()
        ]
        url_text = " ".join(source.url or [])
        corpus = " ".join(
            [
                source.title or "",
                source.description or "",
                source.uploaded_filename or "",
                url_text,
                " ".join(upload_names),
            ]
        ).lower()

        score = 0
        press_keywords = [
            "press release",
            "pressrelease",
            "press-release",
            "प्रेस विज्ञप्ति",
            "विज्ञप्ति",
        ]
        ciaa_keywords = ["ciaa", "अख्तियार"]

        if any(keyword in corpus for keyword in press_keywords):
            score += 5
        if any(keyword in corpus for keyword in ciaa_keywords):
            score += 3
        if source.source_type == SourceType.OFFICIAL_GOVERNMENT:
            score += 1
        return score

    def _convert_source_to_markdown(self, source: DocumentSource) -> str:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise CommandError(
                "markitdown is required for allegation enrichment conversion. "
                "Install conversion dependencies (markitdown + likhit plugin)."
            ) from exc

        converter = MarkItDown(enable_plugins=True)
        with tempfile.TemporaryDirectory(prefix="allegation-enrichment-") as tmp_dir:
            temp_path = self._download_source_to_path(source, Path(tmp_dir))
            if temp_path:
                result = converter.convert_uri(temp_path.resolve().as_uri())
                return result.markdown

            source_url = self._pick_source_url(source)
            if not source_url:
                raise CommandError(
                    f"No downloadable source found for source_id={source.source_id}."
                )
            source_url = self._validate_url_scheme(source_url)
            result = converter.convert_uri(source_url)
            return result.markdown

    def _download_source_to_path(
        self, source: DocumentSource, output_dir: Path
    ) -> Path | None:
        if source.uploaded_file:
            filename = _sanitize_download_filename(
                source.uploaded_filename or source.uploaded_file.name,
                source.source_id,
            )
            out_path = _confined_output_path(output_dir, filename)
            with source.uploaded_file.open("rb") as in_file:
                _copy_stream_to_path_with_limit(in_file, out_path)
            return out_path

        uploaded = source.uploaded_files.first()
        if uploaded and uploaded.file:
            filename = _sanitize_download_filename(
                uploaded.filename or uploaded.file.name,
                source.source_id,
            )
            out_path = _confined_output_path(output_dir, filename)
            with uploaded.file.open("rb") as in_file:
                _copy_stream_to_path_with_limit(in_file, out_path)
            return out_path

        source_url = self._pick_source_url(source)
        if not source_url:
            return None

        source_url = self._validate_url_scheme(source_url)
        parsed = urllib.parse.urlparse(source_url)
        guessed_name = _sanitize_download_filename(parsed.path, source.source_id)
        out_path = _confined_output_path(output_dir, guessed_name)
        try:
            request = urllib.request.Request(
                source_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                },
            )
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            with urllib.request.urlopen(request, timeout=30, context=context) as response:
                _copy_stream_to_path_with_limit(response, out_path)
            return out_path
        except OSError:
            out_path.unlink(missing_ok=True)
            return None
        except CommandError:
            out_path.unlink(missing_ok=True)
            raise

    def _pick_source_url(self, source: DocumentSource) -> str | None:
        urls = [
            url for url in (source.url or []) if isinstance(url, str) and url.strip()
        ]
        return urls[0].strip() if urls else None

    def _validate_url_scheme(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
        raise ValueError(
            f"Invalid URL '{url}'. Only http and https URLs are allowed with a host."
        )

    def _call_llm(self, client, model, prompt):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=3000,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
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

    def _extract_json_body(self, raw):
        raw = raw.strip()
        json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
        if json_match:
            return json_match.group(1).strip()
        brace_start = raw.find("{")
        brace_end = raw.rfind("}")
        if brace_start != -1 and brace_end != -1:
            return raw[brace_start : brace_end + 1]
        return raw

    def _parse_json_to_allegations(self, raw):
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            result = data.get("allegations", data.get("response", []))
            if not isinstance(result, list):
                return [str(result)]
            return result
        return []

    def _parse_fallback_allegations(self, raw):
        lines = [
            line.strip().lstrip("0123456789.-) ")
            for line in raw.split("\n")
            if line.strip()
        ]
        allegations = [line for line in lines if len(line) > 10]
        return allegations if allegations else None

    def _flatten_allegation_items(self, allegations):
        flat = []
        for a in allegations:
            if isinstance(a, str) and a.strip():
                flat.append(a.strip())
            elif isinstance(a, dict):
                vals = [str(v) for v in a.values() if v]
                if vals:
                    flat.append("; ".join(vals))
        return flat

    def _parse_allegations(self, raw):
        body = self._extract_json_body(raw)
        try:
            allegations = self._parse_json_to_allegations(body)
        except json.JSONDecodeError:
            return self._parse_fallback_allegations(body)

        flat = self._flatten_allegation_items(allegations)
        if not flat:
            return None
        return flat[:5]

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
