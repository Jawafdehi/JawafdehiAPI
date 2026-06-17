"""
Django management command to extract a case's FACTUAL TIMELINE from CIAA source
documents using LLM extraction, fully over the Jawafdehi HTTP API.

Phase A.3 of the CIAA Case Enrichment pipeline. Populates ``Case.timeline``
with the factual milestones of a corruption case (incident period, complaint,
CIAA investigation, press release, chargesheet filing, interim orders, Special
Court verdict, Supreme Court appeal/verdict) — NOT the routine hearing-by-hearing
court progression (that is already tracked as the case's Pragati Bibaran by case
number). See https://github.com/Jawafdehi/JawafdehiAPI/issues/186.

This command is API-driven: it reads cases, source content and NGM hearing
records over HTTP and writes the timeline via ``PATCH /api/cases/{slug}/``. It
never touches the ORM, so a ``--dry-run`` needs no database credentials — only
an API token (to read DRAFT cases) and an LLM key (timeline generation requires
the model). The LLM is given a ``convert_date`` tool so it converts dates
between Bikram Sambat and Gregorian reliably instead of doing the arithmetic in
its head.

Idempotent: skips cases whose ``timeline`` is already populated (unless
``--force``).

Usage::

    python manage.py enrich_ciaa_timeline --dry-run
    python manage.py enrich_ciaa_timeline --case-id case-0123
    python manage.py enrich_ciaa_timeline --limit 10 --verbose
    python manage.py enrich_ciaa_timeline --fiscal-year 080 --dry-run
    python manage.py enrich_ciaa_timeline --force
"""

import logging
import os
import re
import urllib.parse
from typing import Optional

import requests
from django.core.management.base import BaseCommand, CommandError

from cases.management.commands._enrich_utils import (
    CONVERT_DATE_TOOL,
    CONVERT_DATE_TOOL_ANTHROPIC,
    call_bedrock_with_tools,
    call_llm_with_tools,
    convert_date,
    is_valid_iso_date,
    parse_extraction_response,
    resolve_api_key,
)
from cases.services.priority_case_loader import load_priority_cases

logger = logging.getLogger(__name__)

# Source types (matched as plain strings from the API payload — no model import
# so the command stays ORM-free). Ordered by usefulness for factual milestones.
MILESTONE_SOURCE_TYPES = (
    "AG_ABHIYOG_PATRA",  # charge sheet — richest factual detail
    "CIAA_PRESS_RELEASE",  # complaint / investigation / chargesheet dates
    "COURT_ORDER",  # verdict
    "COURT_FILING_OTHER",
)

