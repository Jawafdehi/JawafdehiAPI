"""Thin REST client for the standalone NGM API service.

DEPRECATION / MIGRATION CONTEXT (Decision Q13 — RETIRE the backend NGM proxy):
    The backend used to own a raw-SQL passthrough to NGM's Postgres
    (``connections["ngm"]`` in ``ngm/services.py``). Per the locked NGM API-plane
    proposal (``think-big/ngm/ngm-api-plane.md`` §4 migration step 2), the source
    of truth is moving to the standalone **NGM API service** (FastAPI, in the
    ``ngm`` repo). This module is the migration shim: it lets the backend's
    existing endpoints (``/api/ngm/query_judicial`` and ``/api/ngm/court_case``)
    forward to the NGM service over REST instead of querying the NGM database
    directly, so existing consumers (MCP ``ngm_*`` tools, casework workflow) keep
    working unchanged while the storage moves.

    This whole path is scheduled for removal once consumers re-point directly at
    the NGM service. See ``ngm/services.py`` and ``ngm/api_views.py`` removal
    notes.

The NGM service exposes (see ``ngm-api-plane.md`` §2):
  - a gated raw-SQL endpoint ``POST {NGM_API_BASE_URL}/api/query`` (SELECT-only,
    allowlist/timeout/row-cap enforced server-side), used by the query proxy; and
  - a read-plane resource ``GET {NGM_API_BASE_URL}/api/cases/{court}/{number}``
    returning a full case with hearings + entities, used by the court_case proxy.

Auth: the NGM service authenticates callers with an OIDC bearer token (Zitadel,
the same platform the backend resource-server uses — see ``config/oidc_auth.py``).
The backend calls the NGM service as a **service account** (machine principal).
Token acquisition is currently a TODO/stub (``_service_token``); the structure
(env-configured base URL + bearer header) is wired so only the client-credentials
grant needs filling in.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class NGMServiceError(Exception):
    """The NGM service was unreachable or returned an unexpected response."""


class NGMServiceNotConfigured(NGMServiceError):
    """``NGM_API_BASE_URL`` is not configured."""


class NGMQueryRejected(NGMServiceError):
    """The NGM service rejected the query as invalid (HTTP 400).

    Carries the service's own error message so the proxy can preserve the
    existing client contract (e.g. "Only SELECT queries are allowed").
    """


# Court refs come from operator-entered case data, so validate the parts
# strictly before they reach a URL path. Mirrors review/ngm_client.py.
_COURT_ID_RE = re.compile(r"^[a-z0-9]+$")
_CASE_NUMBER_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _base_url() -> str:
    base = getattr(settings, "NGM_API_BASE_URL", "") or ""
    if not base:
        raise NGMServiceNotConfigured("NGM service is not configured")
    return base.rstrip("/")


def _service_token() -> str | None:
    """Return an OIDC bearer token for the NGM service, or None.

    TODO(ngm-proxy-retire): implement the Zitadel client-credentials (or
    JWT-bearer) grant for the backend's NGM service account and cache the
    access token until shortly before ``exp``. For now we support a static
    token via ``NGM_API_TOKEN`` (useful for local/dev and integration tests);
    when absent the request is sent without an Authorization header so a
    not-yet-gated dev NGM service still works. The acquisition flow should
    reuse the same Zitadel instance as ``config/oidc_auth.py`` (issuer/JWKS),
    obtaining a token whose role grants access to the NGM service's gated
    ``/query`` and read planes.
    """
    return getattr(settings, "NGM_API_TOKEN", "") or None


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = _service_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _timeout(timeout_seconds: float | None) -> float:
    # Give the HTTP call a little headroom over the SQL statement timeout the
    # NGM service enforces, so we surface the service's timeout error rather
    # than tearing down the connection first.
    if timeout_seconds is None:
        return float(getattr(settings, "NGM_API_TIMEOUT_SECONDS", 30))
    return float(timeout_seconds) + 5.0


def query_judicial(query: str, timeout_seconds: float) -> dict:
    """Forward a gated SELECT query to the NGM service's ``/api/query`` endpoint.

    Returns the service's result dict, expected to carry the same shape the
    backend proxy historically produced::

        {"columns": [...], "rows": [...], "row_count": int,
         "max_rows": int, "query_time_ms": int}

    Raises ``NGMQueryRejected`` (HTTP 400 — invalid/non-SELECT query, preserving
    the service's message), ``NGMServiceNotConfigured`` (no base URL), or
    ``NGMServiceError`` (transport / non-200) for everything else.
    """
    url = f"{_base_url()}/api/query"
    payload = {"query": query, "timeout": timeout_seconds}
    try:
        response = httpx.post(
            url,
            json=payload,
            headers=_headers(),
            timeout=_timeout(timeout_seconds),
        )
    except httpx.HTTPError as exc:
        logger.exception("NGM service query request failed")
        raise NGMServiceError("NGM service request failed") from exc

    if response.status_code == 400:
        # Preserve the service's validation message (e.g. non-SELECT rejection)
        # so the proxy returns the same error contract as before.
        raise NGMQueryRejected(_extract_error(response))
    if response.status_code != 200:
        logger.error("NGM service query returned HTTP %s", response.status_code)
        raise NGMServiceError(f"NGM service query failed: HTTP {response.status_code}")

    body = response.json()
    # The NGM service mirrors the backend's success envelope
    # ({"success", "data", "error", "query_time_ms"}); unwrap it to the flat
    # result dict the proxy view expects. Tolerate a bare result dict too.
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        return {
            "columns": data.get("columns", []),
            "rows": data.get("rows", []),
            "row_count": data.get("row_count", 0),
            "max_rows": data.get("max_rows"),
            "query_time_ms": body.get("query_time_ms", 0),
        }
    if isinstance(body, dict) and "columns" in body:
        return body
    raise NGMServiceError("NGM service returned an unexpected response shape")


def get_court_case(
    court_identifier: str, case_number: str, timeout_seconds=None
) -> dict | None:
    """Fetch one case (with hearings + entities) from the NGM read plane.

    Returns the service's record dict in the shape the proxy view expects::

        {"case": {...}, "hearings": [...], "entities": [...]}

    Returns ``None`` if the case is not found (HTTP 404). Raises
    ``NGMServiceNotConfigured`` / ``NGMServiceError`` otherwise.

    The court/case parts are validated and percent-encoded before being placed
    in the URL path; the case number is expected already-normalized by the view.
    """
    if not _COURT_ID_RE.match(court_identifier or "") or not _CASE_NUMBER_RE.match(
        case_number or ""
    ):
        raise NGMServiceError(
            f"Invalid court/case ref: {court_identifier!r}:{case_number!r}"
        )

    safe_court = quote(court_identifier, safe="")
    safe_number = quote(case_number, safe="")
    url = f"{_base_url()}/api/cases/{safe_court}/{safe_number}"
    try:
        response = httpx.get(
            url,
            headers=_headers(),
            timeout=_timeout(timeout_seconds),
        )
    except httpx.HTTPError as exc:
        logger.exception("NGM service court_case request failed")
        raise NGMServiceError("NGM service request failed") from exc

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.error("NGM service court_case returned HTTP %s", response.status_code)
        raise NGMServiceError(
            f"NGM service court_case failed: HTTP {response.status_code}"
        )

    body = response.json()
    if not isinstance(body, dict):
        raise NGMServiceError("NGM service returned an unexpected response shape")

    # The read plane may return the case fields at the top level alongside
    # `hearings`/`entities` (as the existing backend court_case response does),
    # or already nested under `case`. Normalize to the nested shape the view
    # consumes.
    if "case" in body and isinstance(body["case"], dict):
        return {
            "case": body["case"],
            "hearings": body.get("hearings", []),
            "entities": body.get("entities", []),
        }
    hearings = body.pop("hearings", [])
    entities = body.pop("entities", [])
    return {"case": body, "hearings": hearings, "entities": entities}


def _extract_error(response: httpx.Response) -> str:
    """Pull a human-readable error message out of a non-2xx NGM response."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or "NGM service rejected the request"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str) and error:
            return error
        if error:
            return str(error)
    return "NGM service rejected the request"
