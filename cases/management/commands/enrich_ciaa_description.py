"""
Django management command to generate a CIAA case's narrative DESCRIPTION (and,
optionally, a regenerated TITLE) from its source documents using LLM extraction,
fully over the Jawafdehi HTTP API.

Phase A.4 of the CIAA Case Enrichment pipeline. Populates ``Case.description``
(Markdown) with the case summary defined in issue #199 — the अभियोगदावी /
बयान / फैसला structure of a corruption case — and regenerates ``Case.title``
into a concise, searchable headline ending in the special-court case number.
See https://github.com/Jawafdehi/JawafdehiAPI/issues/199.

This command is API-driven (mirrors ``enrich_ciaa_timeline``): it reads cases,
source content and NGM hearing records over HTTP and writes via
``PATCH /api/cases/{slug}/``. Source documents (legacy ``.doc`` charge sheets and
court orders) are converted to Markdown by ``sourcing.converter.convert_source``,
which uses LibreOffice headless for the ``.doc`` -> ``.docx`` -> Markdown path.

``--dry-run`` is the DEFAULT: it prints the generated title + description and
writes nothing. Pass ``--patch`` to actually PATCH the API. Either way the
regenerated title is printed to the console.

Usage::

    python manage.py enrich_ciaa_description --case-id case-0123          # dry run
    python manage.py enrich_ciaa_description --priority --patch
    python manage.py enrich_ciaa_description --case-id case-0123 --skip-title
    python manage.py enrich_ciaa_description --priority --benchmark-dir ground_truth
"""

import json
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
    resolve_api_key,
)
from cases.services.priority_case_loader import load_priority_cases

logger = logging.getLogger(__name__)

# Source types ordered by usefulness for the description, richest first.
DESCRIPTION_SOURCE_TYPES = (
    "AG_ABHIYOG_PATRA",  # charge sheet — the prosecution claim verbatim
    "CIAA_PRESS_RELEASE",  # the allegation summary + amounts
    "COURT_ORDER",  # verdict — outcome, bench, reasoning
    "COURT_FILING_OTHER",
)

# A court case number like 080-CR-0047 / 081-WO-1234. Case-insensitive to match
# the review gate's detector (review.rules_engine.COURT_RE uses [A-Za-z]). The
# negative lookbehind/ahead anchor the token so a malformed number with extra
# adjacent digits/letters (e.g. "080-CR-00478") does NOT spuriously match the
# real number "080-CR-0047" as a substring — title validation must be exact.
COURT_RE = re.compile(r"(?<![\dA-Za-z])\d{2,3}-[A-Za-z]{1,3}-\d{3,4}(?![\dA-Za-z])")

# Source-budget (characters) fed to the description prompt. The Special Court
# verdict (.doc) is often 100k+ chars — far past any sane prompt budget — so it
# is SUMMARISED in a first LLM pass when it exceeds VERDICT_SUMMARY_TRIGGER, and
# the summary (not the raw verdict) goes into the description prompt. The charge
# sheet and press release are short enough to pass through whole.
SOURCE_TEXT_BUDGET = 60000  # total chars of source text in the description prompt
VERDICT_SUMMARY_TRIGGER = 12000  # summarise a COURT_ORDER longer than this
VERDICT_SUMMARY_TARGET = 8000  # approx chars the verdict summary should occupy

VERDICT_SUMMARY_SYSTEM_PROMPT = """\
You are a Nepali legal analyst. You are given the full text of a Special Court \
(विशेष अदालत) judgment (फैसला) in a CIAA corruption case. Produce a faithful \
Nepali summary (देवनागरी, government/court register; keep English technical terms \
as-is) that a downstream writer will use to draft the "विशेष अदालतको फैसलाको सार" \
section of a public case record.

Capture ONLY what the judgment states — never infer or invent:
- फैसला मिति (judgment date) and the इजलास / न्यायाधीशहरू (the bench, by name).
- नि.नं. / मुद्दा नं. and the parties (वादी / प्रतिवादीहरू).
- For EACH defendant: the outcome — दोषी (convicted, with कैद/जरिवाना/बिगो असुल) or
  सफाई (acquitted) — and the court's key reasoning for it.
- Any legal principle / नजिर the court established.
- The disputed बिगो the court accepted or rejected, and why.

Be specific (names, दफा, amounts, dates) but concise — aim for about \
%(target)d characters. Output plain Nepali prose/short lists, NOT JSON.
""" % {"target": VERDICT_SUMMARY_TARGET}

