"""Shared library for the casework sourcing/enrichment scripts.

Every script imports this. It provides:
  * bootstrap(): boot Django with the DB-optional settings and bridge the
    existing JAWAFDEHI_LLM_* env vars onto the llm package's settings, so
    operators keep their current environment and no DATABASE_URL is required.
  * CaseworkApi: a small HTTP client to read cases and PATCH case fields
    (RFC-6902 JSON Patch), using JAWAFDEHI_API_BASE_URL / JAWAFDEHI_API_TOKEN.
  * convert_date_tool(): the AD<->BS date-conversion Tool for llm.invoke_with_tools.

These scripts never touch the ORM; reads and writes go over the API.
"""

import logging
import os
import sys
import urllib.parse

import requests

log = logging.getLogger("casework")

# ── Django bootstrap (DB-free) ───────────────────────────────────────────────

_DEFAULT_PROXY_URL = "https://llm-proxy.jawafdehi.org/v1"


def bootstrap(provider: str = "proxy", model: str = "") -> None:
    """Boot Django for a script: DB-optional settings + LLM env bridge.

    Maps the existing JAWAFDEHI_LLM_* env onto the llm package's settings BEFORE
    django.setup() (settings read env at import). `provider` selects the llm
    backend for both tiers ("proxy" or "bedrock"); `model` (or the
    JAWAFDEHI_LLM_MODEL env) sets the model id for that provider. Proxy has no
    default model, so a proxy run needs --model / JAWAFDEHI_LLM_MODEL; bedrock
    defaults to BEDROCK_MODEL_ID.
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings_scripts")

    # Bridge the enrichers' existing env onto the llm package's settings names.
    proxy_url = os.environ.get("JAWAFDEHI_LLM_PROXY_URL", _DEFAULT_PROXY_URL)
    os.environ.setdefault("LLM_PROXY_BASE_URL", proxy_url)
    api_key = os.environ.get("JAWAFDEHI_LLM_API_KEY") or os.environ.get(
        "ANTHROPIC_API_KEY", ""
    )
    if api_key:
        os.environ.setdefault("LLM_PROXY_API_KEY", api_key)
    # The public llm-proxy host sits behind a Cloudflare WAF that 403s the OpenAI
    # SDK's default UA; scripts run out-of-cluster, so default a WAF-friendly UA.
    os.environ.setdefault("LLM_PROXY_USER_AGENT", "curl/8.4.0")

    os.environ["REVIEW_LLM_PROVIDER_PREMIUM"] = provider
    os.environ["REVIEW_LLM_PROVIDER_CHEAP"] = provider

    # Model id for the selected provider (both tiers). Default from env. Each
    # provider reads its own settings names, so route --model to the right pair.
    model = model or os.environ.get("JAWAFDEHI_LLM_MODEL", "")
    if model:
        model_env = {
            "bedrock": ("BEDROCK_MODEL_ID", "BEDROCK_MODEL_ID_CHEAP"),
            "claude_cli": ("CLAUDE_CLI_MODEL_PREMIUM", "CLAUDE_CLI_MODEL_CHEAP"),
            "codex_cli": ("CODEX_MODEL_ID", "CODEX_MODEL_ID"),
        }.get(provider, ("LLM_PROXY_MODEL_ID", "LLM_PROXY_MODEL_ID_CHEAP"))
        for name in model_env:
            os.environ[name] = model

    import django

    django.setup()


# ── HTTP API client (read + PATCH; no ORM) ───────────────────────────────────


class CaseworkApi:
    """Token-authenticated HTTP client for the Jawafdehi case API.

    Reads cases and PATCHes case fields over HTTP — no database access.
    """

    def __init__(self, base_url: str = None, token: str = None):
        self.base_url = (
            base_url
            or os.environ.get("JAWAFDEHI_API_BASE_URL")
            or "http://127.0.0.1:8000"
        ).rstrip("/")
        self.token = token or os.environ.get("JAWAFDEHI_API_TOKEN")
        if not self.token:
            raise RuntimeError(
                "API token is required; set --api-token or JAWAFDEHI_API_TOKEN."
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {self.token}",
                "Accept": "application/json",
            }
        )

    def _api_root(self) -> str:
        return (
            self.base_url if self.base_url.endswith("/api") else f"{self.base_url}/api"
        )

    def get(self, path: str, params: dict = None, timeout: int = 30) -> dict:
        url = path if path.startswith("http") else f"{self._api_root()}{path}"
        resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def iter_cases(self, params: dict = None, timeout: int = 60):
        """Yield case dicts from the paginated /cases/ list endpoint."""
        next_url = f"{self._api_root()}/cases/"
        first = True
        while next_url:
            data = self.get(next_url, params=params if first else None, timeout=timeout)
            first = False
            for row in data.get("results", data if isinstance(data, list) else []):
                yield row
            next_url = data.get("next") if isinstance(data, dict) else None

    def get_case(self, slug: str, timeout: int = 30) -> dict:
        quoted = urllib.parse.quote(str(slug).strip(), safe="")
        return self.get(f"/cases/{quoted}/", timeout=timeout)

    def patch_field(self, slug: str, field: str, value, timeout: int = 30) -> None:
        """RFC-6902 JSON Patch: replace a single case field over HTTP."""
        if not slug:
            raise RuntimeError("cannot PATCH a case with no slug")
        quoted = urllib.parse.quote(str(slug).strip(), safe="")
        url = f"{self._api_root()}/cases/{quoted}/"
        patch = [{"op": "replace", "path": f"/{field}", "value": value}]
        resp = self.session.patch(
            url,
            json=patch,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()

    def create_entity(
        self, display_name: str, nes_id: str = "", timeout: int = 30
    ) -> int:
        """POST /api/entities/ to create a JawafEntity; return its id."""
        url = f"{self._api_root()}/entities/"
        payload = {"display_name": display_name}
        if nes_id:
            payload["nes_id"] = nes_id
        resp = self.session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["id"]


# ── convert_date tool (AD <-> Bikram Sambat) ─────────────────────────────────

_DEVANAGARI_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def convert_date(dates: list, mode: str) -> dict:
    """Convert YYYY-MM-DD dates between AD and BS via the `nepali` package.

    Returns {input: converted-or-"Error: ..."}. Runs in-process (no network);
    the same calendar math the jawafdehi-mcp convert_date tool uses.
    """
    import datetime as _dt

    from nepali.datetime import nepalidate

    if mode not in ("ad_to_bs", "bs_to_ad"):
        raise ValueError("mode must be 'ad_to_bs' or 'bs_to_ad'")
    if not isinstance(dates, list):
        raise ValueError("dates must be a list of YYYY-MM-DD strings")

    results: dict[str, str] = {}
    for raw in dates:
        if not isinstance(raw, str):
            results[str(raw)] = "Error: date must be a YYYY-MM-DD string"
            continue
        normalized = (
            raw.strip().translate(_DEVANAGARI_TO_ASCII_DIGITS).replace("/", "-")
        )
        parts = normalized.split("-")
        if len(parts) != 3:
            results[raw] = "Error: date must be in YYYY-MM-DD format"
            continue
        try:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            if mode == "ad_to_bs":
                converted = nepalidate.from_date(_dt.date(year, month, day)).strftime(
                    "%Y-%m-%d"
                )
            else:
                converted = (
                    nepalidate(year, month, day)
                    .to_datetime()
                    .date()
                    .strftime("%Y-%m-%d")
                )
            results[raw] = converted
        except Exception as exc:  # noqa: BLE001
            results[raw] = f"Error: {exc}"
    return results


def convert_date_tool():
    """An llm.tools.Tool wrapping convert_date for invoke_with_tools."""
    from llm.tools import Tool

    return Tool(
        name="convert_date",
        description=(
            "Convert dates between AD (Gregorian) and BS (Bikram Sambat) using "
            "Nepal's official calendar (Asia/Kathmandu). LLMs frequently get "
            "BS<->AD conversion wrong; always use this tool instead of converting "
            "in your head."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Dates in YYYY-MM-DD format.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["ad_to_bs", "bs_to_ad"],
                    "description": "Direction of conversion.",
                },
            },
            "required": ["dates", "mode"],
        },
        run=convert_date,
        run_path="casework.common:convert_date",
    )


# ── shared CLI args + logging ────────────────────────────────────────────────


def add_common_args(parser):
    """Add the flags shared by every enrichment script."""
    parser.add_argument(
        "--slug", action="append", help="Specific case slug(s) (repeatable)"
    )
    parser.add_argument(
        "--court-case",
        action="append",
        help="Specific court case number(s), e.g. 081-CR-0121 (repeatable)",
    )
    parser.add_argument("--limit", type=int, help="Max number of cases to process")
    parser.add_argument("--fiscal-year", help="Filter by fiscal year (e.g. '080')")
    parser.add_argument(
        "--priority",
        action="store_true",
        help="Only process cases listed in cases/data/priority_cases.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even if the target field is already populated",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without PATCHing the API"
    )
    parser.add_argument(
        "--provider",
        choices=("proxy", "bedrock", "claude_cli", "codex_cli"),
        default="proxy",
        help="LLM provider (claude_cli = local `claude -p` subscription harness)",
    )
    parser.add_argument(
        "--model", default="", help="Model id (JAWAFDEHI_LLM_MODEL); required for proxy"
    )
    parser.add_argument(
        "--api-base-url", default=None, help="API base URL (JAWAFDEHI_API_BASE_URL)"
    )
    parser.add_argument(
        "--api-token", default=None, help="API token (JAWAFDEHI_API_TOKEN)"
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    return parser


def setup_logging(verbose=False):
    """Uniform logging: human INFO/DEBUG to stdout, chatty HTTP libs quieted."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "urllib3", "boto3", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── CIAA case filtering ──────────────────────────────────────────────────────


