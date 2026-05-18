"""
Management command to enrich CIAA DRAFT cases with key allegations extracted
via LLM from press release markdown content.

Usage::

    python manage.py enrich_ciaa_allegations --dry-run
    python manage.py enrich_ciaa_allegations --limit 10
    python manage.py enrich_ciaa_allegations --llm-model claude-sonnet-4-5 --verbose

Environment variables::

    ANTHROPIC_API_KEY        — API key for Anthropic (fallback)
    JAWAFDEHI_LLM_API_KEY    — API key for Jawafdehi LLM proxy
    JAWAFDEHI_LLM_PROXY_URL  — base URL for Jawafdehi LLM proxy
    JAWAFDEHI_LLM_TIMEOUT_SECONDS — timeout in seconds (default 300)
    OPENCODE_API_KEY         — API key for OpenCode Go
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
DEFAULT_OPENCODE_BASE = "https://opencode.ai/zen/go/v1"
DEFAULT_LLM_TIMEOUT = 300
MAX_LLM_RETRIES = 3
MINIMAX_MODELS = frozenset({"minimax-m2.5", "minimax-m2.7"})


def normalize_model(model: str) -> str:
    model = model.strip()
    for prefix in ("opencode-go/", "openai:"):
        if model.startswith(prefix):
            model = model[len(prefix) :]
    return model


def normalize_base_url(url: str | None) -> str:
    if url and url.strip():
        url = url.strip().rstrip("/")
    else:
        url = os.environ.get("JAWAFDEHI_LLM_PROXY_URL", DEFAULT_OPENCODE_BASE).rstrip(
            "/"
        )
    if url.endswith("/zen/v1"):
        url = url.replace("/zen/v1", "/zen/go/v1")
    elif url.endswith("/zen/go"):
        url += "/v1"
    return url


def resolve_api_key(cli_key: str | None = None) -> str:
    if cli_key and cli_key.strip():
        return cli_key.strip()
    for env_var in (
        "JAWAFDEHI_LLM_API_KEY",
        "OPENCODE_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        val = os.environ.get(env_var)
        if val:
            return val
    raise CommandError(
        "No API key provided. Set --llm-api-key, JAWAFDEHI_LLM_API_KEY, "
        "OPENCODE_API_KEY, or ANTHROPIC_API_KEY."
    )


def _llm_endpoint(base_url: str, model: str) -> str:
    normalized = normalize_model(model)
    base = base_url.rstrip("/")
    if normalized in MINIMAX_MODELS:
        return f"{base}/messages"
    return f"{base}/chat/completions"


def _llm_timeout(cli_timeout: int | None = None) -> int:
    if cli_timeout is not None:
        return cli_timeout
    env = os.environ.get("JAWAFDEHI_LLM_TIMEOUT_SECONDS")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_LLM_TIMEOUT


def _build_llm_opencode_body(
    normalized_model: str, is_minimax: bool, prompt: str
) -> str:
    if is_minimax:
        body = {
            "model": normalized_model,
            "max_tokens": 3000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
    else:
        body = {
            "model": normalized_model,
            "max_tokens": 3000,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
    return json.dumps(body)


def _parse_llm_opencode_response(payload: dict, is_minimax: bool) -> str:
    if is_minimax:
        return payload.get("content", [{}])[0].get("text", "")
    return payload.get("choices", [{}])[0].get("message", {}).get("content", "")


def _format_llm_http_error(status: int, body_snippet: str) -> str:
    msg = f"LLM HTTP {status}: {body_snippet[:300]}"
    if status == 429:
        msg += (
            " Hint: OpenCode Go usage limits may apply. "
            "Try reducing --limit or switching models."
        )
    return msg


def _extract_fenced_block(raw: str) -> str | None:
    if not raw.startswith("```"):
        return None
    lines = raw.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip().startswith("```"):
            end_idx = i
            break
    if end_idx is None:
        return None
    inner = "\n".join(lines[1:end_idx]).strip()
    return inner if inner else None


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
        raise CommandError(f"Refusing to write outside output directory: '{filename}'")
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
            help="Model id (accepts opencode-go/ prefix, defaults to claude-sonnet-4-5)",
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=None,
            help="Base URL for LLM API (env: JAWAFDEHI_LLM_PROXY_URL, "
            "default: https://opencode.ai/zen/go/v1)",
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            default=None,
            help="API key (env: JAWAFDEHI_LLM_API_KEY, OPENCODE_API_KEY, "
            "ANTHROPIC_API_KEY)",
        )
        parser.add_argument(
            "--llm-timeout",
            type=int,
            default=None,
            help="LLM request timeout in seconds (env: JAWAFDEHI_LLM_TIMEOUT_SECONDS, "
            "default: 300)",
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
        parser.add_argument(
            "--base-url",
            type=str,
            default=None,
            help=("Deprecated: use --llm-base-url instead. " "Base URL for LLM API."),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        model = options["llm_model"]
        base_url = options["llm_base_url"] or options.get("base_url")
        llm_api_key = options.get("llm_api_key")
        llm_timeout = options.get("llm_timeout")
        verbose = options["verbose"]
        force = options["force"]
        case_id = options.get("case_id")

        if verbose:
            logger.setLevel(logging.DEBUG)

        model = normalize_model(model)
        base_url = normalize_base_url(base_url)
        api_key = resolve_api_key(llm_api_key)
        timeout = _llm_timeout(llm_timeout)

        self.stdout.write(
            self.style.WARNING(
                f"{'[DRY RUN] ' if dry_run else ''}Starting CIAA allegation enrichment..."
            )
        )

        cases = self._get_eligible_cases(limit, force, case_id)
        self.stdout.write(f"Found {len(cases)} eligible CIAA DRAFT case(s) to process")

        self._fetch_source_cache(cases)

        is_opencode = "anthropic.com" not in base_url

        for idx, case in enumerate(cases, 1):
            try:
                self.stdout.write(
                    f"\n[{idx}/{len(cases)}] {case.case_id} - {case.title[:80]}..."
                )
                self._process_case(
                    case, model, base_url, api_key, timeout, is_opencode, dry_run
                )
            except Exception as e:
                self.stats["cases_failed"] += 1
                logger.exception(f"Error processing {case.case_id}: {e}")
                self.stdout.write(self.style.ERROR(f"FAILED: {case.case_id} - {e}"))

        self._print_summary(dry_run)

    def _init_client(self, api_key, base_url):
        if "anthropic.com" in (base_url or ""):
            import anthropic

            return anthropic.Anthropic(api_key=api_key, base_url=base_url or None)
        return None

    def _build_llm_headers(self, api_key: str) -> dict:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "JawafdehiAPI/1.0 enrich_ciaa_allegations",
        }

    def _call_llm_opencode(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: int,
        prompt: str,
    ) -> str | None:
        endpoint = _llm_endpoint(base_url, model)
        normalized_model = normalize_model(model)
        headers = self._build_llm_headers(api_key)
        is_minimax = normalized_model in MINIMAX_MODELS
        body = _build_llm_opencode_body(normalized_model, is_minimax, prompt)

        last_status = None
        last_body_snippet = ""

        for attempt in range(1, MAX_LLM_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=body.encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                ctx = ssl.create_default_context()
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
                payload = json.loads(resp.read().decode("utf-8"))
                raw = _parse_llm_opencode_response(payload, is_minimax)
                logger.debug(f"LLM response: {raw[:500]}...")
                return raw

            except urllib.error.HTTPError as e:
                last_status = e.code
                try:
                    last_body_snippet = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    last_body_snippet = "<unreadable>"
                if self._should_retry_llm_http(attempt, last_status):
                    continue
                raise CommandError(
                    _format_llm_http_error(last_status, last_body_snippet)
                )

            except (urllib.error.URLError, OSError, ssl.SSLError) as e:
                if self._should_retry_llm_network(attempt, e):
                    continue
                raise CommandError(
                    f"LLM connection failed after {MAX_LLM_RETRIES} attempts: {e}"
                )

        return None

    def _should_retry_llm_http(self, attempt: int, status: int) -> bool:
        if attempt >= MAX_LLM_RETRIES:
            return False
        if status not in (429, 503):
            return False
        wait = 2**attempt
        hint = ""
        if status == 429:
            hint = (
                " (OpenCode Go usage limits may apply; "
                "consider reducing --limit or using a different model)"
            )
        self.stdout.write(
            self.style.WARNING(
                f"  LLM {status} on attempt {attempt}, " f"retrying in {wait}s...{hint}"
            )
        )
        time.sleep(wait)
        return True

    def _should_retry_llm_network(self, attempt: int, error: Exception) -> bool:
        if attempt >= MAX_LLM_RETRIES:
            return False
        wait = 2**attempt
        self.stdout.write(
            self.style.WARNING(
                f"  LLM connection error on attempt {attempt} "
                f"({error}), retrying in {wait}s..."
            )
        )
        time.sleep(wait)
        return True

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

    def _process_case(
        self, case, model, base_url, api_key, timeout, is_opencode, dry_run
    ):
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

        if is_opencode:
            raw = self._call_llm_opencode(model, base_url, api_key, timeout, prompt)
            if not raw:
                self.stats["cases_failed"] += 1
                self.stdout.write(self.style.ERROR("  FAILED: No LLM response"))
                return
            allegations = self._parse_allegations(raw)
        else:
            client = self._init_client(api_key, base_url)
            allegations = self._call_llm_anthropic(client, model, prompt)
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
            with urllib.request.urlopen(
                request, timeout=30, context=context
            ) as response:
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

    def _call_llm_anthropic(self, client, model, prompt):
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
        body = _extract_fenced_block(raw)
        if body is not None:
            return body
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