EXTRACTION_SYSTEM_PROMPT = """\
You are a Nepali legal analyst writing the public case summary (description) for \
Jawafdehi, a civic accountability archive of Nepal's anti-corruption cases. The \
case was investigated by the CIAA (अख्तियार दुरुपयोग अनुसन्धान आयोग) and tried at \
the Special Court (विशेष अदालत).

You will be given the case's key allegations, factual timeline, bigo (बिगो) \
amount, named entities, and the full text of the source documents (CIAA press \
release, charge sheet/अभियोगपत्र, and Special Court verdict/फैसला). Write a \
faithful, well-structured Markdown description.

LANGUAGE: Write in formal Nepali (देवनागरी), matching the register of the court \
and government source documents. Keep technical, proper, and forensic terms in \
their original form (English where the source uses English — e.g. "CR", \
"Common Authorship", company names, "forensic") rather than forcing a translation.

STRUCTURE — use these Markdown sections, in this order, but ONLY include a \
section when the sources actually support it (omit sections with no grounding; \
never invent content to fill one):

### क) अभियोगदावीको सार
The prosecution's claim: the core facts, how they breach the law (cite the
ऐन/दफा when the sources state them), the evidence the CIAA relied on, the persons
involved, the बिगो, and the punishment sought. When the CIAA lays out distinct
grounds/findings, present them as a numbered list (**१.** … **२.** …). When there
are multiple defendants with per-person amounts or demands, present them as a
Markdown table (प्रतिवादी | भूमिका/अभियोग | बिगो | मागदावी).

### ख) प्रतिवादीको बयानको सार
For EACH defendant, summarise their statement (बयान) before the authorised
authority or the court in at least ~100 words: whether they admit (स्वीकार) or
deny (इन्कार) the allegation and their reasoning. With several defendants, use a
Markdown table (क्र.सं | प्रतिवादी | भूमिका | बयानको सार).

### ग) विशेष अदालतको फैसलाको सार
The verdict: the judgment date, the bench (इजलास / न्यायाधीशहरू), and the outcome
for each defendant (दोषी / सफाई). Briefly state the court's reasoning and any
legal principle (नजिर) it established.

### घ) पुनरावेदनको सार
Only if the sources or a supreme-court reference show an appeal: the grounds and
legal basis of the appeal and who filed it.

### ङ) सर्वोच्च अदालतको फैसलाको सार
Only if a Supreme Court judgment is in the sources: date, bench, and final
outcome.

### च) नजिरको सार
Only if the judgment establishes a precedent: state the key principle only.

QUALITY RULES:
- Ground every sentence in the provided sources/case data. Do NOT fabricate
  names, amounts, section numbers, dates, benches, or outcomes. If the verdict is
  not in the sources, write section ग only to the extent the timeline/NGM data
  supports (e.g. "मिति … मा फैसला भएको") and omit unknown specifics.
- Prefer specifics from the documents (exact बिगो, दफा, र.नं./नि.नं., dates,
  named officials) over vague phrasing.
- Use the बिगो figure provided in the case data as the headline amount.
- This is an official public record drawn from government/court documents; do not
  soften, editorialise, or add commentary. Neutral, factual tone only.

TITLE RULES (when asked to regenerate the title):
- Produce a concise, engaging, SEARCHABLE Nepali headline that names the real
  subject of the case — the institution/scheme and/or the principal accused, and
  ideally the बिगो amount or the nature of the offence.
- Vary the construction across cases; do not use a rigid template. Be catchy but
  strictly factual.
- NEVER put a defendant HEADCOUNT in the title. Forbidden: any "<संख्या> जना",
  "समेत X जना", "X प्रतिवादी(माथि/मा)", "तीन/चार… अध्यक्षसहित", or similar count
  of people. This applies even when there are many defendants.
    * Many defendants → name the ONE principal accused (or the institution) and
      use "लगायत" / "सहित" with NO number, e.g.
      "…सचिव संजय शर्मासहित…", NOT "…सचिवसमेत १२ जना…".
    * BAD:  "…पदाधिकारीसमेत १२ जना सबैले सफाई" / "…२४९ प्रतिवादीमाथि…"
      GOOD: "…सामुदायिक वनका पदाधिकारीसहित सबैलाई सफाई" /
            "…तत्कालीन अध्यक्ष <नाम> लगायतमाथि भ्रष्टाचार अभियोग"
- The title MUST end with the special-court case number in parentheses, exactly
  as given to you, e.g. "… (080-CR-0047)".
- Keep it under ~160 characters.

OUTPUT FORMAT — return ONLY a single JSON object, no markdown fences, no prose:
{"title": "नेपाली शीर्षक (080-CR-0047)", "description": "### क) …\\n…"}
When title regeneration is disabled you may omit "title" or set it to null.
"""

EXTRACTION_USER_PROMPT = """\
Write the Jawafdehi case description for the following CIAA Special Court case.

Current title: {case_title}
Special-court case number (MUST end the regenerated title): {court_number}
Bigo (बिगो), NPR: {bigo}
Court case references: {court_cases}

{title_instruction}

KEY ALLEGATIONS (already curated for this case):
{key_allegations}

FACTUAL TIMELINE (already curated; dates are reliable — use for section ग etc.):
{timeline}

NAMED ENTITIES (accused / related / location):
{entities}

{ngm_section}

SOURCE DOCUMENTS (press release, charge sheet, verdict — the factual basis for
the description; quote specifics from here):

{source_text}

Return ONLY the JSON object described in the system prompt.
"""