def is_ciaa_special_court_case(case: dict) -> bool:
    """True if the case has a CIAA Special Court reference (court_cases 'special:...')."""
    court_cases = case.get("court_cases") or []
    return isinstance(court_cases, list) and any(
        isinstance(ref, str) and ref.startswith("special:") for ref in court_cases
    )


def matches_fiscal_year(case: dict, fiscal_year: str) -> bool:
    """True if a court reference's fiscal-year prefix matches `fiscal_year`."""
    fy = fiscal_year.lstrip("0") or "0"
    for entry in case.get("court_cases") or []:
        if not isinstance(entry, str):
            continue
        case_number = entry.split(":")[-1] if ":" in entry else entry
        if "-CR-" in case_number:
            prefix = case_number.split("-CR-")[0].lstrip("0") or "0"
            if prefix == fy:
                return True
    return False


# ── source content ───────────────────────────────────────────────────────────


def content_from_evidence_entry(entry: dict):
    """Usable text for one evidence entry: an already-extracted description, an
    existing MARKDOWN-role link, or a fresh likhit conversion of the source."""
    description = (entry.get("description") or "").strip()
    if len(description) > 200:
        return description

    urls = (entry.get("source") or {}).get("urls") or []
    md_link = next(
        (
            u["link"]
            for u in urls
            if isinstance(u, dict) and u.get("role") == "MARKDOWN" and u.get("link")
        ),
        None,
    )
    if md_link:
        try:
            from sourcing import jds_client

            content, _ = jds_client.download_source_file(md_link)
            text = content.decode("utf-8", errors="replace")
            if len(text) > 200:
                return text
        except Exception:  # noqa: BLE001
            pass

    convertible = [
        u["link"]
        for u in urls
        if isinstance(u, dict)
        and u.get("link")
        and u.get("role") in ("RAW", "ALTERNATE", "SOURCE_PAGE")
    ]
    if not convertible:
        return None
    try:
        from sourcing import converter as source_converter

        result = source_converter.convert_source({"url": convertible})
        if result.get("status") in ("converted", "attached"):
            text = (result.get("markdown") or "").strip()
            if len(text) > 200:
                return text
    except Exception:  # noqa: BLE001
        pass
    return None


