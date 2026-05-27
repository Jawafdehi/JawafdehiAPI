"""Enrich DRAFT CIAA cases with Markdown case overviews from charge sheet evidence."""

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from cases.models import Case, CaseState, DocumentSource, SourceType

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 16 * 1024
DEFAULT_OPENCODE_BASE = "https://opencode.ai/zen/go/v1"
DEFAULT_LLM_TIMEOUT = 300
MAX_LLM_RETRIES = 3
MINIMAX_MODELS = frozenset({"minimax-m2.5", "minimax-m2.7"})
DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
_CLOUD_METADATA_IP = "169.254.169.254"  # NOSONAR — cloud metadata link-local
_SSRF_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", _CLOUD_METADATA_IP, "metadata", "0.0.0.0"})

EXTRACTION_SYSTEM_PROMPT = """You are a Nepali legal document parser specialized in CIAA charge sheets (अभियोगपत्र).
Extract structured data from charge sheet text converted from DOCX/PDF.

Rules:
1. Extract ONLY information explicitly present. Do NOT fabricate.
2. Preserve exact names, dates, amounts, legal citations.
3. For fiscal analysis, extract every fiscal year row separately.
4. If a field is missing, set it to null or omit it.
5. Preserve dates in the original Nepali calendar format.

Return valid JSON only. No markdown. No explanation."""

EXTRACTION_USER_PROMPT = """Extract structured case data from this CIAA charge sheet text.

Case context:
- Case ID: {case_id}
- Case title: {case_title}
- Known court cases: {court_cases}
- Known bigo amount: {bigo}

Return JSON with these keys:
- accused_persons: list of {{name, position, institution, employment_dates, role_in_case}}
- case_metadata: {{case_number, filing_date, court, charge_sheet_number, complaint_numbers, investigation_period}}
- fiscal_analysis: list of {{fiscal_year, income, expenditure, balance, source_detail}}
- legal_provisions: list of {{act, section, description, penalty}}
- key_events: list of {{date, description}}
- total_disputed_amount: string or null

CHARGE SHEET TEXT:
{charge_sheet_text}

IMPORTANT: Return ONLY a valid JSON object."""

FORMATTING_SYSTEM_PROMPT = """You are a Nepali legal writer for JAWAFDEHI, Nepal's public corruption case archive.
Format structured CIAA case data into a case overview in Nepali Markdown.

Rules:
- Write entirely in Nepali Devanagari; English only for proper nouns/citation numbers.
- Transcribe and format; do not summarize away specific details.
- Use Markdown bold headings, NOT HTML.
- Use Markdown pipe tables for fiscal data.
- Return valid JSON: {"short_description": "...", "description": "..."}
- No placeholder text."""

FORMATTING_USER_PROMPT = """Format this extracted case data into a JAWAFDEHI case overview.

CASE DATA:
{extracted_json}

Sections:

**क) अभियोगदावीको सार**
- Mandatory.
- 4-6 paragraph narrative from charge sheet data.
- Include accused persons, positions, institutions, alleged scheme, key dates, complaints, investigation period.
- Include fiscal analysis table with ALL fiscal year rows if present.
- Use Markdown pipe table: | आर्थिक वर्ष | आय विवरण | आय (रु.) | व्यय विवरण | व्यय (रु.) | बचत/अपुग |

**ख) आकर्षित कानुनी व्यवस्था**
- Only if legal_provisions exist.
- Explain each provision in plain Nepali.

**ग) प्रमाणको संक्षेप**
- Only if key_events or evidence facts exist.
- List evidence items and significance.

Rules:
- short_description: 2-3 sentence plain Nepali summary; no markdown.
- description: Full Markdown.
- Preserve all specific details exactly.
- Return ONLY valid JSON."""


