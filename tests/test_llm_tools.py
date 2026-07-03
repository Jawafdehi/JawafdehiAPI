"""Tests for llm.tools / invoke_with_tools (the agentic tool-use loop)."""

import io
import json

from llm.providers.bedrock import BedrockProvider
from llm.providers.cli import ClaudeCliProvider, CodexCliProvider
from llm.tools import Tool, run_tool
from llm.usage import UsageAccumulator


def _add_one_tool(counter):
    def add_one(x):
        counter["n"] += 1
        return {"result": x + 1}

    return Tool(
        name="add_one",
        description="add 1 to x",
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
        run=add_one,
    )


def test_run_tool_dispatch_and_errors():
    counter = {"n": 0}
    tools = [_add_one_tool(counter)]
    assert json.loads(run_tool(tools, "add_one", {"x": 41})) == {"result": 42}
    assert run_tool(tools, "missing", {}).startswith("Error: unknown tool")
    assert run_tool(tools, "add_one", {"y": 1}).startswith("Error:")  # bad args


def test_tool_schema_translation():
    t = _add_one_tool({"n": 0})
    assert t.to_anthropic()["name"] == "add_one"
    assert "input_schema" in t.to_anthropic()
    openai = t.to_openai()
    assert openai["type"] == "function"
    assert openai["function"]["parameters"]["required"] == ["x"]


def test_bedrock_tool_loop_executes_then_answers():
    counter = {"n": 0}
    tool = _add_one_tool(counter)
    seq = iter(
        [
            {
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "add_one",
                        "input": {"x": 41},
                    }
                ],
            },
            {
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 20, "output_tokens": 3},
                "content": [{"type": "text", "text": "The answer is 42."}],
            },
        ]
    )

    class FakeClient:
        def invoke_model(self, modelId, body):
            return {"body": io.BytesIO(json.dumps(next(seq)).encode())}

    p = BedrockProvider()
    p._client = lambda: FakeClient()
    usage = UsageAccumulator()
    out = p.invoke_with_tools(
        "sys", "what is 41+1?", 1000, "model-x", "premium", [tool], usage, 8
    )
    assert out == "The answer is 42."
    assert counter["n"] == 1  # tool executed exactly once
    assert usage.calls == 2  # both model turns counted
    assert usage.input_tokens == 30


def test_claude_cli_provider_supports_tools():
    # claude_cli implements tool-use via a stdio MCP server (cli_mcp_server), so
    # it advertises support and the invoke layer routes tools to it. (Actually
    # launching it needs the claude binary, which CI lacks, so only the contract
    # flag is asserted here.)
    assert ClaudeCliProvider.supports_tools is True


def test_codex_cli_provider_rejects_tools():
    # codex_cli has no tool loop; it must raise so the invoke layer falls back to
    # a no-tool invoke_text instead of pretending to run tools.
    import pytest

    assert CodexCliProvider.supports_tools is False
    with pytest.raises(NotImplementedError):
        CodexCliProvider().invoke_with_tools(
            "s", "c", 100, "m", "premium", [_add_one_tool({"n": 0})]
        )
