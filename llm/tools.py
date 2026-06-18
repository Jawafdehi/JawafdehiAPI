"""Provider-agnostic tool-use primitives for the llm package.

A `Tool` bundles a JSON-schema spec with a Python executor. `invoke_with_tools`
(see llm.invoke) runs the agentic loop on a provider that supports it (bedrock,
proxy). The CLI-harness providers (codex_cli/claude_cli) do NOT support tool-use
— they are their own agents with their own tool systems — so passing tools to a
CLI tier raises NotImplementedError.

Tools themselves (e.g. a Nepali date converter) live in the calling layer, not
here; this module only defines the spec + the loop's tool-dispatch helper.
"""

import json
from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    """A callable tool the model may invoke during `invoke_with_tools`.

    `input_schema` is a JSON Schema object describing the tool's arguments.
    `run` is a Python callable invoked as `run(**args)` returning a
    JSON-serialisable result.
    """

    name: str
    description: str
    input_schema: dict
    run: Callable[..., object]
    # Importable "module:function" for the same executor, used to expose this
    # tool to subprocess CLI harnesses (claude -p) via a stdio MCP server, which
    # can't receive the in-process `run` callable. Set it when the tool should be
    # callable under the CLI providers.
    run_path: str | None = None

    def to_anthropic(self) -> dict:
        """Anthropic / Bedrock Messages tool schema (flat name/description/input_schema)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_openai(self) -> dict:
        """OpenAI function-tool schema (nested type:function/function:{...})."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


def run_tool(tools, name, args) -> str:
    """Execute the named tool with parsed args; return a JSON-string result.

    Tool failures are returned as an "Error: ..." string (not raised) so the
    model can recover or proceed rather than aborting the whole tool loop.
    """
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return f"Error: unknown tool '{name}'"
    try:
        result = tool.run(**args) if isinstance(args, dict) else tool.run(args)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"
