"""Tests for unicode escape repair middleware."""

from __future__ import annotations

from unittest.mock import AsyncMock

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from case_workflows.unicode_repair_middleware import (
    UnicodeEscapeRepairMiddleware,
    _maybe_repair,
    _repair_unicode_escapes,
)


def _request(
    tool_name: str, args: dict, tool_call_id: str = "tool-1"
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id},
        tool=None,
        state={},
        runtime=None,
    )


class TestRepairHelpers:
    def test_repair_unicode_escapes_decodes_literal_escape_sequences(self):
        repaired, changed = _repair_unicode_escapes(
            r"\u0928\u092e\u0938\u094d\u0924\u0947"
        )

        assert repaired == "नमस्ते"
        assert changed is True

    def test_repair_unicode_escapes_leaves_utf8_text_unchanged(self):
        repaired, changed = _repair_unicode_escapes("नमस्ते")

        assert repaired == "नमस्ते"
        assert changed is False

    def test_maybe_repair_changes_edit_file_args(self):
        request = _request(
            "edit_file",
            {
                "file_path": "/tmp/example.md",
                "old_string": r"\u0928\u092e",
                "new_string": r"\u0938\u094d\u0935\u093e\u0917\u0924",
            },
        )

        repaired = _maybe_repair(request)

        assert repaired is not request
        assert repaired.tool_call["args"]["old_string"] == "नम"
        assert repaired.tool_call["args"]["new_string"] == "स्वागत"
        assert request.tool_call["args"]["old_string"] == r"\u0928\u092e"

    def test_maybe_repair_changes_write_file_content_only(self):
        request = _request(
            "write_file",
            {
                "file_path": "/tmp/example.md",
                "content": r"Header: \u0928\u092e\u0938\u094d\u0924\u0947",
            },
        )

        repaired = _maybe_repair(request)

        assert repaired.tool_call["args"]["content"] == "Header: नमस्ते"

    def test_maybe_repair_ignores_other_tools(self):
        request = _request("read_file", {"file_path": "/tmp/example.md"})

        repaired = _maybe_repair(request)

        assert repaired is request


class TestUnicodeEscapeRepairMiddleware:
    def test_wrap_tool_call_passes_repaired_request_to_handler(self):
        middleware = UnicodeEscapeRepairMiddleware()
        request = _request(
            "edit_file",
            {
                "file_path": "/tmp/example.md",
                "old_string": r"\u0928\u092e",
                "new_string": "hello",
            },
        )
        captured = {}

        def handler(inner_request: ToolCallRequest):
            captured["request"] = inner_request
            return ToolMessage(content="ok", tool_call_id="tool-1", name="edit_file")

        result = middleware.wrap_tool_call(request, handler)

        assert result.content == "ok"
        assert captured["request"].tool_call["args"]["old_string"] == "नम"

    async def test_awrap_tool_call_passes_repaired_request_to_handler(self):
        middleware = UnicodeEscapeRepairMiddleware()
        request = _request(
            "write_file",
            {
                "file_path": "/tmp/example.md",
                "content": r"\u0928\u092e\u0938\u094d\u0924\u0947",
            },
        )
        handler = AsyncMock(
            return_value=ToolMessage(
                content="ok", tool_call_id="tool-1", name="write_file"
            )
        )

        result = await middleware.awrap_tool_call(request, handler)

        assert result.content == "ok"
        forwarded_request = handler.await_args.args[0]
        assert forwarded_request.tool_call["args"]["content"] == "नमस्ते"
