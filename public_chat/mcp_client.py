from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings


class PublicChatMCPError(RuntimeError):
    pass


_TOOL_CACHE: dict[str, dict[str, Any]] = {}


class PublicChatMCPClient:
    """Small MCP facade for caller allow-listed tools."""

    def __init__(self, *, allowed_tools: Iterable[str] | None = None) -> None:
        self.allowed_tools = frozenset(allowed_tools or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.allowed_tools:
            raise PublicChatMCPError(f"Tool {name} is not allowed for public chat")
        return async_to_sync(self._call_tool)(name, arguments)

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
        tool = tool_map.get(name)
        if tool is None:
            available_tools = ", ".join(sorted(tool_map)) or "none"
            raise PublicChatMCPError(
                f"MCP tool {name} was not found. "
                f"Available public MCP tools: {available_tools}"
            )

        try:
            raw_result = await self._invoke_tool(tool, arguments)
        except Exception:
            if _should_cache_tools(servers):
                _clear_tool_cache(servers, self.allowed_tools)
                tool_map = await self._load_tool_map(MultiServerMCPClient, servers)
                tool = tool_map.get(name)
                if tool is None:
                    raise
                raw_result = await self._invoke_tool(tool, arguments)
            else:
                raise
        return self._parse_tool_result(raw_result)

    async def _load_tool_map(
        self, client_cls, servers: dict[str, Any]
    ) -> dict[str, Any]:
        # Keep tool objects scoped to this async call. LangChain MCP stdio tools can
        # own event-loop/process resources, so a global tool-object cache is unsafe
        # under Django request concurrency.
        if _should_cache_tools(servers):
            cache_key = _tool_cache_key(servers, self.allowed_tools)
            cached = _TOOL_CACHE.get(cache_key)
            if cached and cached["expires_at"] > time.monotonic():
                return cached["tools"]

        client = client_cls(servers)
        tools = await client.get_tools()
        for tool in tools:
            tool.handle_tool_error = True
        tool_map = {
            tool.name: tool for tool in tools if tool.name in self.allowed_tools
        }
        if _should_cache_tools(servers):
            _TOOL_CACHE[_tool_cache_key(servers, self.allowed_tools)] = {
                "expires_at": time.monotonic()
                + getattr(settings, "PUBLIC_CHAT_MCP_TOOL_CACHE_SECONDS", 300),
                "tools": tool_map,
            }
        return tool_map

    async def _invoke_tool(self, tool: Any, arguments: dict[str, Any]) -> Any:
        timeout = getattr(settings, "PUBLIC_CHAT_MCP_TOOL_TIMEOUT_SECONDS", 30)
        return await asyncio.wait_for(tool.ainvoke(arguments), timeout=timeout)

    def _parse_tool_result(self, raw_result: Any) -> dict[str, Any]:
        if isinstance(raw_result, dict):
            if "structuredContent" in raw_result:
                return raw_result["structuredContent"]
            content = raw_result.get("content")
            if content is None:
                return raw_result
            if isinstance(content, str):
                return self._parse_json_text(content)
            if isinstance(content, list):
                raw_result = content
        if isinstance(raw_result, str):
            return self._parse_json_text(raw_result)
        if isinstance(raw_result, list) and raw_result:
            first_item = raw_result[0]
            text = (
                first_item.get("text")
                if isinstance(first_item, dict)
                else getattr(first_item, "text", None)
            )
            if text is not None:
                return self._parse_json_text(text)
        text = getattr(raw_result, "content", None) or getattr(raw_result, "text", None)
        if text:
            return self._parse_json_text(text)
        raise PublicChatMCPError("Unexpected MCP tool response")

    def _parse_json_text(self, text: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            message = str(text).strip() or "MCP tool returned a non-JSON response"
            if message.lower().startswith("error"):
                raise PublicChatMCPError(message[:500])
            return {"text": message}


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


def _clear_tool_cache(servers: dict[str, Any], allowed_tools: frozenset[str]) -> None:
    _TOOL_CACHE.pop(_tool_cache_key(servers, allowed_tools), None)
