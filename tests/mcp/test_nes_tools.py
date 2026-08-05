"""Tests for NES-backed MCP tools."""

import json

import httpx
import pytest

from jawafdehi_mcp.request_context import current_transport
from jawafdehi_mcp.server import TOOL_MAP
from jawafdehi_mcp.tools.nes import (
    GetNESEntitiesTool,
    GetNESEntityPrefixesTool,
    SearchNESEntitiesTool,
)


class _FakeAsyncClient:
    def __init__(self, get_impl):
        self._get_impl = get_impl

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, timeout=None):
        return await self._get_impl(url, timeout, headers)


class TestSearchNESEntitiesTool:
    def setup_method(self):
        self.tool = SearchNESEntitiesTool()

    def test_schema_matches_supported_entity_filters(self):
        schema = self.tool.input_schema

        assert schema["required"] == []
        assert set(schema["properties"]) == {
            "entity_type",
            "entity_prefix",
            "query",
            "tags",
            "limit",
            "offset",
        }
        assert "sub_type" not in schema["properties"]
        assert schema["properties"]["limit"]["maximum"] == 100

    @pytest.mark.asyncio
    async def test_entity_type_is_optional_and_supported_filters_are_forwarded(
        self, monkeypatch
    ):
        seen = {}

        async def fake_get(url, timeout, headers=None):
            seen["url"] = httpx.URL(url)
            return httpx.Response(
                200,
                json={"entities": [], "total": 0, "limit": 25, "offset": 10},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", "https://api.example")
        monkeypatch.setattr(
            "jawafdehi_mcp.tools.nes.httpx.AsyncClient",
            lambda **_kwargs: _FakeAsyncClient(fake_get),
        )

        result = await self.tool.execute(
            {
                "entity_prefix": "organization/political_party",
                "query": "party",
                "tags": "national,registered",
                "limit": 25,
                "offset": 10,
            }
        )

        assert json.loads(result[0].text)["total"] == 0
        assert dict(seen["url"].params) == {
            "limit": "25",
            "query": "party",
            "entity_prefix": "organization/political_party",
            "tags": "national,registered",
            "offset": "10",
        }


def test_get_entities_schema_documents_canonical_iris():
    description = (
        GetNESEntitiesTool()
        .input_schema["properties"]["entity_ids"]["description"]
    )

    assert "https://jawafdehi.org/entity/person/" in description
    assert "entity:person/" not in description


class TestGetNESEntityPrefixesTool:
    def setup_method(self):
        self.tool = GetNESEntityPrefixesTool()

    def test_tool_name(self):
        assert self.tool.name == "get_nes_entity_prefixes"

    def test_input_schema_is_empty_object(self):
        assert self.tool.input_schema == {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def test_tool_registered_with_server(self):
        assert "get_nes_entity_prefixes" in TOOL_MAP

    @pytest.mark.asyncio
    async def test_successful_response(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", "https://api.example")

        async def fake_get(url, timeout, headers=None):
            assert url == "https://api.example/api/entity_prefixes"
            assert timeout == 30.0
            return httpx.Response(
                200,
                json={
                    "prefixes": [
                        {"prefix": "person", "entity_type": "person"},
                        {
                            "prefix": "organization/political_party",
                            "entity_type": "organization",
                        },
                    ]
                },
            )

        monkeypatch.setattr(
            "jawafdehi_mcp.tools.nes.httpx.AsyncClient",
            lambda **_kwargs: _FakeAsyncClient(fake_get),
        )

        result = await self.tool.execute({})

        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert parsed["prefixes"][0]["prefix"] == "person"

    @pytest.mark.asyncio
    async def test_non_200_response_includes_http_code(self, monkeypatch):
        async def fake_get(url, timeout, headers=None):
            return httpx.Response(503, json={"detail": "NES unavailable"})

        monkeypatch.setattr(
            "jawafdehi_mcp.tools.nes.httpx.AsyncClient",
            lambda: _FakeAsyncClient(fake_get),
        )

        result = await self.tool.execute({})

        assert "HTTP 503" in result[0].text
        assert "NES unavailable" in result[0].text

    @pytest.mark.asyncio
    async def test_service_token_fallback_when_no_caller_bearer(self, monkeypatch):
        # No forwarded caller bearer → the service token is sent as Bearer, so
        # token-only (stdio) flows keep authenticating once NES requires auth.
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "svc-token")
        captured = {}

        async def fake_get(url, timeout, headers=None):
            captured["headers"] = headers or {}
            return httpx.Response(200, json={"prefixes": []})

        monkeypatch.setattr(
            "jawafdehi_mcp.tools.nes.httpx.AsyncClient",
            lambda: _FakeAsyncClient(fake_get),
        )

        token = current_transport.set("stdio")
        try:
            await self.tool.execute({})
        finally:
            current_transport.reset(token)

        assert captured["headers"].get("Authorization") == "Bearer svc-token"

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_http_does_not_fall_back_to_service_token(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "svc-token")
        captured = {}

        async def fake_get(url, timeout, headers=None):
            captured["headers"] = headers or {}
            return httpx.Response(200, json={"prefixes": []})

        monkeypatch.setattr(
            "jawafdehi_mcp.tools.nes.httpx.AsyncClient",
            lambda **_kwargs: _FakeAsyncClient(fake_get),
        )

        token = current_transport.set("http")
        try:
            await self.tool.execute({})
        finally:
            current_transport.reset(token)

        assert "Authorization" not in captured["headers"]