EXTRACTION_SYSTEM_PROMPT = """\
You are a Nepali legal analyst reconstructing the FACTUAL TIMELINE of a \
corruption case investigated by Nepal's CIAA (अख्तियार दुरुपयोग अनुसन्धान आयोग) \
and tried at the Special Court (विशेष अदालत).

Your goal is to capture the factual milestones of the case — what happened and \
when — NOT the routine court-procedure log. The court's hearing-by-hearing \
progression (पेशी/sunwai) is already tracked separately as the case's Pragati \
Bibaran by case number, so DO NOT emit one entry per hearing. Use the hearing \
records only to anchor the dates of the milestones below.

MILESTONES TO EXTRACT (include each only when grounded in the sources):
1. Factual incident period — the span the alleged offence covers, BEFORE the
   complaint (the CIAA "jaanch awadhi" / जाँच अवधि, or the period the accused
   held office or the conduct occurred). Emit as a SINGLE entry with both
   "date" (start) and "end_date" (end).
2. Complaint (उजुरी निवेदन) — when the complaint was registered at the CIAA.
3. CIAA investigation (अनुसन्धान) — when the CIAA began/decided to investigate,
   if distinct from the complaint.
4. Press release (प्रेस विज्ञप्ति) — when the CIAA publicly announced the case.
5. Chargesheet filed / case registered (अभियोगपत्र/आरोपपत्र दायर, मुद्दा दर्ता)
   — when the CIAA filed the chargesheet at the Special Court.
6. Interim court order (अन्तरिम आदेश) — any interim order dates, if issued.
7. Special Court verdict (विशेष अदालतको फैसला) — judgment date and outcome
   (conviction / acquittal "सफाई" / partial).
8. Supreme Court appeal (सर्वोच्च अदालतमा पुनरावेदन) — when an appeal was filed.
9. Supreme Court verdict (सर्वोच्च अदालतको फैसला) — final judgment.

ENTRY FORMAT — each entry is a JSON object:
- "date": AD date "YYYY-MM-DD" (Gregorian). REQUIRED.
- "date_bs": the Bikram Sambat date "YYYY-MM-DD" as it appears in the source.
  REQUIRED — every Nepali legal document states dates in BS; record it.
- "end_date": AD "YYYY-MM-DD" — ONLY for the incident-period entry (milestone 1).
- "end_date_bs": the BS date for end_date — only when end_date is present.
- "title": short Nepali label (देवनागरी, 4-12 words) naming the milestone.
- "description": 1-3 Nepali sentences with specifics (amounts, section numbers,
  press-release number, bench, outcome). Optional but strongly encouraged.

DATE CONVERSION TOOL (MANDATORY):
You have a `convert_date` tool that converts between AD (Gregorian) and BS
(Bikram Sambat) using Nepal's official calendar. LLMs routinely get BS<->AD
conversion wrong by days or months — so you MUST NOT convert dates in your head.
- For every date taken from a source document (stated in BS), call convert_date
  with mode="bs_to_ad" to get the AD "date"; keep the original BS as "date_bs".
- For every date taken from the NGM hearing records (in AD), call convert_date
  with mode="ad_to_bs" to get "date_bs"; keep the AD as "date".
- Batch dates into one tool call where possible (the tool accepts a list).
- Use ONLY the tool's output for "date"/"date_bs"; never adjust or round it.
- Verify every entry's "date" and "date_bs" are a matching pair the tool
  returned before emitting it.

QUALITY RULES:
- Order entries chronologically, earliest first.
- Every entry must be grounded in the provided sources. Do NOT fabricate dates,
  events, amounts, or outcomes.
- Omit a milestone entirely if the sources do not support it. Fewer, accurate
  entries are better than padded ones.
- Do NOT emit routine hearing/पेशी entries — synthesize them into milestone 7.
"""

EXTRACTION_USER_PROMPT = """\
Reconstruct the factual timeline for the following CIAA Special Court case.

Case title: {case_title}

Instructions:
- Extract the factual milestones defined in the system prompt that the sources
  support.
- Every entry needs "date" (AD YYYY-MM-DD), "date_bs" (BS YYYY-MM-DD), and a
  Nepali "title".
- Express the factual incident period (milestone 1) as ONE entry with "date" +
  "end_date" (and "date_bs" + "end_date_bs").
- Convert every date with the convert_date tool — do not convert dates yourself.
  Source dates are BS (use bs_to_ad); NGM dates are AD (use ad_to_bs).
- Use the NGM hearing data only to anchor milestone dates (chargesheet
  registration, verdict). Do NOT create one entry per hearing.
- Order entries chronologically; only include milestones the sources support.

Return ONLY a valid JSON array of entry objects. No markdown, no prose.
Format:
[{{"date": "YYYY-MM-DD", "date_bs": "YYYY-MM-DD", "title": "नेपाली शीर्षक", "description": "विवरण"}}]

{ngm_section}

DOCUMENT TEXT (chargesheet, press release, court order — use for milestones,
narrative, amounts, and any dates not in the NGM data):

{source_text}
"""


