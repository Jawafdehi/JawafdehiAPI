"""Tests for the Jawafdehi case search / read tools.

Focus: BB-03 — the search tool must not force a case_type=CORRUPTION filter
(which hid tax-evasion and other non-corruption cases), and read tools must
surface the API's error body (e.g. an expired forwarded token) rather than a
bare status string.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from jawafdehi_mcp.tools.jawafdehi_cases import (
    GetJawafdehiCaseTool,
    SearchJawafdehiCasesTool,
)

_PATCH_TARGET = "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient"


def _mock_async_client(response):
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)

    context_manager = AsyncMock()
    context_manager.__aenter__.return_value = client
    context_manager.__aexit__.return_value = False
    return context_manager, client


def _query_of(client):
    """Parsed query dict of the URL the tool issued its GET against."""
    args, _ = client.get.await_args
    return parse_qs(urlparse(args[0]).query)


class TestSearchJawafdehiCasesTool:
    def setup_method(self):
        self.tool = SearchJawafdehiCasesTool()

    def test_metadata_is_not_corruption_scoped(self):
        assert self.tool.name == "search_jawafdehi_cases"
        # Must not advertise itself as corruption-only (BB-03).
        assert "(corruption)" not in self.tool.description.lower()
        assert "case_type" in self.tool.input_schema["properties"]

    @pytest.mark.asyncio
    async def test_default_search_sends_no_case_type(self):
        response = MagicMock()
        response.is_success = True
        response.json.return_value = {"count": 3, "page": 1, "results": []}
        context_manager, client = _mock_async_client(response)

        with patch(_PATCH_TARGET, return_value=context_manager):
            await self.tool.execute({"search": "Ncell"})

        query = _query_of(client)
        assert query["type"] == ["case"]
        assert query["q"] == ["Ncell"]
        # BB-03: no case_type filter unless the caller explicitly asks for one.
        assert "case_type" not in query

    @pytest.mark.asyncio
    async def test_case_type_filter_is_passed_through(self):
        response = MagicMock()
        response.is_success = True
        response.json.return_value = {"count": 1, "page": 1, "results": []}
        context_manager, client = _mock_async_client(response)

        with patch(_PATCH_TARGET, return_value=context_manager):
            await self.tool.execute({"search": "tax", "case_type": "TAX_EVASION"})

        assert _query_of(client)["case_type"] == ["TAX_EVASION"]

    @pytest.mark.asyncio
    async def test_error_body_is_surfaced(self):
        response = MagicMock()
        response.is_success = False
        response.status_code = 401
        response.json.return_value = {"detail": "Token has expired."}
        context_manager, _ = _mock_async_client(response)

        with patch(_PATCH_TARGET, return_value=context_manager):
            result = await self.tool.execute({"search": "Ncell"})

        payload = json.loads(result[0].text)
        assert payload["status_code"] == 401
        assert payload["details"] == {"detail": "Token has expired."}


class TestGetJawafdehiCaseTool:
    def setup_method(self):
        self.tool = GetJawafdehiCaseTool()

    @pytest.mark.asyncio
    async def test_missing_slug_errors(self):
        result = await self.tool.execute({})
        assert "slug" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_not_found_is_friendly(self):
        response = MagicMock()
        response.status_code = 404
        context_manager, _ = _mock_async_client(response)

        with patch(_PATCH_TARGET, return_value=context_manager):
            result = await self.tool.execute({"slug": "missing-case"})

        assert "not found" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_error_body_is_surfaced(self):
        response = MagicMock()
        response.status_code = 401
        response.is_success = False
        response.json.return_value = {"detail": "Token has expired."}
        context_manager, _ = _mock_async_client(response)

        with patch(_PATCH_TARGET, return_value=context_manager):
            result = await self.tool.execute({"slug": "some-case"})

        payload = json.loads(result[0].text)
        assert payload["status_code"] == 401
        assert payload["details"] == {"detail": "Token has expired."}
