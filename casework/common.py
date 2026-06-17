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

import os
import urllib.parse

import requests

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

    # Model id for the selected provider (both tiers). Default from env.
    model = model or os.environ.get("JAWAFDEHI_LLM_MODEL", "")
    if model:
        if provider == "bedrock":
            os.environ["BEDROCK_MODEL_ID"] = model
            os.environ["BEDROCK_MODEL_ID_CHEAP"] = model
        else:
            os.environ["LLM_PROXY_MODEL_ID"] = model
            os.environ["LLM_PROXY_MODEL_ID_CHEAP"] = model

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
    )