def source_content(case: dict, source_types=None):
    """Concatenate usable evidence content (optionally only the given source_types,
    e.g. {'CIAA_PRESS_RELEASE'}). Returns (text, char_count)."""
    parts = []
    for entry in case.get("evidence") or []:
        if not isinstance(entry, dict):
            continue
        if source_types:
            if (entry.get("source") or {}).get("source_type") not in source_types:
                continue
        text = content_from_evidence_entry(entry)
        if text:
            parts.append(text)
    joined = "\n\n---\n\n".join(parts)
    return joined, len(joined)


def env_int(name: str, default: int) -> int:
    """Read an int from env, falling back to `default`. Lets the runner widen the
    source-content caps for big-context models (e.g. claude 1M) without code edits."""
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def clamp(text: str, limit: int, label: str = "source") -> str:
    """Truncate `text` to `limit` chars (<=0 = no limit) and PRINT total vs sent,
    so an operator can see how much of each source actually reached the model."""
    text = text or ""
    total = len(text)
    sent = text if (limit <= 0 or total <= limit) else text[:limit]
    note = "" if len(sent) == total else f"  (capped at {limit:,})"
    print(f"    {label}: {total:,} total chars, sent {len(sent):,}{note}")
    return sent


# ── target case selection ────────────────────────────────────────────────────


