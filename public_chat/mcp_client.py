from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings
from pydantic import BaseModel, Field


class PublicChatMCPError(RuntimeError):
    pass


PUBLIC_CHAT_AGENT_TOOLS = frozenset(
    {
        "search_jawafdehi_cases",
        "get_jawafdehi_case",
        "search_jawaf_entities",
        "get_jawaf_entity",
        "search_jawafdehi_knowledge",
        "get_jawafdehi_knowledge_source",
        "convert_to_markdown",
    }
)

_TOOL_CACHE: dict[str, dict[str, Any]] = {}


class PublicChatMCPClient:
    """Load public-chat MCP tools for a single agent run."""

    def __init__(
        self,
        *,
        allowed_tools: Iterable[str] | None = None,
    ) -> None:
        self.allowed_tools = frozenset(allowed_tools or PUBLIC_CHAT_AGENT_TOOLS)

    def get_tools(self) -> list[Any]:
        return async_to_sync(self._get_tools)()

    async def _get_tools(self) -> list[Any]:
        servers = getattr(settings, "PUBLIC_CHAT_MCP_SERVERS", {})
        if not servers:
            raise PublicChatMCPError("Public chat MCP server is not configured")
        _validate_mcp_server_lifecycle(servers)

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:
            raise PublicChatMCPError(
                "langchain-mcp-adapters is required for public chat MCP access"
            ) from exc

        tool_map = await self._load_tool_map(MultiServerMCPClient, servers)
        return [tool_map[name] for name in sorted(tool_map)]

    async def _load_tool_map(
        self, client_cls, servers: dict[str, Any]
    ) -> dict[str, Any]:
        if _should_cache_tools(servers):
            cache_key = _tool_cache_key(servers, self.allowed_tools)
            cached = _TOOL_CACHE.get(cache_key)
            if cached and cached["expires_at"] > time.monotonic():
                return cached["tools"]

        client = client_cls(servers)
        tools = await client.get_tools()
        raw_tool_map = {}
        for tool in tools:
            if tool.name not in self.allowed_tools:
                continue
            tool.handle_tool_error = True
            if tool.name == "convert_to_markdown":
                tool = _public_document_converter(tool)
            raw_tool_map[tool.name] = tool

        if _should_cache_tools(servers):
            _TOOL_CACHE[_tool_cache_key(servers, self.allowed_tools)] = {
                "expires_at": time.monotonic()
                + getattr(settings, "PUBLIC_CHAT_MCP_TOOL_CACHE_SECONDS", 300),
                "tools": raw_tool_map,
            }
        return raw_tool_map


class PublicDocumentConversionInput(BaseModel):
    uri: str = Field(
        description="Public http(s) URL of the document or web page to convert."
    )
    pages: str | None = Field(
        default=None,
        description="Optional PDF page or inclusive range, such as '12' or '12-15'.",
    )
    page_start: int | None = Field(
        default=None,
        ge=1,
        description="Optional first PDF page to convert.",
    )
    page_end: int | None = Field(
        default=None,
        ge=1,
        description="Optional last PDF page to convert.",
    )


def _public_document_converter(mcp_tool: Any) -> Any:
    from langchain_core.tools import StructuredTool

    async def aconvert(
        uri: str,
        pages: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> Any:
        if not uri.startswith(("http://", "https://")):
            return "Error: only public http(s) document URLs are allowed."
        args: dict[str, Any] = {"uri": uri}
        if pages:
            args["pages"] = pages
        if page_start is not None:
            args["page_start"] = page_start
        if page_end is not None:
            args["page_end"] = page_end
        timeout = getattr(settings, "PUBLIC_CHAT_MCP_TOOL_TIMEOUT_SECONDS", 30)
        return await asyncio.wait_for(mcp_tool.ainvoke(args), timeout=timeout)

    def convert(
        uri: str,
        pages: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> Any:
        return async_to_sync(aconvert)(uri, pages, page_start, page_end)

    return StructuredTool.from_function(
        func=convert,
        coroutine=aconvert,
        name="convert_to_markdown",
        description=(
            "Convert a public http(s) document or web page to Markdown. For PDFs, "
            "use pages, page_start, or page_end to convert only relevant pages. "
            "This public chat wrapper does not allow local files or output paths."
        ),
        args_schema=PublicDocumentConversionInput,
    )


def _validate_mcp_server_lifecycle(servers: dict[str, Any]) -> None:
    if settings.DEBUG:
        return
    stdio_servers = [
        name
        for name, config in servers.items()
        if isinstance(config, dict) and config.get("transport") == "stdio"
    ]
    if stdio_servers:
        raise PublicChatMCPError(
            "Production public chat MCP must use a managed non-stdio transport."
        )


def _should_cache_tools(servers: dict[str, Any]) -> bool:
    return bool(servers) and all(
        isinstance(config, dict) and config.get("transport") != "stdio"
        for config in servers.values()
    )


def _tool_cache_key(servers: dict[str, Any], allowed_tools: frozenset[str]) -> str:
    payload = {
        "servers": servers,
        "allowed_tools": sorted(allowed_tools),
    }
    return json.dumps(payload, sort_keys=True, default=str)
