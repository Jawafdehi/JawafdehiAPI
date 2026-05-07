"""Enrich missing BIGO values for DRAFT cases using press releases + LLM extraction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from cases.models import Case, CaseState, DocumentSource, SourceType
from cases.services.priority_case_loader import filter_by_priority, load_priority_cases

MAX_LIMIT = 1000
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 16 * 1024
BIGO_CONTEXT_KEYWORDS = (
    "बिगो",
    "मागदाबी",
    "हानि",
    "हानी",
    "नोक्सानी",
    "क्षति",
    "damage claim",
    "loss amount",
    "corruption loss",
)

_NEPALI_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


class Command(BaseCommand):
    help = (
        "Find DRAFT cases with missing BIGO, extract amount from CIAA press release "
        "content, and PATCH BIGO via API."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help=f"Max cases to process (1-{MAX_LIMIT}).",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            default=None,
            help="Optional exact case_id to process.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview enrichment results without PATCHing cases.",
        )
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Required when DEBUG=False to run this command in production.",
        )
        parser.add_argument(
            "--api-base-url",
            type=str,
            default=os.getenv("JAWAFDEHI_API_BASE_URL", "http://127.0.0.1:8000"),
            help="Jawafdehi API base URL (root or /api).",
        )
        parser.add_argument(
            "--api-token",
            type=str,
            default=os.getenv("JAWAFDEHI_API_TOKEN"),
            help="Jawafdehi API token. Defaults to JAWAFDEHI_API_TOKEN.",
        )
        parser.add_argument(
            "--anthropic-api-key",
            type=str,
            default=os.getenv("ANTHROPIC_API_KEY"),
            help="Anthropic API key (or LLM proxy API key if using --llm-base-url). Defaults to ANTHROPIC_API_KEY.",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default=os.getenv("BIGO_ENRICHMENT_MODEL", "claude-sonnet-4-5"),
            help="LLM model used for BIGO extraction.",
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=os.getenv("JAWAFDEHI_CASEWORK_BASE_URL"),
            help="LLM API base URL (for OpenAI-compatible proxy). Defaults to JAWAFDEHI_CASEWORK_BASE_URL.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable detailed per-case logging for enrichment flow.",
        )
        parser.add_argument(
            "--min-confidence",
            choices=["high", "medium", "low"],
            default="medium",
            help="Minimum accepted extraction confidence.",
        )
        parser.add_argument(
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list.",
        )

    def handle(self, *args, **options):
        self._verbose = bool(options.get("verbose"))
        self._validate_guardrails(options)
        self._validate_runtime_inputs(options)
        self._log_info(
            "Starting BIGO enrichment run "
            f"(limit={options['limit']}, dry_run={options['dry_run']}, "
            f"case_id={options['case_id'] or 'ALL'}, min_confidence={options['min_confidence']})",
            always=True,
        )

        priority = options["priority"]
        case_id = options.get("case_id")
        if priority and case_id:
            self.stderr.write(
                self.style.ERROR("--priority and --case-id are mutually exclusive")
            )
            return

        queryset = (
            Case.objects.filter(state=CaseState.DRAFT)
            .filter(Q(bigo__isnull=True) | Q(bigo=0))
            .order_by("-created_at")
        )
        if case_id:
            queryset = queryset.filter(case_id=case_id)
        elif priority:
            priority_list = load_priority_cases()
            queryset = filter_by_priority(queryset, priority_list)

        cases = list(queryset[: options["limit"]])
        if not cases:
            self.stdout.write("No eligible DRAFT case found for BIGO enrichment.")
            return

        updated = 0
        skipped = 0
        failed = 0

        for case in cases:
            try:
                self._log_info(f"Processing case {case.case_id}")
                source = self._select_press_release_source(case)
                if source is None:
                    skipped += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"[SKIP] {case.case_id}: no press release source found."
                        )
                    )
                    continue

                self._log_info(
                    f"Selected source {source.source_id} for case {case.case_id}"
                )
                markdown = self._convert_source_to_markdown(source)
                self._log_info(
                    f"Converted source {source.source_id} to markdown ({len(markdown)} chars)"
                )
                bigo = self._extract_bigo_from_markdown(
                    markdown=markdown,
                    case=case,
                    model=options["llm_model"],
                    anthropic_api_key=options["anthropic_api_key"],
                    min_confidence=options["min_confidence"],
                    llm_base_url=options.get("llm_base_url"),
                )
                self._log_info(f"Extracted BIGO candidate for {case.case_id}: {bigo}")
                if bigo is None:
                    skipped += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"[SKIP] {case.case_id}: could not extract a reliable BIGO."
                        )
                    )
                    continue

                if options["dry_run"]:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[DRY-RUN] {case.case_id}: would PATCH BIGO={bigo}"
                        )
                    )
                else:
                    self._patch_case_bigo(
                        case=case,
                        bigo=bigo,
                        api_base_url=options["api_base_url"],
                        api_token=options["api_token"],
                    )
                    self.stdout.write(
                        self.style.SUCCESS(f"[UPDATED] {case.case_id}: BIGO={bigo}")
                    )
                updated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"[FAIL] {case.case_id}: {type(exc).__name__}: {exc}"
                    )
                )

        self.stdout.write(
            f"Processed={len(cases)} Updated={updated} Skipped={skipped} Failed={failed}"
        )

    def _log_info(self, message: str, *, always: bool = False) -> None:
        if always or getattr(self, "_verbose", False):
            self.stdout.write(f"[INFO] {message}")

    def _validate_guardrails(self, options: dict[str, Any]) -> None:
        limit = options["limit"]
        if limit < 1 or limit > MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}.")

        if not settings.DEBUG and not options["allow_production"]:
            raise CommandError(
                "This command refuses to run in production unless --allow-production is provided."
            )

    def _validate_runtime_inputs(self, options: dict[str, Any]) -> None:
        if options["dry_run"]:
            return

        if not options["api_token"]:
            raise CommandError(
                "JAWAFDEHI API token is required. Set --api-token or JAWAFDEHI_API_TOKEN."
            )
        if not options["anthropic_api_key"]:
            raise CommandError(
                "Anthropic API key is required. Set --anthropic-api-key or ANTHROPIC_API_KEY."
            )

    def _select_press_release_source(self, case: Case) -> DocumentSource | None:
        source_ids = [
            item["source_id"]
            for item in (case.evidence or [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
        if not source_ids:
            return None

        sources = list(
            DocumentSource.objects.filter(
                source_id__in=source_ids,
                is_deleted=False,
            ).prefetch_related("uploaded_files")
        )
        if not sources:
            return None

        ranked = sorted(
            (
                (self._score_source_for_press_release(source), source)
                for source in sources
            ),
            key=lambda row: row[0],
            reverse=True,
        )
        best_score, best_source = ranked[0]
        
        # If we have a positive score, return the best match
        if best_score > 0:
            return best_source
        
        # Fallback: if there's only one source and it has a PDF, use it
        # This handles cases where the source isn't properly labeled
        if len(sources) == 1:
            source = sources[0]
            # Check if it has an uploaded PDF file
            has_pdf = (
                source.uploaded_file and source.uploaded_file.name.lower().endswith('.pdf')
            ) or any(
                f.file.name.lower().endswith('.pdf') 
                for f in source.uploaded_files.all()
            )
            if has_pdf:
                self._log_info(
                    f"Using single PDF source {source.source_id} for {case.case_id} "
                    "(no press release keywords found, but only one source available)"
                )
                return source
        
        return None

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
        except ImportError as exc:  # pragma: no cover - env dependent
            raise CommandError(
                "markitdown is required for BIGO enrichment conversion. "
                "Install conversion dependencies (markitdown + likhit plugin)."
            ) from exc

        converter = MarkItDown(enable_plugins=True)
        with tempfile.TemporaryDirectory(prefix="bigo-enrichment-") as tmp_dir:
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
            filename = self._sanitize_download_filename(
                source.uploaded_filename or source.uploaded_file.name,
                source.source_id,
            )
            out_path = self._confined_output_path(output_dir, filename)
            with source.uploaded_file.open("rb") as in_file:
                self._copy_stream_to_path_with_limit(in_file, out_path)
            return out_path

        uploaded = source.uploaded_files.first()
        if uploaded and uploaded.file:
            filename = self._sanitize_download_filename(
                uploaded.filename or uploaded.file.name,
                source.source_id,
            )
            out_path = self._confined_output_path(output_dir, filename)
            with uploaded.file.open("rb") as in_file:
                self._copy_stream_to_path_with_limit(in_file, out_path)
            return out_path

        source_url = self._pick_source_url(source)
        if not source_url:
            return None

        source_url = self._validate_url_scheme(source_url)
        parsed = urllib.parse.urlparse(source_url)
        guessed_name = self._sanitize_download_filename(parsed.path, source.source_id)
        out_path = self._confined_output_path(output_dir, guessed_name)
        try:
            request = urllib.request.Request(
                source_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                self._copy_stream_to_path_with_limit(response, out_path)
            return out_path
        except (urllib.error.URLError, OSError):
            out_path.unlink(missing_ok=True)
            return None
        except CommandError:
            out_path.unlink(missing_ok=True)
            raise

    def _copy_stream_to_path_with_limit(self, in_file: Any, out_path: Path) -> None:
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

    def _sanitize_download_filename(self, filename: str | None, source_id: str) -> str:
        raw = (filename or "").strip()
        if not raw:
            return f"{source_id}.bin"

        # URL paths often contain percent-encoded Unicode; decoding keeps filenames readable
        # and avoids hitting filesystem filename-length limits (e.g., 255 chars on macOS).
        decoded = urllib.parse.unquote(raw)
        candidate = Path(decoded).name.strip()

        if candidate in {"", ".", ".."}:
            return f"{source_id}.bin"

        # Basic cross-platform safety: remove NUL and replace Windows-forbidden characters.
        candidate = candidate.replace("\x00", "")
        candidate = re.sub(r"[<>:\"/\\|?*]+", "_", candidate).rstrip(" .")
        if candidate in {"", ".", ".."}:
            return f"{source_id}.bin"

        # Keep well under typical per-filename limits (255) and leave room for temp dir paths.
        max_len = 200
        if len(candidate) <= max_len:
            return candidate

        suffix = "".join(Path(candidate).suffixes)
        stem = candidate[: -len(suffix)] if suffix else candidate
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]

        stem_budget = max_len - len(suffix) - len(digest) - 1  # for '-'
        if stem_budget < 1:
            # Worst-case fallback: ensure a short, unique name.
            return f"{source_id}-{digest}{suffix}"[:max_len]

        truncated_stem = stem[:stem_budget].rstrip(" .-_")
        if not truncated_stem:
            truncated_stem = source_id

        return f"{truncated_stem}-{digest}{suffix}"

    def _confined_output_path(self, output_dir: Path, filename: str) -> Path:
        output_dir_resolved = output_dir.resolve()
        out_path = (output_dir / filename).resolve()
        if output_dir_resolved not in out_path.parents:
            raise CommandError(
                f"Refusing to write outside output directory: '{filename}'"
            )
        return out_path

    def _extract_bigo_from_markdown(
        self,
        markdown: str,
        case: Case,
        model: str,
        anthropic_api_key: str,
        min_confidence: str,
        llm_base_url: str | None = None,
    ) -> int | None:
        prompt = self._build_bigo_prompt(markdown=markdown, case=case)

        # Use OpenAI-compatible client if base_url provided (Jawafdehi proxy)
        if llm_base_url:
            from openai import OpenAI, AuthenticationError

            try:
                client = OpenAI(api_key=anthropic_api_key, base_url=llm_base_url)
                # Strip "openai:" prefix if present in model name
                model_name = model.replace("openai:", "") if "openai:" in model else model
                response = client.chat.completions.create(
                    model=model_name,
                    max_tokens=500,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = self._openai_response_text(response)
            except AuthenticationError as exc:
                raise CommandError(
                    f"LLM proxy authentication failed at {llm_base_url}. "
                    f"The API key provided via --anthropic-api-key is invalid for this proxy. "
                    f"Original error: {exc}"
                ) from exc
        else:
            # Use native Anthropic client
            import anthropic

            client = anthropic.Anthropic(api_key=anthropic_api_key)
            response = client.messages.create(
                model=model,
                max_tokens=500,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text
                for block in response.content
                if getattr(block, "type", "") == "text"
            )

        payload = self._parse_json_response(text)
        confidence = str(payload.get("confidence", "")).strip().lower()
        if self._confidence_rank(confidence) < self._confidence_rank(min_confidence):
            return None

        evidence_quote = payload.get("evidence_quote")
        if not self._is_explicit_bigo_context(evidence_quote):
            self._log_info(
                f"Rejected extraction for {case.case_id}: evidence_quote lacked explicit BIGO context."
            )
            return None

        return self._coerce_bigo_int(payload.get("bigo"))

    def _openai_response_text(self, response: Any) -> str:
        if not response:
            raise CommandError("OpenAI-compatible LLM response was empty.")

        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise CommandError(
                "OpenAI-compatible LLM response missing choices content."
            )

        first_choice = choices[0]
        message = getattr(first_choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None

        if isinstance(content, str):
            text = content.strip()
            if text:
                return text

        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                    continue
                block_text = getattr(block, "text", None)
                if isinstance(block_text, str):
                    parts.append(block_text)
            text = "".join(parts).strip()
            if text:
                return text

        raise CommandError("OpenAI-compatible LLM response missing text content.")

    def _build_bigo_prompt(self, markdown: str, case: Case) -> str:
        return f"""You extract BIGO (बिगो) amount from CIAA press release content.

