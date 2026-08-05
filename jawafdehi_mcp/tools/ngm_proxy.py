"""Shared helpers for NGM court-data access via the unified Jawafdehi API.

Post-unification (2026-07 hard cut) there is no ``/api/ngm`` proxy: court data
is queried through the platform's gated SQL plane ``POST /api/query/`` on the one
Jawafdehi host. Auth is the caller's OIDC bearer (forwarded), with a service
token fallback for stdio/dev.
"""

import json
import os
from typing import Any

import httpx
import structlog

from ..request_context import (
    get_forwarded_headers,
    get_query_service_token,
    is_local_stdio_transport,
)

logger = structlog.get_logger()


class NGMProxyError(RuntimeError):
    """A non-success response from the gated ``/api/query/`` plane.

    Carries the upstream HTTP ``status_code`` so callers can tell a 4xx (the
    caller's SQL was rejected by the allowlist — an expected input error) apart
    from a 5xx (a real upstream fault) when deciding how loudly to log. Subclasses
    ``RuntimeError`` so existing ``except RuntimeError`` handlers still catch it.
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_jawafdehi_api_config() -> tuple[str, str | None]:
    """Return the API base URL and least-privilege query credential."""
    base_url = os.getenv("JAWAFDEHI_API_BASE_URL", "https://api.jawafdehi.org")
    base_url = base_url.rstrip("/")
    token = get_query_service_token()

    if not base_url.startswith(("http://", "https://")):
        raise ValueError(
            f"JAWAFDEHI_API_BASE_URL must be an HTTP(S) URL. Got: {base_url[:30]}..."
        )

    return base_url, token


def get_jawafdehi_api_config_strict() -> tuple[str, str]:
    """Return validated Jawafdehi API base URL and token (token required)."""
    base_url, token = get_jawafdehi_api_config()
    if not token:
        raise ValueError(
            "MCP_QUERY_API_TOKEN is required (or JAWAFDEHI_API_TOKEN for stdio)."
        )
    return base_url, token


def rows_to_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert proxy response rows+columns payload into dict records."""
    data = payload.get("data") or {}
    columns = data.get("columns") or []
    rows = data.get("rows") or []
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise RuntimeError(
                "Malformed proxy payload: "
                f"row {index} has "
                f"{len(row) if isinstance(row, list) else 'non-list'} values "
                f"for {len(columns)} columns"
            )
        records.append(dict(zip(columns, row)))
    return records


def sql_quote(value: str) -> str:
    """Quote a SQL string literal by escaping single quotes."""
    return value.replace("'", "''")


def _get_proxy_http_timeout() -> float:
    """Return the HTTP call timeout for proxy requests (env MCP_PROXY_HTTP_TIMEOUT, default 30s)."""
    raw = os.getenv("MCP_PROXY_HTTP_TIMEOUT", "30.0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("invalid_mcp_proxy_http_timeout", value=raw)
        return 30.0


async def execute_ngm_proxy_query(
    client: httpx.AsyncClient,
    base_url: str,
    token: str | None,
    query: str,
    timeout: float = 15,
) -> dict[str, Any]:
    """Execute a gated SELECT via the unified court-data SQL plane.

    Posts to ``POST /api/query/`` (the gated SQL route mounted alongside the
    ``/api/courtcases`` read plane; the former ``/api/ngm/query_judicial`` proxy
    is gone). The request body renames ``timeout`` -> ``timeout_seconds``.

    Auth is OIDC ``Bearer``: the caller's forwarded bearer wins, else the service
    token is sent as ``Bearer`` (the platform is OIDC-only — the legacy DRF
    ``Token`` scheme is no longer honoured).

    The endpoint returns a FLAT payload (``{columns, rows, row_count,
    query_time_ms, max_rows}``) and signals success via the HTTP status — there
    is no ``{success, data}`` envelope. We normalise it back into the
    ``{data: {columns, rows, row_count}, query_time_ms}`` shape the callers
    (``rows_to_dicts`` / ``NGMJudicialTool``) already consume.
    """
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # A forwarded caller bearer (HTTP transport) overrides the service token.
    forwarded = get_forwarded_headers()
    headers.update(forwarded)

    body: dict[str, Any] = {"query": query, "timeout_seconds": timeout}
    # No forwarded bearer over HTTP means this request authenticates as the shared
    # MCP service account rather than as a person — the anonymous
    # ``ngm_query_judicial`` path. Ask the API for the narrower public plane
    # explicitly instead of letting it infer disclosure from that account's role
    # membership, which would leak the internal plane to the open internet if the
    # account were ever provisioned with ReadOnly or an NGM role.
    #
    # Computed here rather than at the call sites so it cannot be forgotten by a
    # new one. stdio is excluded deliberately: there the token belongs to a
    # trusted local operator who is entitled to the internal plane, and that is
    # the behaviour this must not change.
    if not forwarded and not is_local_stdio_transport():
        body["public_projection"] = True

    response = await client.post(
        f"{base_url}/api/query/",
        json=body,
        headers=headers,
        timeout=_get_proxy_http_timeout(),
    )

    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        # A non-JSON body on a SUCCESS status (empty body, HTML proxy error page,
        # …) can't be normalized into columns/rows — surface it instead of
        # silently returning an empty successful result.
        if response.is_success:
            raise NGMProxyError(
                f"Non-JSON response from query endpoint "
                f"({response.status_code}): {response.text}",
                response.status_code,
            )
        payload = {
            "detail": f"Non-JSON response from query endpoint ({response.status_code})",
            "raw": response.text,
        }

    if not response.is_success:
        raise NGMProxyError(
            f"NGM query failed ({response.status_code}): "
            f"{json.dumps(payload, ensure_ascii=False)}",
            response.status_code,
        )

    return {
        "success": True,
        "data": {
            "columns": payload.get("columns", []),
            "rows": payload.get("rows", []),
            "row_count": payload.get("row_count", len(payload.get("rows", []))),
        },
        "query_time_ms": payload.get("query_time_ms", 0),
    }