class Command(BaseCommand):
    help = (
        "Extract the factual timeline of CIAA Special Court cases via LLM, "
        "reading and writing entirely over the Jawafdehi HTTP API."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without PATCHing the API",
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
            "--api-base-url",
            type=str,
            default=os.environ.get("JAWAFDEHI_API_BASE_URL", "http://127.0.0.1:8000"),
            help="Jawafdehi API base URL (root or /api).",
        )
        parser.add_argument(
            "--api-token",
            type=str,
            default=None,
            help="Jawafdehi API token. Defaults to JAWAFDEHI_API_TOKEN.",
        )
        parser.add_argument(
            "--llm-backend",
            type=str,
            choices=("auto", "bedrock", "openai"),
            default="auto",
            help=(
                "LLM backend. 'bedrock' uses AWS Bedrock invoke_model (Claude, "
                "e.g. Opus 4.8); 'openai' uses an OpenAI-compatible gateway. "
                "'auto' (default) infers bedrock for claude/anthropic model ids."
            ),
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default=os.environ.get(
                "BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-8"
            ),
            help="LLM model identifier (default: BEDROCK_MODEL_ID env / Opus 4.8).",
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=os.environ.get(
                "JAWAFDEHI_LLM_PROXY_URL", "https://llm-proxy.jawafdehi.org/v1"
            ),
            help="OpenAI-backend base URL (OpenAI-compatible endpoint).",
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            default=None,
            help="OpenAI-backend API key (defaults to JAWAFDEHI_LLM_API_KEY or ANTHROPIC_API_KEY env var)",
        )
        parser.add_argument(
            "--aws-profile",
            type=str,
            default=os.environ.get(
                "REVIEW_AWS_PROFILE", os.environ.get("AWS_PROFILE", "")
            ),
            help="AWS profile for the bedrock backend (defaults to REVIEW_AWS_PROFILE/AWS_PROFILE).",
        )
        parser.add_argument(
            "--aws-region",
            type=str,
            default=os.environ.get("AWS_REGION", "us-west-2"),
            help="AWS region for the bedrock backend (default: us-west-2).",
        )
        parser.add_argument(
            "--llm-max-tokens",
            type=int,
            default=4000,
            help="LLM response token budget (default: 4000).",
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

    # ── argument resolution / validation ─────────────────────────────────

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        case_id = options.get("case_id")
        limit = self._validate_limit(options.get("limit"))
        force = options.get("force")
        fiscal_year = options.get("fiscal_year")
        priority = options.get("priority")
        verbose = options.get("verbose")

        api_base_url = options["api_base_url"]
        api_token = options.get("api_token") or os.environ.get("JAWAFDEHI_API_TOKEN")

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

        # Reading DRAFT cases requires authentication even for a dry run.
        if not api_token:
            raise CommandError(
                "Jawafdehi API token is required to read DRAFT cases. "
                "Set --api-token or JAWAFDEHI_API_TOKEN."
            )

        # Resolve + validate the LLM backend. Timeline generation always needs
        # the LLM (even on a dry run we still call the model; we just skip PATCH).
        llm_cfg = self._resolve_llm_config(options)

        if fiscal_year and not re.match(r"^\d{2,3}$", fiscal_year):
            raise CommandError(
                f"Invalid fiscal year: {fiscal_year}. "
                "Use 2- or 3-digit format, e.g., '80' or '080'."
            )

        session = self._get_session()
        cases = self._get_ciaa_cases(
            api_base_url=api_base_url,
            api_token=api_token,
            session=session,
            case_id=case_id,
            limit=limit,
            force=force,
            fiscal_year=fiscal_year,
            priority=priority,
        )
        total = len(cases)
        self.stdout.write(
            f"Found {total} CIAA draft cases to process. "
            f"Backend: {llm_cfg['backend']} | Model: {llm_cfg['model']}"
        )
        if force:
            self.stdout.write(
                self.style.WARNING("  --force: re-generating even for populated cases")
            )
        if fiscal_year:
            self.stdout.write(f"  Fiscal year filter: {fiscal_year}")
        if priority:
            self.stdout.write(f"  Priority mode: {len(load_priority_cases())} cases")

        for idx, case in enumerate(cases, 1):
            self._process_case(
                case=case,
                idx=idx,
                total=total,
                dry_run=dry_run,
                llm_cfg=llm_cfg,
                api_base_url=api_base_url,
                api_token=api_token,
                session=session,
            )

        self._print_summary(dry_run)

    def _resolve_llm_config(self, options: dict) -> dict:
        """Resolve and validate the LLM backend + its credentials.

        Backend selection: 'bedrock' or 'openai', or 'auto' which infers bedrock
        for claude/anthropic model ids and openai otherwise. Raises CommandError
        if the chosen backend's required credential is missing.
        """
        model = options["llm_model"]
        backend = options["llm_backend"]
        if backend == "auto":
            lowered = model.lower()
            backend = (
                "bedrock"
                if ("anthropic" in lowered or "claude" in lowered)
                else "openai"
            )

        cfg = {
            "backend": backend,
            "model": model,
            "max_tokens": options["llm_max_tokens"],
        }
        if backend == "bedrock":
            # boto3 resolves credentials from the profile/instance role; we only
            # need the profile/region here. A missing role surfaces at call time.
            cfg["aws_profile"] = options.get("aws_profile") or ""
            cfg["aws_region"] = options.get("aws_region") or "us-west-2"
        else:
            api_key = resolve_api_key(options.get("llm_api_key"))
            if not api_key:
                raise CommandError(
                    "No LLM API key provided for the openai backend. Set "
                    "JAWAFDEHI_LLM_API_KEY or ANTHROPIC_API_KEY, or use --llm-api-key."
                )
            cfg["base_url"] = options["llm_base_url"]
            cfg["api_key"] = api_key
        return cfg

    @staticmethod
    def _validate_limit(limit) -> Optional[int]:
        if limit is None:
            return None
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
        return limit_int

    # ── API reads ─────────────────────────────────────────────────────────

    def _api_root(self, api_base_url: str) -> str:
        """Return the API root (".../api") for the given base URL, validated.

        Memoised: the base URL is constant for a run, so we parse/validate once.
        """
        cached = getattr(self, "_api_root_cache", None)
        if cached is not None and cached[0] == api_base_url:
            return cached[1]
        parsed = urllib.parse.urlparse((api_base_url or "").strip())
        if not (
            parsed.scheme == "https"
            or (parsed.scheme == "http" and self._is_loopback_host(parsed.hostname))
        ):
            raise CommandError(
                f"Invalid api_base_url '{api_base_url}': use https for non-local hosts."
            )
        if not parsed.netloc:
            raise CommandError(
                f"Invalid api_base_url '{api_base_url}': URL must include a host."
            )
        path = parsed.path.rstrip("/")
        base = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        api_root = base if base.endswith("/api") else f"{base}/api"
        self._api_root_cache = (api_base_url, api_root)
        return api_root

    @staticmethod
    def _is_loopback_host(hostname: Optional[str]) -> bool:
        if not hostname:
            return False
        host = hostname.lower().rstrip(".")
        if host == "localhost":
            return True
        import ipaddress

        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _api_get(
        self,
        url: str,
        api_token: str,
        session: requests.Session,
        params: Optional[dict] = None,
    ) -> dict:
        """GET a JSON document from the API with token auth."""
        headers = {
            "Authorization": f"Token {api_token}",
            "Accept": "application/json",
        }
        try:
            response = session.get(url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            body = exc.response.text[:300] if exc.response is not None else ""
            raise CommandError(f"API GET {url} failed (HTTP {status}): {body}") from exc
        except requests.RequestException as exc:
            raise CommandError(f"API GET {url} failed: {exc}") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise CommandError(f"API GET {url} returned invalid JSON: {exc}") from exc

    def _get_ciaa_cases(
        self,
        api_base_url: str,
        api_token: str,
        session: requests.Session,
        case_id: Optional[str] = None,
        limit: Optional[int] = None,
        force: bool = False,
        fiscal_year: Optional[str] = None,
        priority: bool = False,
    ) -> list[dict]:
        """Fetch DRAFT CIAA Special Court cases (as API dicts) to enrich."""
        api_root = self._api_root(api_base_url)
        priority_numbers = set(load_priority_cases()) if priority else None

        selected: list[dict] = []
        next_url = f"{api_root}/cases/"
        params = {"case_type": "CORRUPTION"}

        while next_url:
            page = self._api_get(next_url, api_token, session, params=params)
            params = None  # the `next` link already carries query params
            results = page.get("results", []) if isinstance(page, dict) else []
            for summary in results:
                if summary.get("state") != "DRAFT":
                    continue
                if case_id and summary.get("case_id") != case_id:
                    continue
                if not self._is_ciaa_special_court_case(summary):
                    continue
                if fiscal_year and not self._matches_fiscal_year(summary, fiscal_year):
                    continue
                if priority_numbers is not None and not self._matches_priority(
                    summary, priority_numbers
                ):
                    continue

                if not force and summary.get("timeline"):
                    self.stats["cases_already_populated"] += 1
                    continue

                selected.append(summary)
                if limit and len(selected) >= limit:
                    return selected

            next_url = page.get("next") if isinstance(page, dict) else None

        return selected

    @staticmethod
    def _is_ciaa_special_court_case(case: dict) -> bool:
        court_cases = case.get("court_cases") or []
        return isinstance(court_cases, list) and any(
            isinstance(ref, str) and ref.startswith("special:") for ref in court_cases
        )

    @staticmethod
    def _matches_fiscal_year(case: dict, fiscal_year: str) -> bool:
        fy_normalized = fiscal_year.lstrip("0") or "0"
        for entry in case.get("court_cases") or []:
            if not isinstance(entry, str):
                continue
            case_number = entry.split(":")[-1] if ":" in entry else entry
            if "-CR-" in case_number:
                prefix = case_number.split("-CR-")[0].lstrip("0") or "0"
                if prefix == fy_normalized:
                    return True
        return False

    @staticmethod
    def _matches_priority(case: dict, priority_numbers: set) -> bool:
        for entry in case.get("court_cases") or []:
            if not isinstance(entry, str):
                continue
            case_number = entry.split(":")[-1] if ":" in entry else entry
            if case_number in priority_numbers:
                return True
        return False

    # ── core pipeline ─────────────────────────────────────────────────────

    def _process_case(
        self,
        case: dict,
        idx: int,
        total: int,
        dry_run: bool,
        llm_cfg: dict,
        api_base_url: str,
        api_token: str,
        session: requests.Session,
    ):
        self.stats["cases_processed"] += 1
        case_id = case.get("case_id", "?")
        title = case.get("title", "")
        self.stdout.write(f"\n[{idx}/{total}] {case_id} — {title[:80]}")

        # The case-list summary already carries court_cases/timeline, but we need
        # the detail endpoint to get evidence enriched with source URLs.
        detail = self._fetch_case_detail(case, api_base_url, api_token, session)
        source_text = self._get_source_content(detail)
        ngm_data = self._get_ngm_data(detail, api_base_url, api_token, session)

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

        try:
            entries = self._extract_timeline(
                source_text=source_text or "",
                case_title=title,
                llm_cfg=llm_cfg,
                session=session,
                ngm_data=ngm_data,
            )
        except (requests.RequestException, CommandError, ValueError) as exc:
            self.stats["cases_llm_error"] += 1
            self.stdout.write(self.style.ERROR(f"  LLM extraction failed: {exc}"))
            return

        if not entries:
            self.stats["cases_skipped"] += 1
            self.stdout.write(
                self.style.WARNING("  LLM returned no timeline entries — skipping")
            )
            return

        self.stdout.write(self.style.SUCCESS(f"  Extracted {len(entries)} entry(s)"))
        for i, entry in enumerate(entries, 1):
            span = f" → {entry['end_date']}" if entry.get("end_date") else ""
            self.stdout.write(
                f"    {i}. {entry.get('date', '?')}{span} — "
                f"{entry.get('title', '?')[:80]}"
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("  [DRY RUN] Would PATCH but --dry-run is set")
            )
            return

        try:
            self._patch_timeline(
                case_slug=detail.get("slug") or case.get("slug"),
                case_id=case_id,
                entries=entries,
                api_base_url=api_base_url,
                api_token=api_token,
                session=session,
            )
            self.stats["cases_enriched"] += 1
            self.stdout.write(self.style.SUCCESS(f"  [UPDATED] {case_id}"))
        except CommandError as exc:
            self.stats["cases_llm_error"] += 1
            self.stdout.write(self.style.ERROR(f"  Failed to PATCH timeline: {exc}"))

    def _fetch_case_detail(
        self,
        case: dict,
        api_base_url: str,
        api_token: str,
        session: requests.Session,
    ) -> dict:
        """Fetch the case-detail document (evidence enriched with source URLs)."""
        slug = case.get("slug")
        if not slug:
            return case
        api_root = self._api_root(api_base_url)
        quoted = urllib.parse.quote(str(slug).strip(), safe="")
        url = f"{api_root}/cases/{quoted}/"
        try:
            return self._api_get(url, api_token, session)
        except CommandError as exc:
            logger.warning("  Falling back to summary for %s: %s", slug, exc)
            return case

    # ── source acquisition ─────────────────────────────────────────────────

    def _get_source_content(self, case: dict) -> Optional[str]:
        """Assemble source document text for the milestone-relevant source types.

        Reads the detail endpoint's enriched evidence (each entry may carry a
        nested ``source`` with ``source_type`` and ``urls``). Prefers the
        already-extracted ``description`` when long enough, else downloads and
        converts a MARKDOWN/RAW link from an allowed host.
        """
        evidence = case.get("evidence") or []
        if not evidence:
            return None

        # Group evidence by source_type so we can honour milestone priority.
        by_type: dict[str, list[dict]] = {}
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            if not isinstance(source, dict):
                continue
            stype = source.get("source_type")
            by_type.setdefault(stype, []).append(entry)

        content_parts: list[str] = []
        for stype in MILESTONE_SOURCE_TYPES:
            for entry in by_type.get(stype, []):
                text = self._content_from_evidence_entry(entry)
                if text:
                    content_parts.append(text)

        if not content_parts:
            return None
        return "\n\n---\n\n".join(content_parts)

    def _content_from_evidence_entry(self, entry: dict) -> Optional[str]:
        """Return usable text for one evidence entry.

        Order of preference:
        1. The already-extracted evidence ``description`` when long enough.
        2. An existing MARKDOWN-role link on the source (already converted).
        3. Otherwise, create the markdown with the shared source converter
           (``sourcing.converter.convert_source``) — the canonical likhit/markitdown
           pipeline (PR #178), which disk-caches by URL so we don't re-convert.
        """
        description = (entry.get("description") or "").strip()
        if len(description) > 200:
            return description

        source = entry.get("source") or {}
        urls = source.get("urls") or []

        # 2. Use an existing MARKDOWN link verbatim — it is already converted.
        md_link = next(
            (
                u["link"]
                for u in urls
                if isinstance(u, dict) and u.get("role") == "MARKDOWN" and u.get("link")
            ),
            None,
        )
        if md_link:
            text = self._download_text(md_link)
            if text and len(text) > 200:
                return text

        # 3. No markdown yet — create it from the convertible links.
        convertible = [
            u["link"]
            for u in urls
            if isinstance(u, dict)
            and u.get("link")
            and u.get("role") in ("RAW", "ALTERNATE", "SOURCE_PAGE")
        ]
        if not convertible:
            return None

        from sourcing import converter as source_converter

        result = source_converter.convert_source({"url": convertible})
        if result.get("status") in ("converted", "attached"):
            text = (result.get("markdown") or "").strip()
            if len(text) > 200:
                return text
        else:
            logger.warning(
                "  Source conversion %s: %s",
                result.get("status"),
                result.get("note"),
            )
        return None

    @staticmethod
    def _download_text(url: str) -> Optional[str]:
        """Download an already-converted markdown link and return its text."""
        from sourcing import jds_client

        try:
            content, _ = jds_client.download_source_file(url)
        except Exception as exc:  # noqa: BLE001 - one bad link must not abort
            logger.warning("  Failed to download markdown link %s: %s", url, exc)
            return None
        return content.decode("utf-8", errors="replace")

    # ── NGM structured hearing data (via API) ──────────────────────────────

    def _get_ngm_data(
        self,
        case: dict,
        api_base_url: str,
        api_token: str,
        session: requests.Session,
    ) -> Optional[dict]:
        """Fetch NGM hearing records for the case's special-court reference."""
        special_ref = next(
            (
                ref.split(":", 1)[1]
                for ref in (case.get("court_cases") or [])
                if isinstance(ref, str) and ref.startswith("special:")
            ),
            None,
        )
        if not special_ref:
            return None

        api_root = self._api_root(api_base_url)
        quoted = urllib.parse.quote(f"special:{special_ref}", safe=":")
        url = f"{api_root}/ngm/court_case/{quoted}"
        try:
            data = self._api_get(url, api_token, session)
        except CommandError as exc:
            logger.warning("  NGM query failed for %s: %s", special_ref, exc)
            return None
        if not isinstance(data, dict) or data.get("error"):
            return None
        return data

    def _format_ngm_section(self, ngm_data: Optional[dict]) -> str:
        """Format the flat NGM API payload as a prompt section.

        The NGM detail API returns a flat object (registration/verdict fields at
        the top level, plus a ``hearings`` list), unlike the nested ORM shape.
        """
        if not ngm_data:
            return ""

        lines = [
            "NGM STRUCTURED HEARING DATA (ground-truth AD dates — convert to BS "
            "with the convert_date tool; use only to anchor milestone dates):",
            "",
        ]
        reg_date = ngm_data.get("registration_date_ad")
        verdict_date = ngm_data.get("verdict_date_ad")
        case_status = ngm_data.get("case_status")

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
            verdict_judge = ngm_data.get("verdict_judge")
            if verdict_judge:
                lines.append(f"  Judge: {verdict_judge}")

        return "\n".join(lines) + "\n"

    # ── LLM extraction (tool-use) ───────────────────────────────────────────

    def _extract_timeline(
        self,
        source_text: str,
        case_title: str,
        llm_cfg: dict,
        session: requests.Session,
        ngm_data: Optional[dict] = None,
    ) -> Optional[list[dict]]:
        """Call the LLM (with the convert_date tool) to extract timeline entries.

        Dispatches to the configured backend: the OpenAI-compatible gateway
        (``call_llm_with_tools``) or AWS Bedrock (``call_bedrock_with_tools``).
        """
        prompt = EXTRACTION_USER_PROMPT.format(
            case_title=case_title,
            ngm_section=self._format_ngm_section(ngm_data),
            source_text=source_text[:40000],
        )
        executors = {"convert_date": convert_date}

        if llm_cfg["backend"] == "bedrock":
            response_text = call_bedrock_with_tools(
                system_prompt=EXTRACTION_SYSTEM_PROMPT,
                user_prompt=prompt,
                model_id=llm_cfg["model"],
                tools=[CONVERT_DATE_TOOL_ANTHROPIC],
                tool_executors=executors,
                aws_profile=llm_cfg.get("aws_profile", ""),
                aws_region=llm_cfg.get("aws_region", "us-west-2"),
                max_tokens=llm_cfg["max_tokens"],
            )
        else:
            response_text = call_llm_with_tools(
                system_prompt=EXTRACTION_SYSTEM_PROMPT,
                user_prompt=prompt,
                model=llm_cfg["model"],
                base_url=llm_cfg["base_url"],
                api_key=llm_cfg["api_key"],
                session=session,
                tools=[CONVERT_DATE_TOOL],
                tool_executors=executors,
                max_tokens=llm_cfg["max_tokens"],
            )
        return self._parse_timeline_response(response_text)

    def _parse_timeline_response(self, response_text: str) -> Optional[list[dict]]:
        """Parse the LLM response into clean, validated timeline entries."""
        raw = parse_extraction_response(
            response_text, wrapper_keys={"timeline", "entries"}
        )
        if raw is None:
            return None

        clean = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            entry = self._clean_entry(item)
            if entry is not None:
                clean.append(entry)

        if not clean:
            return None
        clean.sort(key=lambda e: e["date"])
        return clean

    def _clean_entry(self, item: dict) -> Optional[dict]:
        """Validate and normalise a single LLM-produced entry, or drop it."""
        date_val = str(item.get("date") or "").strip()
        title_val = str(
            item.get("title") or item.get("event") or item.get("name") or ""
        ).strip()
        if not date_val or not title_val:
            return None
        if not is_valid_iso_date(date_val):
            logger.warning("  Dropping entry with non-ISO date: %s", date_val)
            return None

        entry: dict = {"date": date_val, "title": title_val}

        desc_val = str(
            item.get("description") or item.get("desc") or item.get("detail") or ""
        ).strip()
        if desc_val:
            entry["description"] = desc_val

        date_bs = str(item.get("date_bs") or "").strip()
        if date_bs:
            entry["date_bs"] = date_bs

        end_date = str(item.get("end_date") or "").strip()
        if end_date:
            if not is_valid_iso_date(end_date):
                logger.warning(
                    "  Dropping invalid end_date %s; keeping entry", end_date
                )
            elif end_date < date_val:
                logger.warning(
                    "  Dropping end_date %s before date %s; keeping entry",
                    end_date,
                    date_val,
                )
            else:
                entry["end_date"] = end_date
                end_date_bs = str(item.get("end_date_bs") or "").strip()
                if end_date_bs:
                    entry["end_date_bs"] = end_date_bs

        return entry

    # ── API write ───────────────────────────────────────────────────────────

    def _patch_timeline(
        self,
        case_slug: Optional[str],
        case_id: str,
        entries: list[dict],
        api_base_url: str,
        api_token: str,
        session: requests.Session,
    ) -> None:
        """PATCH the case timeline via an RFC 6902 JSON Patch replace op."""
        if not case_slug:
            raise CommandError(f"Case {case_id} has no slug; cannot PATCH.")

        api_root = self._api_root(api_base_url)
        quoted = urllib.parse.quote(str(case_slug).strip(), safe="")
        url = f"{api_root}/cases/{quoted}/"
        patch = [{"op": "replace", "path": "/timeline", "value": entries}]
        headers = {
            "Authorization": f"Token {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = session.patch(url, json=patch, headers=headers, timeout=30)
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            body = exc.response.text[:300] if exc.response is not None else ""
            raise CommandError(
                f"PATCH failed for case {case_id} (status {status}): {body}"
            ) from exc
        except requests.RequestException as exc:
            raise CommandError(f"PATCH failed for case {case_id}: {exc}") from exc

    # ── summary ─────────────────────────────────────────────────────────────

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