Return STRICT JSON only with this schema:
{{
  "bigo": <integer or null>,
  "confidence": "high" | "medium" | "low",
  "evidence_quote": "<short quote from text that supports the amount>",
  "press_release_type": "sting_operation" | "appeal_review" | "charge_filing" | "other"
}}

CRITICAL RULES (apply in order):

Rule 1 — Output format
BIGO must be an NPR integer only. No commas, no currency symbols (रू/Rs/NPR), no paisa suffix (/90, /39, etc.), no floats.
If the extracted amount has a paisa portion (e.g. १,४६,८१,२२५/९०), strip everything after / before returning.

Rule 2 — Numeral normalization
Before any matching, normalize Devanagari digits to Arabic (०→0, १→1, ... ९→9). CIAA PDFs mix both in the same number.
Then strip commas. Then strip the paisa suffix. Then parse as integer.

Rule 3 — Type-routing first (CHECK THIS BEFORE READING TEXT)
Before reading any text, determine the press release type:
- Sting Operation (रंगेहात, sting, caught red-handed) → return null (high confidence). The amounts in sting releases are physical cash caught during arrest — bribe/unexplained cash — not a formally established bigo.
- Appeal/Review (पुनरावेदन, अपील, appeal, review) → return null (high confidence). These record CIAA appealing a court verdict; bigo was defined at charge-sheet stage and is not re-stated here.
- Charge Filing (अभियोग दायर, charge filed, मुद्दा दर्ता) → proceed to extraction rules below.
- Other → proceed to extraction rules below.