def _validate_host_safety(hostname: str) -> None:
    host = hostname.lower().rstrip(".")
    if host in _SSRF_BLOCKED_HOSTNAMES:
        raise ValueError(f"Blocked internal host: {hostname!r}")
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host: {hostname!r}") from exc
    for info in addrinfo:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            raise ValueError(f"Blocked internal address: {hostname!r} -> {addr}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise urllib.error.HTTPError(req.full_url, code, f"Unsafe redirect to {newurl}", headers, fp)
        _validate_host_safety(parsed.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sanitize_download_filename(filename: str | None, source_id: str) -> str:
    raw = (filename or "").strip()
    if not raw:
        return f"{source_id}.bin"
    candidate = Path(urllib.parse.unquote(raw)).name.strip().replace("\x00", "")
    candidate = re.sub(r"[<>:\"/\\|?*]+", "_", candidate).rstrip(" .")
    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"
    if len(candidate) <= 200:
        return candidate
    suffix = "".join(Path(candidate).suffixes)
    stem = candidate[: -len(suffix)] if suffix else candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:10]
    budget = max(1, 200 - len(suffix) - len(digest) - 1)
    return f"{stem[:budget].rstrip(' .-_') or source_id}-{digest}{suffix}"[:200]


def _confined_output_path(output_dir: Path, filename: str) -> Path:
    output_dir_resolved = output_dir.resolve()
    out_path = (output_dir / filename).resolve()
    if output_dir_resolved not in out_path.parents:
        raise CommandError(f"Refusing to write outside output directory: {filename!r}")
    return out_path


def _copy_stream_to_path_with_limit(in_file: Any, out_path: Path) -> None:
    total = 0
    try:
        with out_path.open("wb") as out_file:
            while True:
                chunk = in_file.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise CommandError(f"Downloaded source exceeds max size of {MAX_DOWNLOAD_BYTES} bytes")
                out_file.write(chunk)
    except (OSError, CommandError):
        out_path.unlink(missing_ok=True)
        raise


def normalize_model(model: str) -> str:
    model = model.strip()
    for prefix in ("opencode-go/", "openai:"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def normalize_base_url(url: str | None) -> str:
    url = (url or os.environ.get("JAWAFDEHI_LLM_PROXY_URL", DEFAULT_OPENCODE_BASE)).strip().rstrip("/")
    if url.endswith("/zen/v1"):
        return url.replace("/zen/v1", "/zen/go/v1")
    if url.endswith("/zen/go"):
        return f"{url}/v1"
    return url


def resolve_api_key(cli_key: str | None = None, is_anthropic: bool = False) -> str:
    if cli_key and cli_key.strip():
        return cli_key.strip()
    env_vars = ("ANTHROPIC_API_KEY", "JAWAFDEHI_LLM_API_KEY", "OPENCODE_API_KEY") if is_anthropic else ("JAWAFDEHI_LLM_API_KEY", "OPENCODE_API_KEY", "ANTHROPIC_API_KEY")
    for env_var in env_vars:
        val = os.environ.get(env_var)
        if val:
            return val
    raise CommandError("No API key provided. Set --llm-api-key, JAWAFDEHI_LLM_API_KEY, OPENCODE_API_KEY, or ANTHROPIC_API_KEY.")


def _llm_endpoint(base_url: str, model: str) -> str:
    return f"{base_url.rstrip('/')}/{'messages' if normalize_model(model) in MINIMAX_MODELS else 'chat/completions'}"


def _llm_timeout(cli_timeout: int | None = None) -> int:
    if cli_timeout is not None:
        if cli_timeout <= 0:
            raise CommandError(f"--llm-timeout must be > 0, got {cli_timeout}")
        return cli_timeout
    env = os.environ.get("JAWAFDEHI_LLM_TIMEOUT_SECONDS")
    if env:
        try:
            val = int(env)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_LLM_TIMEOUT


def _extract_json_body(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        for i in range(1, len(lines)):
            if lines[i].strip().startswith("```"):
                inner = "\n".join(lines[1:i]).strip()
                return re.sub(r"^json\s*", "", inner, flags=re.IGNORECASE).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        return raw[start:end + 1]
    return raw


class Command(BaseCommand):
    help = "Generate Markdown case overview content from CIAA charge sheets using LLM"

    def __init__(self):
        super().__init__()
        self.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_failed": 0,
            "cases_no_content": 0,
            "llm_extraction_failures": 0,
            "llm_formatting_failures": 0,
        }

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview without saving to database")
        parser.add_argument("--limit", type=int, default=None, help="Process only N cases")
        parser.add_argument("--case-id", type=str, default=None, help="Process a specific case by case_id")
        parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
        parser.add_argument("--force", action="store_true", help="Re-process cases with existing overview content")
        parser.add_argument("--llm-model", type=str, default=os.environ.get("JAWAFDEHI_ALLEGATION_MODEL", "claude-sonnet-4-5"), help="LLM model")
        parser.add_argument("--llm-base-url", type=str, default=None, help="LLM base URL")
        parser.add_argument("--llm-api-key", type=str, default=None, help="LLM API key")
        parser.add_argument("--llm-timeout", type=int, default=None, help="LLM timeout seconds")

    def handle(self, *args, **options):
        if options["verbose"]:
            logger.setLevel(logging.DEBUG)
        model = normalize_model(options["llm_model"])
        base_url = normalize_base_url(options["llm_base_url"])
        is_opencode = "anthropic.com" not in base_url
        api_key = resolve_api_key(options.get("llm_api_key"), is_anthropic=not is_opencode)
        timeout = _llm_timeout(options.get("llm_timeout"))
        dry_run = options["dry_run"]

        self.stdout.write(self.style.WARNING(f"{'[DRY RUN] ' if dry_run else ''}Starting case overview enrichment..."))
        started = time.time()
        cases = self._get_eligible_cases(options["limit"], options["force"], options.get("case_id"))
        self.stdout.write(f"Found {len(cases)} eligible CIAA DRAFT case(s) to process")
        self._fetch_source_cache(cases)

        for idx, case in enumerate(cases, 1):
            self.stdout.write(f"\n[{idx}/{len(cases)}] {case.case_id} - {case.title[:80]}...")
            try:
                self._process_case(case, model, base_url, api_key, timeout, is_opencode, dry_run)
            except Exception as exc:
                self.stats["cases_failed"] += 1
                logger.exception("Error processing %s", case.case_id)
                self.stdout.write(self.style.ERROR(f"FAILED: {case.case_id} - {exc}"))

        elapsed = int(time.time() - started)
        self._print_summary(dry_run, f"{elapsed // 60}m {elapsed % 60}s" if elapsed >= 60 else f"{elapsed}s")

    def _get_eligible_cases(self, limit, force, case_id):
        queryset = Case.objects.filter(state=CaseState.DRAFT)
        if case_id:
            queryset = queryset.filter(case_id=case_id)
        if not force:
            queryset = queryset.filter(Q(short_description__isnull=True) | Q(short_description=""))
        if limit is not None:
            if limit < 0:
                raise CommandError(f"--limit must be >= 0, got {limit}")
            queryset = queryset[:limit] if limit > 0 else queryset.none()
        return list(queryset)

    def _fetch_source_cache(self, cases):
        source_ids = set()
        for case in cases:
            for entry in case.evidence or []:
                if isinstance(entry, dict) and isinstance(entry.get("source_id"), str):
                    source_ids.add(entry["source_id"])
        self._source_lookup = {
            source.source_id: source
            for source in DocumentSource.objects.filter(source_id__in=source_ids, is_deleted=False).prefetch_related("uploaded_files")
        }

    def _process_case(self, case, model, base_url, api_key, timeout, is_opencode, dry_run):
        self.stats["cases_processed"] += 1
        if not case.evidence:
            self.stats["cases_skipped"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No evidence"))
            return

        source = self._select_charge_sheet_source(case)
        if not source:
            self._skip_no_content(case, "No charge sheet source found", dry_run)
            return
        self.stdout.write(f"  Source: {self._describe_source(source)}")

        try:
            charge_sheet_text = self._convert_source_to_markdown(source)
        except (CommandError, ValueError, OSError) as exc:
            self._skip_no_content(case, f"Failed to convert source: {exc}", dry_run)
            return
        if len(charge_sheet_text.strip()) < 50:
            self._skip_no_content(case, "Converted charge sheet content is too short", dry_run)
            return

        extracted_json = self._extract_structured_data(case, charge_sheet_text, model, base_url, api_key, timeout, is_opencode)
        if extracted_json is None:
            return
        overview = self._format_overview(extracted_json, model, base_url, api_key, timeout, is_opencode)
        if overview is None:
            return

        short_description = (overview.get("short_description") or "").strip()
        description = (overview.get("description") or "").strip()
        valid, issues = self._validate_overview(short_description, description)
        for issue in issues:
            self.stdout.write(self.style.WARNING(f"  Quality issue: {issue}"))
        if not valid:
            self.stats["cases_failed"] += 1
            self.stdout.write(self.style.ERROR("  FAILED: Overview failed required quality gates"))
            return

        if dry_run:
            self.stats["cases_enriched"] += 1
            self.stdout.write(self.style.SUCCESS(f"  [DRY RUN] Would save overview ({len(description)} chars)"))
            return
        case.short_description = short_description
        case.description = description
        case.save(update_fields=["short_description", "description", "updated_at"])
        self.stats["cases_enriched"] += 1
        self.stdout.write(self.style.SUCCESS(f"  ENRICHED: Saved overview ({len(description)} chars)"))

    def _extract_structured_data(self, case, text, model, base_url, api_key, timeout, is_opencode):
        bigo = f"रू {case.bigo:,}" if case.bigo else "उल्लेख छैन"
        prompt = EXTRACTION_USER_PROMPT.format(
            case_id=case.case_id,
            case_title=case.title,
            court_cases=json.dumps(case.court_cases, ensure_ascii=False) if case.court_cases else "None",
            bigo=bigo,
            charge_sheet_text=text[:60000],
        )
        self.stdout.write(f"  [1/2] Extracting structured data ({len(text)} chars)...")
        raw = self._call_llm(model, base_url, api_key, timeout, is_opencode, EXTRACTION_SYSTEM_PROMPT, prompt)
        try:
            parsed = json.loads(raw or "")
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            self.stats["llm_extraction_failures"] += 1
            self.stats["cases_failed"] += 1
            self.stdout.write(self.style.ERROR("  FAILED: Extraction returned invalid JSON"))
            return None
        return parsed

    def _format_overview(self, extracted_json, model, base_url, api_key, timeout, is_opencode):
        prompt = FORMATTING_USER_PROMPT.format(extracted_json=json.dumps(extracted_json, ensure_ascii=False, indent=2))
        self.stdout.write("  [2/2] Formatting Markdown overview...")
        raw = self._call_llm(model, base_url, api_key, timeout, is_opencode, FORMATTING_SYSTEM_PROMPT, prompt)
        try:
            parsed = json.loads(raw or "")
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            self.stats["llm_formatting_failures"] += 1
            self.stats["cases_failed"] += 1
            self.stdout.write(self.style.ERROR("  FAILED: Formatting returned invalid JSON"))
            return None
        return parsed

    def _call_llm(self, model, base_url, api_key, timeout, is_opencode, system_prompt, prompt):
        if is_opencode:
            return self._call_llm_opencode(model, base_url, api_key, timeout, system_prompt, prompt)
        return self._call_llm_anthropic(model, base_url, api_key, timeout, system_prompt, prompt)

    def _call_llm_opencode(self, model, base_url, api_key, timeout, system_prompt, prompt):
        endpoint = _llm_endpoint(base_url, model)
        normalized_model = normalize_model(model)
        is_minimax = normalized_model in MINIMAX_MODELS
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "JawafdehiAPI/1.0 enrich_case_overview"}
        if is_minimax:
            body = {"model": normalized_model, "max_tokens": 6000, "system": system_prompt, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
        else:
            body = {"model": normalized_model, "max_tokens": 6000, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}], "temperature": 0.1}
        data = json.dumps(body).encode("utf-8")
        for attempt in range(1, MAX_LLM_RETRIES + 1):
            try:
                req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                raw = payload.get("content", [{}])[0].get("text", "") if is_minimax else payload.get("choices", [{}])[0].get("message", {}).get("content", "")
                return _extract_json_body(raw)
            except urllib.error.HTTPError as exc:
                if attempt < MAX_LLM_RETRIES and exc.code in (429, 503):
                    wait = 2 ** attempt
                    self.stdout.write(self.style.WARNING(f"  LLM {exc.code} on attempt {attempt}, retrying in {wait}s..."))
                    time.sleep(wait)
                    continue
                body = exc.read().decode("utf-8", errors="replace")[:500]
                raise CommandError(f"LLM HTTP {exc.code}: {body[:300]}") from exc
            except OSError as exc:
                if attempt < MAX_LLM_RETRIES:
                    wait = 2 ** attempt
                    self.stdout.write(self.style.WARNING(f"  LLM connection error on attempt {attempt}, retrying in {wait}s..."))
                    time.sleep(wait)
                    continue
                raise CommandError(f"LLM connection failed after {MAX_LLM_RETRIES} attempts: {exc}") from exc
        return None

    def _call_llm_anthropic(self, model, base_url, api_key, timeout, system_prompt, prompt):
        try:
            import anthropic
        except ImportError as exc:
            raise CommandError("anthropic package is required for direct Anthropic API calls") from exc
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url or None)
        for attempt in range(MAX_LLM_RETRIES):
            try:
                response = client.messages.create(model=model, max_tokens=6000, system=system_prompt, messages=[{"role": "user", "content": prompt}], temperature=0.1, timeout=timeout)
                return _extract_json_body(response.content[0].text)
            except Exception as exc:
                if attempt < MAX_LLM_RETRIES - 1:
                    wait = 2 ** attempt
                    self.stdout.write(self.style.WARNING(f"  Retrying in {wait}s..."))
                    time.sleep(wait)
                    continue
                raise CommandError(f"LLM call failed after {MAX_LLM_RETRIES} attempts: {exc}") from exc

    def _select_charge_sheet_source(self, case):
        sources = [
            source for source in (self._source_lookup.get(entry.get("source_id")) for entry in case.evidence or [] if isinstance(entry, dict))
            if source is not None
        ]
        if not sources:
            return None
        best_score, best = max(((self._score_source_for_charge_sheet(source), source) for source in sources), key=lambda item: item[0])
        return best if best_score > 0 else None

    def _score_source_for_charge_sheet(self, source):
        upload_names = [upload.filename or Path(upload.file.name).name for upload in source.uploaded_files.all()]
        url_text = " ".join(url for url in (source.url or []) if isinstance(url, str))
        corpus = " ".join([source.title or "", source.description or "", source.uploaded_filename or "", url_text, " ".join(upload_names)]).lower()
        score = 0
        if source.source_type == SourceType.OFFICIAL_GOVERNMENT:
            score += 10
        if any(keyword in corpus for keyword in ["charge sheet", "charge-sheet", "chargesheet", "अभियोगपत्र", "अभियोगदावी", "अभियोग पत्र", "अभियोग दावी"]):
            score += 10
        if any(keyword in corpus for keyword in ["ciaa", "अख्तियार"]):
            score += 5
        if source.uploaded_file or source.uploaded_files.exists():
            score += 3
        if source.url and any(url.strip() for url in source.url if isinstance(url, str)):
            score += 2
        if source.description and len(source.description.strip()) >= 1000:
            score += 1
        return score

    def _convert_source_to_markdown(self, source):
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise CommandError("markitdown is required for case overview enrichment") from exc
        converter = MarkItDown(enable_plugins=True)
        with tempfile.TemporaryDirectory(prefix="overview-enrichment-") as tmp_dir:
            output_dir = Path(tmp_dir)
            temp_path = self._download_source_to_path(source, output_dir)
            if temp_path:
                result = converter.convert_uri(temp_path.resolve().as_uri())
                if result.text_content and len(result.text_content.strip()) >= 50:
                    return result.text_content
            last_error = None
            for url in self._ranked_source_urls(source):
                try:
                    temp_path = self._download_url_to_path(url, source.source_id, output_dir)
                    if temp_path:
                        result = converter.convert_uri(temp_path.resolve().as_uri())
                        if result.text_content and len(result.text_content.strip()) >= 50:
                            return result.text_content
                    last_error = f"insufficient content from {url}"
                except (OSError, ValueError) as exc:
                    last_error = f"{url}: {exc}"
            if source.description and len(source.description.strip()) >= 500:
                return source.description
            raise CommandError(f"Unable to convert source {source.source_id}: {last_error or 'no content'}")

    def _download_source_to_path(self, source, output_dir):
        if source.uploaded_file:
            filename = _sanitize_download_filename(source.uploaded_filename or source.uploaded_file.name, source.source_id)
            out_path = _confined_output_path(output_dir, filename)
            with source.uploaded_file.open("rb") as in_file:
                _copy_stream_to_path_with_limit(in_file, out_path)
            return out_path
        uploaded = source.uploaded_files.first()
        if uploaded and uploaded.file:
            filename = _sanitize_download_filename(uploaded.filename or uploaded.file.name, source.source_id)
            out_path = _confined_output_path(output_dir, filename)
            with uploaded.file.open("rb") as in_file:
                _copy_stream_to_path_with_limit(in_file, out_path)
            return out_path
        return None

    def _download_url_to_path(self, url, source_id, output_dir):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid URL {url!r}")
        _validate_host_safety(parsed.hostname)
        out_path = _confined_output_path(output_dir, _sanitize_download_filename(parsed.path, source_id))
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"})
            with urllib.request.build_opener(_SafeRedirectHandler()).open(request, timeout=30) as response:
                _copy_stream_to_path_with_limit(response, out_path)
            return out_path
        except OSError:
            out_path.unlink(missing_ok=True)
            return None

    def _ranked_source_urls(self, source):
        urls = [url.strip() for url in (source.url or []) if isinstance(url, str) and url.strip()]
        direct = [url for url in urls if self._is_direct_document_url(url)]
        other = [url for url in urls if url not in direct]
        direct.sort(key=self._source_url_priority, reverse=True)
        return direct + other

    def _is_direct_document_url(self, url):
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path).lower()
        return path.endswith((".pdf", ".doc", ".docx"))

    def _source_url_priority(self, url):
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path).lower()
        return (int(parsed.netloc.lower() == "ngm-store.jawafdehi.org"), int(path.endswith(".pdf")), int(path.endswith((".pdf", ".doc", ".docx"))))

    def _validate_overview(self, short_description, description):
        issues = []
        valid = True
        if "क) अभियोगदावीको सार" not in description:
            issues.append("Missing required section: क) अभियोगदावीको सार")
            valid = False
        if not description or len(description) < 100:
            issues.append("Description too short")
            valid = False
        if not short_description or len(short_description) < 50:
            issues.append("short_description too short")
        if len(short_description) > 1000:
            issues.append("short_description too long")
            valid = False
        if re.search(r"<\s*(h[1-6]|table|tr|td|th|div|p|span|br|ul|ol|li|a)\b", description):
            issues.append("Raw HTML tags found")
        non_ws = re.sub(r"\s+", "", description)
        if non_ws and len(DEVANAGARI_RE.findall(non_ws)) / len(non_ws) < 0.80:
            issues.append("Devanagari ratio below 80%")
        if any(token.lower() in description.lower() for token in ["[insert]", "[tbd]", "[todo]"]):
            issues.append("Placeholder text found")
            valid = False
        return valid, issues

    def _skip_no_content(self, case, note, dry_run):
        self.stats["cases_no_content"] += 1
        if not dry_run:
            self._record_missing_details(case, f"enrich_case_overview: {note}")
        self.stdout.write(self.style.WARNING(f"  SKIPPED: {note}"))

    def _record_missing_details(self, case, note):
        current = case.missing_details or ""
        if note not in current:
            case.missing_details = f"{current}\n{note}" if current else note
            case.save(update_fields=["missing_details"])

    def _describe_source(self, source):
        if source.uploaded_file:
            return f"uploaded file: {source.uploaded_filename or source.uploaded_file.name} ({source.source_id})"
        uploaded = source.uploaded_files.first()
        if uploaded and uploaded.file:
            return f"uploaded file: {uploaded.filename or uploaded.file.name} ({source.source_id})"
        urls = self._ranked_source_urls(source)
        if urls:
            parsed = urllib.parse.urlsplit(urls[0])
            return f"URL: {urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))} ({source.source_id})"
        if source.description and len(source.description.strip()) >= 500:
            return f"description fallback ({source.source_id})"
        return f"{source.source_id} (no content)"

    def _print_summary(self, dry_run, elapsed_str):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.WARNING(f"{'[DRY RUN] ' if dry_run else ''}SUMMARY"))
        self.stdout.write("=" * 60)
        self.stdout.write(f"Total time:              {elapsed_str}")
        self.stdout.write(f"Cases processed:         {self.stats['cases_processed']}")
        self.stdout.write(self.style.SUCCESS(f"Cases enriched:          {self.stats['cases_enriched']}"))
        self.stdout.write(self.style.WARNING(f"Cases skipped:           {self.stats['cases_skipped']}"))
        self.stdout.write(self.style.WARNING(f"Cases no content:        {self.stats['cases_no_content']}"))
        if self.stats["cases_failed"]:
            self.stdout.write(self.style.ERROR(f"Cases failed:            {self.stats['cases_failed']}"))
        if self.stats["llm_extraction_failures"]:
            self.stdout.write(self.style.WARNING(f"LLM extraction failures: {self.stats['llm_extraction_failures']}"))
        if self.stats["llm_formatting_failures"]:
            self.stdout.write(self.style.WARNING(f"LLM formatting failures: {self.stats['llm_formatting_failures']}"))
        self.stdout.write("=" * 60)
        if dry_run:
            self.stdout.write(self.style.WARNING("\nThis was a dry run. No changes were made to the database."))
