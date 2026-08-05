"""Shared HTTP client for bounded calls into the Jawafdehi control plane."""

from __future__ import annotations

import math
import os
from typing import Any, Mapping
from urllib.parse import unquote

import httpx

from .api_transport import embedded_api_client_kwargs
from .request_context import get_forwarded_headers, get_local_service_token


def _base_url() -> str:
    value = os.getenv("JAWAFDEHI_API_BASE_URL", "https://api.jawafdehi.org").rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("JAWAFDEHI_API_BASE_URL must be an HTTP(S) URL.")
    return value


def _timeout_seconds() -> float:
    raw = os.getenv("MCP_CONTROL_PLANE_TIMEOUT", "30")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 30.0
    if not math.isfinite(value) or value <= 0:
        return 30.0
    return min(value, 120.0)


def _auth_headers() -> dict[str, str]:
    headers = get_forwarded_headers()
    if headers:
        return headers
    token = get_local_service_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _validate_api_path(path: str) -> None:
    if (
        not path.startswith("/api/")
        or "://" in path
        or "?" in path
        or "#" in path
        or "\\" in path
    ):
        raise ValueError("Control-plane requests require a fixed /api/ path.")
    decoded = path
    for _ in range(6):
        if any(segment in {".", ".."} for segment in decoded.split("/")):
            raise ValueError("Control-plane path contains a traversal segment.")
        next_value = unquote(decoded)
        if next_value == decoded:
            return
        decoded = next_value
    raise ValueError("Control-plane path is excessively percent-encoded.")


def _response_payload(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


async def request_control_plane(
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Call one fixed control-plane route and return a stable tool envelope."""
    _validate_api_path(path)
    request_headers = _auth_headers()
    if headers:
        request_headers.update(headers)

    timeout = _timeout_seconds()
    async with httpx.AsyncClient(
        **embedded_api_client_kwargs(),
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        response = await client.request(
            method,
            f"{_base_url()}{path}",
            params=params,
            json=json_body,
            headers=request_headers,
        )

    result: dict[str, Any] = {
        "success": response.is_success,
        "status_code": response.status_code,
        "data": _response_payload(response),
    }
    if etag := response.headers.get("etag"):
        result["etag"] = etag
    if not response.is_success:
        result["error"] = "Jawafdehi control-plane request failed."
    return result