Rule 4 — Null with low confidence
If no reliable bigo signal exists after all checks, return null with low confidence. Do not guess.

Rule 5 — No ranges, floats, or formatted strings
Never return ranges (१-५ लाख), floats (1.5 करोड), or formatted strings. Integer only.

Rule 6 — Priority signal hierarchy (apply in order, stop at first match)
1. Title contains "बिगो रू.[AMOUNT] कायम गरी" → extract AMOUNT → high confidence
2. PDF table row under "बिगो रू." column header → extract amount → high confidence
3. PDF body sentence "कूल आय भन्दा कूल व्यय ... [AMOUNT] ले बढी" (excess of expenditure over income) → extract AMOUNT → medium confidence, verify it matches signal 2
4. No match → null

Note: "बिगो बमोजिम जरिवाना" is a reference to an already-stated bigo, not a declaration of it — use it only to confirm, not as a primary source.

Rule 7 — Multiple amounts: label is mandatory
When multiple monetary amounts appear in the text, extract only the one explicitly labeled as bigo using the hierarchy in Rule 6.
If multiple amounts are present and none carries a clear bigo label, return null — do not pick the largest, the first, or the one that "seems right."

Rule 8 — Ignore list (NEVER extract these as bigo)
Amount type | Nepali marker
------------|---------------
Bribe received/demanded | घुस/रिसवत रकम रू.
Unexplained cash seized | स्रोत नखुलेको रकम रू.
Lawful income subtotal | जम्मा/कूल आय रू.
Expenditure subtotal | जम्मा/कूल व्यय रू.
Fine/penalty | जरिवाना रू.
Contract/budget amount | ठेक्का रकम, बजेट रकम
Asset seizure value | जफत गर्ने सम्पत्ति रू.
Co-accused row with — | no amount; asset forfeiture only