TITLE_ON = (
    "Regenerate the title following the TITLE RULES, ending in the case number "
    'above. Return it in the JSON "title" field.'
)
TITLE_OFF = (
    'Do NOT regenerate the title; set "title" to null in the JSON. Only write '
    "the description."
)


class Command(BaseCommand):
    help = (
        "Generate CIAA Special Court case descriptions (and regenerate titles) "
        "via LLM, reading and writing entirely over the Jawafdehi HTTP API."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--patch",
            action="store_true",
            help="Actually PATCH the API. Default is a dry run (prints only).",
        )
        parser.add_argument(
            "--case-id", type=str, help="Process a specific case by case_id"
        )
        parser.add_argument(
            "--limit", type=int, help="Maximum number of cases to process"
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-generate even if a substantial description already exists",
        )
        parser.add_argument(
            "--fiscal-year", type=str, help="Filter by fiscal year (e.g. '080')"
        )
        parser.add_argument(
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list",
        )
        parser.add_argument(
            "--skip-title",
            action="store_true",
            help="Do not regenerate the title; only write the description.",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=1,
            help=(
                "Number of cases to process in parallel (each case = a verdict-"
                "summary + a description LLM call). 1 (default) runs serially. "
                "Bedrock throttling is handled by adaptive retry; keep this at or "
                "below the account's Bedrock TPM headroom (8-ish is safe)."
            ),
        )
        parser.add_argument(
            "--benchmark-dir",
            type=str,
            default=None,
            help=(
                "Directory of <court_number>.md ground-truth descriptions; when "
                "set, prints a coverage diff of generated vs human description."
            ),
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
            help="LLM backend ('auto' infers bedrock for claude/anthropic models).",
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
            help="OpenAI-backend API key (defaults to JAWAFDEHI_LLM_API_KEY/ANTHROPIC_API_KEY).",
        )
        parser.add_argument(
            "--aws-profile",
            type=str,
            default=os.environ.get(
                "REVIEW_AWS_PROFILE", os.environ.get("AWS_PROFILE", "")
            ),
            help="AWS profile for the bedrock backend.",
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
            default=8000,
            help="LLM response token budget (default: 8000 — descriptions are long).",
        )
        parser.add_argument(
            "--verbose", action="store_true", help="Enable verbose debug logging"
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
        self._http_session: Optional[requests.Session] = None
        self._api_root_cache = None

    def _get_session(self) -> requests.Session:
        if self._http_session is None:
            self._http_session = requests.Session()
        return self._http_session

    # ── handle ───────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        patch = options["patch"]
        dry_run = not patch
        case_id = options.get("case_id")
        limit = self._validate_limit(options.get("limit"))
        force = options.get("force")
        fiscal_year = options.get("fiscal_year")
        priority = options.get("priority")
        skip_title = options.get("skip_title")
        benchmark_dir = options.get("benchmark_dir")
        concurrency = self._validate_concurrency(options.get("concurrency"))
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
            self.stdout.write(
                self.style.WARNING("[DRY RUN] No changes will be saved (pass --patch).")
            )
        if not api_token:
            raise CommandError(
                "Jawafdehi API token is required to read DRAFT cases. "
                "Set --api-token or JAWAFDEHI_API_TOKEN."
            )

        llm_cfg = self._resolve_llm_config(options)

        if fiscal_year and not re.match(r"^\d{2,3}$", fiscal_year):
            raise CommandError(f"Invalid fiscal year: {fiscal_year}.")

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
        workers = min(concurrency, total) if total else 1
        self.stdout.write(
            f"Found {total} CIAA draft cases to process. "
            f"Backend: {llm_cfg['backend']} | Model: {llm_cfg['model']} | "
            f"Title: {'skip' if skip_title else 'regenerate'} | "
            f"Concurrency: {workers}"
        )

        kwargs = dict(
            total=total,
            dry_run=dry_run,
            force=force,
            skip_title=skip_title,
            benchmark_dir=benchmark_dir,
            llm_cfg=llm_cfg,
            api_base_url=api_base_url,
            api_token=api_token,
        )
        if workers <= 1:
            self._run_serial(cases, kwargs)
        else:
            self._run_concurrent(cases, kwargs, workers)

        self._print_summary(dry_run)

    # ── dispatch (serial / concurrent) ────────────────────────────────────

    def _run_serial(self, cases, kwargs):
        """Process cases one at a time, streaming each case's output as it goes."""
        for idx, case in enumerate(cases, 1):
            out, delta = self._process_case(case=case, idx=idx, session=None, **kwargs)
            self.stdout.write(out.getvalue(), ending="")
            self._merge_stats(delta)

    def _run_concurrent(self, cases, kwargs, workers):
        """Process cases in a thread pool.

        Each case is an independent unit of work (its own HTTP session + output
        buffer); Bedrock throttling is absorbed by botocore adaptive retry. Each
        case's buffered output is flushed atomically as its future completes, so
        the long per-case description dumps never interleave. Stats are merged in
        this (main) thread only — no shared mutable state across workers.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    self._process_case, case=case, idx=idx, session=None, **kwargs
                ): idx
                for idx, case in enumerate(cases, 1)
            }
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    out, delta = fut.result()
                    self.stdout.write(out.getvalue(), ending="")
                    self._merge_stats(delta)
                except Exception as exc:  # noqa: BLE001 - one case must not abort all
                    self.stats["cases_llm_error"] += 1
                    self.stdout.write(
                        self.style.ERROR(f"[{idx}/{kwargs['total']}] crashed: {exc}")
                    )

    def _merge_stats(self, delta: dict):
        for k, v in delta.items():
            self.stats[k] = self.stats.get(k, 0) + v

    def _resolve_llm_config(self, options: dict) -> dict:
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
            cfg["aws_profile"] = options.get("aws_profile") or ""
            cfg["aws_region"] = options.get("aws_region") or "us-west-2"
        else:
            api_key = resolve_api_key(options.get("llm_api_key"))
            if not api_key:
                raise CommandError(
                    "No LLM API key for the openai backend. Set JAWAFDEHI_LLM_API_KEY "
                    "or ANTHROPIC_API_KEY, or use --llm-api-key."
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
            raise CommandError(f"Invalid --limit value: {limit}.")
        if limit_int <= 0:
            raise CommandError(f"Invalid --limit: {limit_int}. Must be positive.")
        return limit_int

    @staticmethod
    def _validate_concurrency(value) -> int:
        try:
            n = int(value) if value is not None else 1
        except (ValueError, TypeError):
            raise CommandError(f"Invalid --concurrency value: {value}.")
        if n < 1:
            raise CommandError(f"Invalid --concurrency: {n}. Must be >= 1.")
        return n

    # ── API reads (shared shape with enrich_ciaa_timeline) ─────────────────

    def _api_root(self, api_base_url: str) -> str:
        cached = self._api_root_cache
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
            raise CommandError(f"Invalid api_base_url '{api_base_url}': missing host.")
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

    def _api_get(self, url, api_token, session, params=None) -> dict:
        headers = {"Authorization": f"Token {api_token}", "Accept": "application/json"}
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
        api_base_url,
        api_token,
        session,
        case_id=None,
        limit=None,
        force=False,
        fiscal_year=None,
        priority=False,
    ) -> list[dict]:
        api_root = self._api_root(api_base_url)
        priority_numbers = set(load_priority_cases()) if priority else None

        selected: list[dict] = []
        next_url = f"{api_root}/cases/"
        params = {"case_type": "CORRUPTION"}
        while next_url:
            page = self._api_get(next_url, api_token, session, params=params)
            params = None
            results = page.get("results", []) if isinstance(page, dict) else []
            for summary in results:
                if case_id and summary.get("case_id") != case_id:
                    continue
                # case_id is unique: once we see it, decide its fate and return —
                # never keep paging the (2900+ case) CORRUPTION list, regardless
                # of whether it passes the DRAFT/court/fiscal/priority filters.
                if case_id:
                    if self._is_enrichable(
                        summary, fiscal_year, priority_numbers, force
                    ):
                        selected.append(summary)
                    return selected
                if not self._is_enrichable(
                    summary, fiscal_year, priority_numbers, force
                ):
                    continue
                selected.append(summary)
                if limit and len(selected) >= limit:
                    return selected
            next_url = page.get("next") if isinstance(page, dict) else None
        return selected

    def _is_enrichable(self, summary, fiscal_year, priority_numbers, force) -> bool:
        """Whether a case-list summary should be selected for enrichment."""
        if summary.get("state") != "DRAFT":
            return False
        if not self._is_ciaa_special_court_case(summary):
            return False
        if fiscal_year and not self._matches_fiscal_year(summary, fiscal_year):
            return False
        if priority_numbers is not None and not self._matches_priority(
            summary, priority_numbers
        ):
            return False
        # Cheap pre-filter: the case-LIST serializer normally OMITS `description`,
        # so this rarely fires — the authoritative idempotency skip is in
        # _process_case against the detail payload. Kept for the (local/custom)
        # case where a list response does carry a description (saves a fetch).
        if not force and self._has_substantial_description(summary):
            self.stats["cases_already_populated"] += 1
            return False
        return True

    @staticmethod
    def _has_substantial_description(case: dict) -> bool:
        return len((case.get("description") or "").strip()) >= 600

    @staticmethod
    def _is_ciaa_special_court_case(case: dict) -> bool:
        court_cases = case.get("court_cases") or []
        return isinstance(court_cases, list) and any(
            isinstance(ref, str) and ref.startswith("special:") for ref in court_cases
        )

    @staticmethod
    def _matches_fiscal_year(case: dict, fiscal_year: str) -> bool:
        fy = fiscal_year.lstrip("0") or "0"
        for entry in case.get("court_cases") or []:
            if not isinstance(entry, str):
                continue
            num = entry.split(":")[-1] if ":" in entry else entry
            if "-CR-" in num and (num.split("-CR-")[0].lstrip("0") or "0") == fy:
                return True
        return False

    @staticmethod
    def _matches_priority(case: dict, priority_numbers: set) -> bool:
        for entry in case.get("court_cases") or []:
            if not isinstance(entry, str):
                continue
            num = entry.split(":")[-1] if ":" in entry else entry
            if num in priority_numbers:
                return True
        return False

    # ── core pipeline ───────────────────────────────────────────────────────

    def _process_case(
        self,
        case,
        idx,
        total,
        dry_run,
        force,
        skip_title,
        benchmark_dir,
        llm_cfg,
        api_base_url,
        api_token,
        session=None,
    ):
        # Thread-safe: this method touches no shared mutable state. It writes to
        # a private buffer and returns (buffer, stats_delta); the caller flushes
        # the buffer atomically and merges the delta on the main thread.
        from io import StringIO

        out = StringIO()
        delta: dict = {"cases_processed": 1}
        # Each call (concurrent or not) uses its own HTTP session — requests
        # Sessions are not safe to share across threads.
        session = session or requests.Session()

        def w(msg=""):
            out.write(msg + "\n")

        def warn(msg):
            w(self.style.WARNING(msg))

        case_id = case.get("case_id", "?")
        title = case.get("title", "")
        court_number = self._special_court_number(case)
        w(f"\n[{idx}/{total}] {case_id} — {title[:80]}")

        detail = self._fetch_case_detail(case, api_base_url, api_token, session)

        # Idempotency: skip cases that already have a substantial description.
        # This MUST be checked on the detail payload — the case-LIST serializer
        # (CaseListSerializer) drops `description`, so the equivalent check in
        # _get_ciaa_cases never fires. Without this, every run re-generates and
        # (under --patch) OVERWRITES existing descriptions.
        if not force and self._has_substantial_description(detail):
            # Count this like the list-level already-populated skip: as
            # already_populated only, NOT as processed (an already-described case
            # is not "processed"), so the summary totals reconcile regardless of
            # whether the skip fired at the list or detail stage.
            delta.pop("cases_processed", None)
            delta["cases_already_populated"] = 1
            warn("  Already has a substantial description — skipping (use --force)")
            return out, delta

        source_parts = self._get_source_parts(detail)
        ngm_data = self._get_ngm_data(detail, api_base_url, api_token, session)

        if not source_parts:
            delta["cases_no_content"] = 1
            warn("  No source content — skipping")
            return out, delta
        w(
            "  Sources: "
            + ", ".join(f"{stype}({len(text)})" for stype, text in source_parts)
        )

        try:
            result = self._generate(
                detail=detail,
                court_number=court_number,
                source_parts=source_parts,
                ngm_data=ngm_data,
                skip_title=skip_title,
                llm_cfg=llm_cfg,
                session=session,
                out=out,
            )
        except (requests.RequestException, CommandError, ValueError) as exc:
            delta["cases_llm_error"] = 1
            w(self.style.ERROR(f"  LLM generation failed: {exc}"))
            return out, delta

        if not result or not result.get("description"):
            delta["cases_skipped"] = 1
            warn("  No description generated — skip")
            return out, delta

        new_title = result.get("title")
        description = result["description"]

        # Always print the (regenerated or current) title.
        w(self.style.SUCCESS(f"  TITLE: {new_title or title}"))
        w(f"  DESCRIPTION: {len(description)} chars")
        w(self._indent(description, "    | "))

        title_issue = (
            self._validate_title(new_title, court_number) if new_title else None
        )
        if title_issue:
            warn(f"  TITLE WARNING: {title_issue}")
        title_has_headcount = bool(new_title and self._title_has_headcount(new_title))
        if title_has_headcount:
            warn(
                "  TITLE WARNING: contains a defendant headcount "
                "(e.g. 'X जना' / 'X प्रतिवादी') — title NOT written."
            )

        if benchmark_dir and court_number:
            self._print_benchmark(benchmark_dir, court_number, description, out)

        if dry_run:
            warn("  [DRY RUN] not patching")
            return out, delta

        # Never write a title under --skip-title (even if the model returned one
        # despite the prompt); otherwise don't write a title that fails the
        # court-number gate or carries a defendant headcount. The description is
        # written either way.
        patch_title = (
            new_title
            if (
                not skip_title
                and new_title
                and not title_issue
                and not title_has_headcount
            )
            else None
        )
        try:
            self._patch_case(
                case_slug=detail.get("slug") or case.get("slug"),
                case_id=case_id,
                description=description,
                title=patch_title,
                api_base_url=api_base_url,
                api_token=api_token,
                session=session,
            )
            delta["cases_enriched"] = 1
            w(self.style.SUCCESS(f"  [UPDATED] {case_id}"))
        except CommandError as exc:
            delta["cases_llm_error"] = 1
            w(self.style.ERROR(f"  PATCH failed: {exc}"))
        return out, delta

    def _fetch_case_detail(self, case, api_base_url, api_token, session) -> dict:
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

    @staticmethod
    def _special_court_number(case: dict) -> Optional[str]:
        for ref in case.get("court_cases") or []:
            if isinstance(ref, str) and ref.startswith("special:"):
                return ref.split(":", 1)[1]
        # Fall back to any court number present.
        for ref in case.get("court_cases") or []:
            if isinstance(ref, str) and ":" in ref:
                return ref.split(":", 1)[1]
        return None

    # ── source acquisition (same approach as enrich_ciaa_timeline) ──────────

    def _get_source_parts(self, case: dict) -> list[tuple[str, str]]:
        """Return [(source_type, text)] for milestone-relevant sources, in
        priority order (charge sheet, press release, verdict, other)."""
        evidence = case.get("evidence") or []
        if not evidence:
            return []
        by_type: dict[str, list[dict]] = {}
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            if not isinstance(source, dict):
                continue
            by_type.setdefault(source.get("source_type"), []).append(entry)

        parts: list[tuple[str, str]] = []
        for stype in DESCRIPTION_SOURCE_TYPES:
            for entry in by_type.get(stype, []):
                text = self._content_from_evidence_entry(entry)
                if text:
                    parts.append((stype, text))
        return parts

    def _content_from_evidence_entry(self, entry: dict) -> Optional[str]:
        description = (entry.get("description") or "").strip()
        source = entry.get("source") or {}
        urls = source.get("urls") or []

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

        convertible = [
            u["link"]
            for u in urls
            if isinstance(u, dict)
            and u.get("link")
            and u.get("role") in ("RAW", "ALTERNATE", "SOURCE_PAGE")
        ]
        if convertible:
            from sourcing import converter as source_converter

            result = source_converter.convert_source({"urls": urls})
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
        # Fall back to the evidence description (e.g. NEWS sources) if usable.
        if len(description) > 200:
            return description
        return None

    @staticmethod
    def _download_text(url: str) -> Optional[str]:
        from sourcing import jds_client

        try:
            content, _ = jds_client.download_source_file(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("  Failed to download markdown link %s: %s", url, exc)
            return None
        return content.decode("utf-8", errors="replace")

    # ── NGM ─────────────────────────────────────────────────────────────────

    def _get_ngm_data(self, case, api_base_url, api_token, session) -> Optional[dict]:
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

    @staticmethod
    def _format_ngm_section(ngm_data: Optional[dict]) -> str:
        if not ngm_data:
            return ""
        lines = ["NGM STRUCTURED COURT DATA (ground-truth):", ""]
        if ngm_data.get("registration_date_ad"):
            lines.append(
                f"- Case registration (AD): {ngm_data['registration_date_ad']}"
            )
        if ngm_data.get("case_status"):
            lines.append(f"- Case status: {ngm_data['case_status']}")
        if ngm_data.get("verdict_date_ad"):
            lines.append(f"- Verdict date (AD): {ngm_data['verdict_date_ad']}")
        if ngm_data.get("verdict_judge"):
            lines.append(f"- Verdict bench: {ngm_data['verdict_judge']}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _format_bigo(bigo) -> str:
        """Render the bigo for the prompt; '(unknown)' when missing/non-positive.

        ``bigo`` is nullable on DRAFT cases. Passing None/0 verbatim would put
        the literal "None"/"0" into the prompt, which the model could echo as
        the headline amount — so collapse those to an explicit unknown marker.
        """
        try:
            value = int(bigo)
        except (TypeError, ValueError):
            return "(unknown)"
        return f"{value:,}" if value > 0 else "(unknown)"

    # ── LLM generation ──────────────────────────────────────────────────────

    def _generate(
        self,
        detail,
        court_number,
        source_parts,
        ngm_data,
        skip_title,
        llm_cfg,
        session,
        out,
    ) -> Optional[dict]:
        source_text = self._assemble_source_text(source_parts, llm_cfg, session, out)
        prompt = EXTRACTION_USER_PROMPT.format(
            case_title=detail.get("title", ""),
            court_number=court_number or "(unknown)",
            bigo=self._format_bigo(detail.get("bigo")),
            court_cases=", ".join(detail.get("court_cases") or []) or "(none)",
            title_instruction=TITLE_OFF if skip_title else TITLE_ON,
            key_allegations=self._format_list(detail.get("key_allegations")),
            timeline=json.dumps(detail.get("timeline") or [], ensure_ascii=False),
            entities=self._format_entities(detail.get("entities")),
            ngm_section=self._format_ngm_section(ngm_data),
            source_text=source_text,
        )
        response_text = self._call_llm(
            EXTRACTION_SYSTEM_PROMPT,
            prompt,
            llm_cfg,
            session,
            tools=True,
            max_tokens=llm_cfg["max_tokens"],
        )
        return self._parse_response(response_text)

    def _assemble_source_text(self, source_parts, llm_cfg, session, out=None) -> str:
        """Build the source-document block within SOURCE_TEXT_BUDGET.

        A long COURT_ORDER verdict is summarised in a first LLM pass (so the
        फैसला reasoning survives instead of being truncated mid-document); the
        charge sheet and press release pass through whole. Sections are added in
        priority order (charge sheet, press release, verdict) until the budget
        is spent. Progress is written to ``out`` (the per-case buffer) when given.
        """

        def note(msg):
            if out is not None:
                out.write(msg + "\n")

        prepared: list[tuple[str, str]] = []
        for stype, text in source_parts:
            if stype == "COURT_ORDER" and len(text) > VERDICT_SUMMARY_TRIGGER:
                summary = self._summarize_verdict(text, llm_cfg, session)
                if summary:
                    note(f"  Verdict summarised: {len(text)} -> {len(summary)} chars")
                    prepared.append(("COURT_ORDER (फैसला सारांश)", summary))
                    continue
                # Summary failed — fall back to a truncated head of the verdict.
                prepared.append((stype, text[:VERDICT_SUMMARY_TARGET]))
                continue
            prepared.append((stype, text))

        parts: list[str] = []
        remaining = SOURCE_TEXT_BUDGET
        for label, text in prepared:
            if remaining <= 0:
                note(self.style.WARNING(f"  Budget spent; dropped a {label} source"))
                break
            chunk = text[:remaining]
            parts.append(f"[{label}]\n{chunk}")
            remaining -= len(chunk)
        return "\n\n---\n\n".join(parts)

    def _summarize_verdict(self, verdict_text, llm_cfg, session) -> Optional[str]:
        """First-pass LLM summary of a long Special Court verdict document."""
        try:
            return self._call_llm(
                VERDICT_SUMMARY_SYSTEM_PROMPT,
                "Summarise this Special Court judgment as instructed.\n\n"
                + verdict_text[:120000],
                llm_cfg,
                session,
                tools=False,
                max_tokens=4000,
            ).strip()
        except (requests.RequestException, CommandError, ValueError) as exc:
            logger.warning("  Verdict summarisation failed: %s", exc)
            return None

    def _call_llm(
        self, system_prompt, user_prompt, llm_cfg, session, *, tools, max_tokens
    ) -> str:
        """Dispatch one LLM call to the configured backend.

        The convert_date tool is always offered (BS<->AD conversion); the
        verdict-summary pass simply never calls it. ``tools`` is accepted for
        call-site clarity but does not gate the tool — an empty Bedrock tools
        array can trip a ValidationException, so we keep the tool present.
        """
        executors = {"convert_date": convert_date}
        if llm_cfg["backend"] == "bedrock":
            return call_bedrock_with_tools(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model_id=llm_cfg["model"],
                tools=[CONVERT_DATE_TOOL_ANTHROPIC],
                tool_executors=executors,
                aws_profile=llm_cfg.get("aws_profile", ""),
                aws_region=llm_cfg.get("aws_region", "us-west-2"),
                max_tokens=max_tokens,
            )
        return call_llm_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=llm_cfg["model"],
            base_url=llm_cfg["base_url"],
            api_key=llm_cfg["api_key"],
            session=session,
            tools=[CONVERT_DATE_TOOL],
            tool_executors=executors,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _parse_response(response_text: str) -> Optional[dict]:
        """Parse the {title, description} JSON object from the LLM response.

        Tries EVERY ``{`` position (not just the first) and returns the first
        balanced block that parses to a dict carrying a ``description`` — so
        leading conversational prose that itself contains braces (e.g. "Here is
        the JSON {title, description}: {...}") doesn't abort the parse.
        """
        text = (response_text or "").strip()
        if "```" in text:
            start = text.find("```")
            nl = text.find("\n", start)
            end = text.find("```", nl + 1) if nl != -1 else -1
            if nl != -1 and end != -1:
                text = text[nl + 1 : end].strip()

        for obj_start in range(len(text)):
            if text[obj_start] != "{":
                continue
            block = Command._balanced_object(text, obj_start)
            if block is None:
                continue
            try:
                obj = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "description" in obj:
                desc = (obj.get("description") or "").strip()
                title = obj.get("title")
                title = title.strip() if isinstance(title, str) else None
                return {"description": desc, "title": title or None}
        logger.warning("No JSON object with a description found in LLM response")
        return None

    @staticmethod
    def _balanced_object(text: str, start: int) -> Optional[str]:
        """Return the substring of the balanced {...} block starting at ``start``,
        respecting JSON string quoting/escapes; None if it never closes."""
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    # ── title validation (mirrors the review court_number_in_title gate) ─────

    @staticmethod
    def _validate_title(title: str, court_number: Optional[str]) -> Optional[str]:
        nums = {m.group(0).upper() for m in COURT_RE.finditer(title)}
        if not nums:
            return "regenerated title has no court case number"
        if court_number and court_number.upper() not in nums:
            return (
                f"title number(s) {sorted(nums)} do not include the special-court "
                f"number {court_number}"
            )
        # The contract (and the review gate's convention) is that the number
        # appears at the END, in parentheses — enforce that, not just presence.
        if court_number:
            expected = f"({court_number.upper()})"
            if not title.upper().rstrip().endswith(expected):
                return (
                    f"title must end with the special-court case number "
                    f"in parentheses, e.g. '… {expected}'"
                )
        return None

    # Defendant-headcount patterns the title must avoid: a Devanagari/ASCII
    # number immediately followed by जना / व्यक्ति / प्रतिवादी (e.g. "१२ जना",
    # "249 प्रतिवादी"). The court case number itself (080-CR-0098) won't match —
    # it has no such trailing noun.
    _HEADCOUNT_RE = re.compile(r"[०-९0-9]+\s*(जना|व्यक्ति|प्रतिवादी)")

    @classmethod
    def _title_has_headcount(cls, title: str) -> bool:
        return bool(cls._HEADCOUNT_RE.search(title or ""))

    # ── formatting helpers ───────────────────────────────────────────────────

    @staticmethod
    def _format_list(items) -> str:
        if not items:
            return "(none provided)"
        return "\n".join(f"- {x}" for x in items)

    @staticmethod
    def _format_entities(entities) -> str:
        if not entities:
            return "(none provided)"
        lines = []
        for e in entities:
            if not isinstance(e, dict):
                continue
            name = e.get("display_name") or ""
            etype = e.get("type") or ""
            notes = e.get("notes") or ""
            line = f"- [{etype}] {name}"
            if notes:
                line += f" — {notes}"
            lines.append(line)
        return "\n".join(lines) or "(none provided)"

    @staticmethod
    def _indent(text: str, prefix: str) -> str:
        return "\n".join(prefix + ln for ln in text.splitlines())

    def _print_benchmark(self, benchmark_dir, court_number, generated, out):
        from pathlib import Path

        def w(msg):
            out.write(msg + "\n")

        gt_path = Path(benchmark_dir) / f"{court_number}.md"
        if not gt_path.exists():
            return
        human = gt_path.read_text(encoding="utf-8")
        if len(human.strip()) < 50:
            w(
                self.style.WARNING(
                    f"  BENCHMARK: no human description for {court_number} "
                    "(cold generation; nothing to diff)"
                )
            )
            return
        w(
            self.style.HTTP_INFO(
                f"  BENCHMARK vs human ({court_number}.md, {len(human)} chars "
                f"vs generated {len(generated)} chars):"
            )
        )
        # Coverage heuristics: section headers + key facts present in both.
        for label, sec in (("क", "क)"), ("ख", "ख)"), ("ग", "ग)"), ("घ", "घ)")):
            h = sec in human
            g = sec in generated
            w(
                f"    section {label}): human={'Y' if h else '-'} "
                f"generated={'Y' if g else '-'}"
            )
        # Numbers (amounts, dates, section refs) present in human but missing
        # from the generated text — a quick "what did we drop" signal.
        human_nums = set(re.findall(r"[०-९]{2,}", human))
        gen_nums = set(re.findall(r"[०-९]{2,}", generated))
        missed = sorted(human_nums - gen_nums)[:25]
        if missed:
            w(
                f"    Devanagari numbers in human but NOT generated ({len(human_nums - gen_nums)}): "
                + ", ".join(missed)
            )

    # ── API write ─────────────────────────────────────────────────────────

    def _patch_case(
        self,
        case_slug,
        case_id,
        description,
        title,
        api_base_url,
        api_token,
        session,
    ) -> None:
        if not case_slug:
            raise CommandError(f"Case {case_id} has no slug; cannot PATCH.")
        api_root = self._api_root(api_base_url)
        quoted = urllib.parse.quote(str(case_slug).strip(), safe="")
        url = f"{api_root}/cases/{quoted}/"
        patch = [{"op": "replace", "path": "/description", "value": description}]
        if title:
            patch.append({"op": "replace", "path": "/title", "value": title})
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
                f"PATCH failed for {case_id} (status {status}): {body}"
            ) from exc
        except requests.RequestException as exc:
            raise CommandError(f"PATCH failed for {case_id}: {exc}") from exc

    # ── summary ─────────────────────────────────────────────────────────────

    def _print_summary(self, dry_run: bool):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.SUCCESS(
                f"{'[DRY RUN] ' if dry_run else ''}Description generation complete."
            )
        )
        self.stdout.write(f"  Cases processed:    {self.stats['cases_processed']}")
        self.stdout.write(f"  Cases enriched:     {self.stats['cases_enriched']}")
        self.stdout.write(f"  Cases skipped:      {self.stats['cases_skipped']}")
        self.stdout.write(f"  No source content:  {self.stats['cases_no_content']}")
        self.stdout.write(f"  LLM errors:         {self.stats['cases_llm_error']}")
        self.stdout.write(
            f"  Already populated:  {self.stats['cases_already_populated']}"
        )
