"""Tests for the NGM court-data query helper (gated SQL plane)."""

import httpx
import pytest

from jawafdehi_mcp.request_context import current_transport, jawafdehi_bearer_token
from jawafdehi_mcp.tools.ngm_proxy import (
    execute_ngm_proxy_query,
    get_jawafdehi_api_config,
)


class _FakeAsyncClient:
    """Minimal async client stub whose .post returns a preset httpx.Response."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    async def post(self, url, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._response


@pytest.mark.asyncio
async def test_posts_to_query_plane_with_timeout_seconds():
    resp = httpx.Response(
        200, json={"columns": ["a"], "rows": [[1]], "row_count": 1, "query_time_ms": 7}
    )
    client = _FakeAsyncClient(resp)

    out = await execute_ngm_proxy_query(
        client, "https://portal.jawafdehi.org", "svc-token", "SELECT 1", timeout=9
    )

    # Path + renamed param + Bearer auth. `public_projection` is present because
    # no caller bearer was forwarded and the transport is not stdio — see
    # TestPublicProjectionFlag below.
    assert client.calls[0]["url"] == "https://portal.jawafdehi.org/api/query/"
    assert client.calls[0]["json"] == {
        "query": "SELECT 1",
        "timeout_seconds": 9,
        "public_projection": True,
    }
    assert client.calls[0]["headers"]["Authorization"] == "Bearer svc-token"
    # Flat response normalized back into the legacy {success, data} envelope.
    assert out["success"] is True
    assert out["data"] == {"columns": ["a"], "rows": [[1]], "row_count": 1}
    assert out["query_time_ms"] == 7


@pytest.mark.asyncio
async def test_error_status_raises_with_payload():
    resp = httpx.Response(400, json={"detail": "forbidden: SELECT-only"})
    client = _FakeAsyncClient(resp)

    with pytest.raises(RuntimeError, match="NGM query failed"):
        await execute_ngm_proxy_query(
            client, "https://x", None, "DROP TABLE court_cases"
        )


@pytest.mark.asyncio
async def test_non_json_on_success_raises_not_silently_empty():
    # A 200 with a non-JSON body (empty / HTML proxy page) must raise, not return
    # an empty successful result.
    resp = httpx.Response(200, text="<html>gateway timeout</html>")
    client = _FakeAsyncClient(resp)

    with pytest.raises(RuntimeError, match="Non-JSON response from query endpoint"):
        await execute_ngm_proxy_query(client, "https://x", None, "SELECT 1")


@pytest.mark.asyncio
async def test_non_json_on_error_status_raises_query_failed():
    resp = httpx.Response(502, text="<html>bad gateway</html>")
    client = _FakeAsyncClient(resp)

    with pytest.raises(RuntimeError, match="NGM query failed"):
        await execute_ngm_proxy_query(client, "https://x", None, "SELECT 1")


def test_http_query_uses_only_dedicated_query_token(monkeypatch):
    monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "full-stdio-token")
    monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)
    transport = current_transport.set("http")
    try:
        _base_url, token = get_jawafdehi_api_config()
    finally:
        current_transport.reset(transport)

    assert token is None


def test_stdio_query_can_fall_back_to_full_local_token(monkeypatch):
    monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "full-stdio-token")
    monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)
    transport = current_transport.set("stdio")
    try:
        _base_url, token = get_jawafdehi_api_config()
    finally:
        current_transport.reset(transport)

    assert token == "full-stdio-token"


def test_dedicated_query_token_wins_in_all_transports(monkeypatch):
    monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "full-stdio-token")
    monkeypatch.setenv("MCP_QUERY_API_TOKEN", "query-only-token")

    _base_url, token = get_jawafdehi_api_config()

    assert token == "query-only-token"


class TestPublicProjectionFlag:
    """The anonymous SQL path must ASK for the narrow plane, not infer it.

    ``ngm_query_judicial`` is reachable anonymously and authenticates upstream with
    a shared service account. The API decides disclosure from role membership, so
    provisioning that account with ReadOnly (or any NGM role) would have handed the
    unprojected internal plane — soft-deleted rows, ``is_deleted``, every internal
    column — to the open internet. These pin that the request no longer depends on
    how the account is configured.
    """

    @staticmethod
    def _client():
        return _FakeAsyncClient(
            httpx.Response(
                200,
                json={"columns": [], "rows": [], "row_count": 0, "query_time_ms": 1},
            )
        )

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_anonymous_http_forces_the_public_projection(self):
        client = self._client()
        token = current_transport.set("http")
        try:
            await execute_ngm_proxy_query(client, "https://x", "svc-token", "SELECT 1")
        finally:
            current_transport.reset(token)
        assert client.calls[0]["json"]["public_projection"] is True

    @pytest.mark.asyncio
    async def test_forwarded_caller_bearer_does_not_force_it(self):
        """A real user's own bearer decides its own disclosure, as before."""
        client = self._client()
        transport = current_transport.set("http")
        bearer = jawafdehi_bearer_token.set("caller-token")
        try:
            await execute_ngm_proxy_query(client, "https://x", "svc-token", "SELECT 1")
        finally:
            jawafdehi_bearer_token.reset(bearer)
            current_transport.reset(transport)
        assert "public_projection" not in client.calls[0]["json"]
        assert client.calls[0]["headers"]["Authorization"] == "Bearer caller-token"

    @pytest.mark.asyncio
    async def test_stdio_service_token_keeps_the_internal_plane(self):
        """A trusted local operator is entitled to it; this must not change."""
        client = self._client()
        token = current_transport.set("stdio")
        try:
            await execute_ngm_proxy_query(client, "https://x", "svc-token", "SELECT 1")
        finally:
            current_transport.reset(token)
        assert "public_projection" not in client.calls[0]["json"]