IMPORTANT: Income and expenditure subtotals are always larger than bigo in illegal property cases. If you accidentally extract either, the number will be bigger than the bigo stated in the table. Use this as a sanity check.

Rule 9 — Vague/verbal amounts → null
If the amount is expressed in vague prose (करोडौं, अरबौं, लाखौं) with no accompanying numeric, return null.
Word-amount parsing of Nepali number words is error-prone and CIAA's structured documents always pair prose amounts with numerics when a formal bigo exists.

Case ID: {case.case_id}
Case title: {case.title}

Press release markdown:
{markdown[:100000]}
"""

    def _is_explicit_bigo_context(self, evidence_quote: Any) -> bool:
        if not isinstance(evidence_quote, str):
            return False
        normalized_quote = evidence_quote.strip().lower()
        if not normalized_quote:
            return False
        return any(keyword in normalized_quote for keyword in BIGO_CONTEXT_KEYWORDS)

    def _parse_json_response(self, content: str) -> dict[str, Any]:
        content = content.strip()
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain a JSON object.")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON root must be an object.")
        return parsed

    def _confidence_rank(self, confidence: str) -> int:
        rank = {"low": 1, "medium": 2, "high": 3}
        return rank.get(confidence, 0)

    def _coerce_bigo_int(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, float):
            return int(value) if value > 0 else None
        if not isinstance(value, str):
            return None

        normalized = value.translate(_NEPALI_TO_ASCII_DIGITS)
        digits_only = re.sub(r"[^\d]", "", normalized)
        if not digits_only:
            return None
        bigo = int(digits_only)
        return bigo if bigo > 0 else None

    def _patch_case_bigo(
        self,
        case: Case,
        bigo: int,
        api_base_url: str,
        api_token: str,
    ) -> None:
        url = self._case_patch_url(api_base_url, case.id)
        payload = [{"op": "replace", "path": "/bigo", "value": bigo}]
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=url,
            method="PATCH",
            data=data,
            headers={
                "Authorization": f"Token {api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30):  # noqa: S310
                return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise CommandError(
                f"PATCH failed for case {case.case_id} (status {exc.code}): {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CommandError(
                f"PATCH failed for case {case.case_id}: {exc.reason}"
            ) from exc

    def _case_patch_url(self, api_base_url: str, case_db_id: int) -> str:
        parsed = urllib.parse.urlparse((api_base_url or "").strip())
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                f"Invalid api_base_url '{api_base_url}': scheme must be http or https."
            )
        if not parsed.netloc:
            raise ValueError(
                f"Invalid api_base_url '{api_base_url}': URL must include a host."
            )
        path = parsed.path.rstrip("/")
        base = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        if base.endswith("/api"):
            return f"{base}/cases/{case_db_id}/"
        return f"{base}/api/cases/{case_db_id}/"