def _court_number(ref) -> str:
    """Normalize a court ref/number for matching: 'special:081-CR-0121' or
    '081-CR-0121' -> '081-CR-0121' (uppercased, trimmed)."""
    if not isinstance(ref, str):
        return ""
    return ref.split(":")[-1].strip().upper()


def get_target_cases(api, args, skip_field):
    """Yield the case dicts to enrich, honoring --slug / --court-case /
    --fiscal-year / --limit / --force. `skip_field` is the case field that, when
    already populated, skips the case unless --force (e.g. 'timeline', 'tags').

    Cases are selected by slug or court case number (e.g. 081-CR-0121); there is
    no internal case_id selector. --slug and --court-case fetch the case in ANY
    state; the batch path (no selector) scans DRAFT CORRUPTION CIAA cases.
    """
    count = 0
    limit = getattr(args, "limit", None)
    force = getattr(args, "force", False)

    def _wanted(case) -> bool:
        return is_ciaa_special_court_case(case) and (force or not case.get(skip_field))

    # 1) Explicit slugs -> direct fetch (any state).
    for slug in getattr(args, "slug", None) or []:
        try:
            case = api.get_case(slug)
        except requests.HTTPError as exc:
            log.warning("fetch %s failed: %s", slug, exc)
            continue
        if _wanted(case):
            yield case
            count += 1
            if limit and count >= limit:
                return
    if getattr(args, "slug", None):
        return

    # 2) Explicit court case numbers -> resolve by scanning court_cases (any state).
    court_cases = getattr(args, "court_case", None)
    if court_cases:
        wanted_nums = {_court_number(c) for c in court_cases if c}
        seen = set()
        for summary in api.iter_cases(params={"case_type": "CORRUPTION"}):
            nums = {_court_number(ref) for ref in summary.get("court_cases") or []}
            slug = summary.get("slug")
            if not slug or slug in seen or not (wanted_nums & nums):
                continue
            seen.add(slug)
            try:
                case = api.get_case(slug)  # authoritative full detail
            except requests.HTTPError as exc:
                log.warning("fetch %s failed: %s", slug, exc)
                continue
            if _wanted(case):
                yield case
                count += 1
                if limit and count >= limit:
                    return
        return

    # 3) Batch: pull ALL corruption cases and filter CLIENT-SIDE. The /cases/ API
    #    ignores ?state, so we can't filter DRAFT (or court/fiscal year) server-side;
    #    instead we select on the cheap list summary (which carries state/court_cases
    #    and the key_allegations/tags/entities/bigo fields) and only fetch the full
    #    detail for matches — the detail is what carries `evidence` (the list summary
    #    omits it) and the timeline/description fields, which the enrichers need.
    fiscal_year = getattr(args, "fiscal_year", None)
    priority_nums = None
    if getattr(args, "priority", False):
        from cases.services.priority_case_loader import load_priority_cases

        priority_nums = {_court_number(n) for n in load_priority_cases()}
        log.info("Priority mode: %d priority case number(s) loaded", len(priority_nums))
    scanned = 0
    log.info("Scanning corruption cases (filtering client-side)...")
    for summary in api.iter_cases(params={"case_type": "CORRUPTION"}):
        scanned += 1
        if scanned % 500 == 0:
            log.info("  scanned %d cases (matched %d so far)...", scanned, count)
        if summary.get("state") != "DRAFT":
            continue
        if not is_ciaa_special_court_case(summary):
            continue
        if fiscal_year and not matches_fiscal_year(summary, fiscal_year):
            continue
        if priority_nums is not None:
            nums = {_court_number(ref) for ref in summary.get("court_cases") or []}
            if not (priority_nums & nums):
                continue
        # Cheap skip for fields present on the summary (allegations/tags/entities/bigo).
        if not force and summary.get(skip_field):
            continue
        slug = summary.get("slug")
        if not slug:
            continue
        try:
            case = api.get_case(slug)  # full detail: evidence + timeline/description
        except requests.HTTPError as exc:
            log.warning("fetch %s failed: %s", slug, exc)
            continue
        # Full skip check for fields only on the detail (timeline/description).
        if not force and case.get(skip_field):
            continue
        yield case
        count += 1
        if limit and count >= limit:
            return


