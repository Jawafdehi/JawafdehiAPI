"""Middleware to repair literal unicode escape sequences in filesystem tool calls."""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

_UNICODE_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")

_WATCHED_ARGS: dict[str, list[str]] = {
    "edit_file": ["old_string", "new_string"],
    "write_file": ["content"],
}


def _repair_unicode_escapes(value: str) -> tuple[str, bool]:
    """Decode literal ``\\uXXXX`` escape sequences to actual Unicode."""
    if not _UNICODE_ESCAPE_RE.search(value):
        return value, False
    repaired = value.encode("raw_unicode_escape").decode("unicode_escape")
    return repaired, True


def _maybe_repair(request: ToolCallRequest) -> ToolCallRequest:
    """Return a request with repaired tool arguments when needed."""
    tool_name = request.tool_call["name"]
    watched = _WATCHED_ARGS.get(tool_name)
    if not watched:
        return request

    args = request.tool_call["args"]
    repaired_args = dict(args)
    any_repaired = False

    for field in watched:
        value = args.get(field)
        if not isinstance(value, str):
            continue
        fixed, changed = _repair_unicode_escapes(value)
        if changed:
            repaired_args[field] = fixed
            any_repaired = True
            logger.warning(
                "Decoded literal unicode escapes in %s.%s (tool_call_id=%s)",
                tool_name,
                field,
                request.tool_call.get("id"),
            )

    if not any_repaired:
        return request

    return request.override(tool_call={**request.tool_call, "args": repaired_args})


class UnicodeEscapeRepairMiddleware(AgentMiddleware):
    """Repair double-escaped unicode in `edit_file` and `write_file` args."""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return handler(_maybe_repair(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        return await handler(_maybe_repair(request))
