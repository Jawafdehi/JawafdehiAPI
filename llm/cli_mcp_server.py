"""Minimal stdio MCP server that exposes a fixed set of tools to a CLI harness.

`claude -p` (Claude Code) can call custom tools only through MCP, not via the
in-process tool callback the API providers use. ClaudeCliProvider.invoke_with_tools
launches this module as an MCP server (`--mcp-config`) so the model can call the
exact tools passed to invoke_with_tools — and only those.

No third-party deps: it speaks newline-delimited JSON-RPC 2.0 (the MCP stdio
framing) directly. Tools are described by a registry JSON file (env
LLM_CLI_TOOLS_REGISTRY) of objects: {name, description, input_schema, run_path},
where run_path is "module:function" executed as function(**arguments).
"""

import importlib
import json
import os
import sys

PROTOCOL_VERSION = "2024-11-05"


def _load_registry():
    for path in (os.environ.get("LLM_CLI_TOOLS_PYPATH") or "").split(os.pathsep):
        if path and path not in sys.path:
            sys.path.insert(0, path)
    with open(os.environ["LLM_CLI_TOOLS_REGISTRY"]) as f:
        specs = json.load(f)
    tools = {}
    for spec in specs:
        mod_name, func_name = spec["run_path"].split(":", 1)
        func = getattr(importlib.import_module(mod_name), func_name)
        tools[spec["name"]] = {"spec": spec, "func": func}
    return tools


def _send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def main():
    tools = _load_registry()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        req_id = req.get("id")
        if req_id is None:  # notification (e.g. notifications/initialized)
            continue
        if method == "initialize":
            client_ver = (req.get("params") or {}).get("protocolVersion")
            _result(
                req_id,
                {
                    "protocolVersion": client_ver or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "llmtools", "version": "0.1.0"},
                },
            )
        elif method == "ping":
            _result(req_id, {})
        elif method == "tools/list":
            _result(
                req_id,
                {
                    "tools": [
                        {
                            "name": t["spec"]["name"],
                            "description": t["spec"]["description"],
                            "inputSchema": t["spec"]["input_schema"],
                        }
                        for t in tools.values()
                    ]
                },
            )
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            entry = tools.get(name)
            if entry is None:
                _result(
                    req_id,
                    {
                        "content": [{"type": "text", "text": f"unknown tool '{name}'"}],
                        "isError": True,
                    },
                )
                continue
            try:
                out = entry["func"](**args)
                text = json.dumps(out, ensure_ascii=False)
                is_error = False
            except Exception as exc:  # noqa: BLE001
                text = f"Error: {exc}"
                is_error = True
            _result(
                req_id,
                {"content": [{"type": "text", "text": text}], "isError": is_error},
            )
        else:
            _error(req_id, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