# ── LLM-response parsing (lifted from the old _enrich_utils) ──────────────────


def is_valid_iso_date(date_str) -> bool:
    """Strict YYYY-MM-DD validation."""
    from datetime import date

    if not isinstance(date_str, str):
        return False
    candidate = date_str.strip()
    if len(candidate) != 10 or candidate[4] != "-" or candidate[7] != "-":
        return False
    try:
        date.fromisoformat(candidate)
        return True
    except (ValueError, TypeError):
        return False


def balanced_object(text: str, start: int):
    """Return the balanced ``{...}`` substring starting at ``start``, or None if
    it never closes. JSON-string aware, so braces inside quoted values (e.g. an
    evidence_quote) don't throw off the depth count — unlike a brace regex."""
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


def parse_extraction_response(response_text, wrapper_keys):
    """Extract a JSON array from an LLM response (handles ```fences``` and
    {"<key>": [...]} wrappers). Returns the list, or None."""
    import json

    text = (response_text or "").strip()
    if "```" in text:
        start = text.find("```")
        nl = text.find("\n", start)
        if nl != -1:
            end = text.find("```", nl)
            if end != -1:
                text = text[nl + 1 : end].strip()

    obj_start = text.find("{")
    if obj_start != -1:
        depth, obj_end = 0, -1
        for i, ch in enumerate(text[obj_start:], obj_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj_end = i
                    break
        if obj_end != -1:
            try:
                obj = json.loads(text[obj_start : obj_end + 1])
                if isinstance(obj, dict):
                    for key in wrapper_keys:
                        if isinstance(obj.get(key), list) and obj[key]:
                            return obj[key]
            except json.JSONDecodeError:
                pass

    arr_start = text.find("[")
    if arr_start != -1:
        depth, arr_end = 0, -1
        for i, ch in enumerate(text[arr_start:], arr_start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    arr_end = i
                    break
        if arr_end != -1:
            try:
                entries = json.loads(text[arr_start : arr_end + 1])
                if isinstance(entries, list) and entries:
                    return entries
            except json.JSONDecodeError:
                pass
    return None


# ── summary ──────────────────────────────────────────────────────────────────


def print_summary(stats: dict, dry_run: bool, title: str):
    """Uniform end-of-run summary from a stats dict."""
    print("\n" + "=" * 60)
    print(f"[DRY RUN] {title}" if dry_run else title)
    for key, val in stats.items():
        print(f"  {key.replace('_', ' ').capitalize():<24} {val}")
