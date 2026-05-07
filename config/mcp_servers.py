from __future__ import annotations

import json
import urllib.parse
from typing import Any, Iterable

JAWAFDEHI_MCP_SERVER_NAME = "jawafdehi"
JAWAFDEHI_MCP_PACKAGE = "git+https://github.com/Jawafdehi/jawafdehi-mcp.git"
JAWAFDEHI_MCP_ENTRYPOINT = "jawafdehi-mcp"


def build_jawafdehi_mcp_stdio_server(
    *,
    env: dict[str, str] | None = None,
    command: str = "uvx",
    args: Iterable[str] | None = None,
) -> dict[str, dict]:
    """Build the same stdio MCP server shape used by case workflows."""

    return {
        JAWAFDEHI_MCP_SERVER_NAME: {
            "command": command,
            "args": list(
                args
                or [
                    "--from",
                    JAWAFDEHI_MCP_PACKAGE,
                    JAWAFDEHI_MCP_ENTRYPOINT,
                ]
            ),
            "transport": "stdio",
            "env": dict(env or {}),
        }
    }


def build_public_chat_mcp_servers(
    *,
    api_base_url: str | None,
    servers_json: str | None = None,
    allow_default_stdio: bool = False,
) -> dict[str, dict]:
    if servers_json:
        return _parse_mcp_servers_json(servers_json)

    if not allow_default_stdio or not api_base_url:
        return {}

    env = {"JAWAFDEHI_API_BASE_URL": api_base_url}
    return build_jawafdehi_mcp_stdio_server(env=env)


def _parse_mcp_servers_json(raw_value: str) -> dict[str, dict]:
    parsed: Any = json.loads(raw_value)
    if not isinstance(parsed, dict):
        raise ValueError("PUBLIC_CHAT_MCP_SERVERS_JSON must be a JSON object.")

    for server_name, server_config in parsed.items():
        if not isinstance(server_name, str) or not server_name:
            raise ValueError(
                "PUBLIC_CHAT_MCP_SERVERS_JSON server names must be strings."
            )
        if not isinstance(server_config, dict):
            raise ValueError(
                "PUBLIC_CHAT_MCP_SERVERS_JSON server configs must be JSON objects."
            )
        if "transport" not in server_config:
            raise ValueError(
                "PUBLIC_CHAT_MCP_SERVERS_JSON server configs must include transport."
            )
        _validate_server_transport(server_name, server_config)

    return parsed


def _validate_server_transport(server_name: str, server_config: dict[str, Any]) -> None:
    transport = server_config.get("transport")
    if transport == "stdio":
        if (
            not isinstance(server_config.get("command"), str)
            or not server_config["command"]
        ):
            raise ValueError(f"{server_name} stdio MCP config requires command.")
        if not isinstance(server_config.get("args", []), list):
            raise ValueError(f"{server_name} stdio MCP config args must be a list.")
        env = server_config.get("env", {})
        if env is not None and not isinstance(env, dict):
            raise ValueError(f"{server_name} stdio MCP config env must be an object.")
        return

    if transport in {"streamable_http", "sse"}:
        url = server_config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"{server_name} {transport} MCP config requires url.")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{server_name} MCP url must be an absolute HTTP(S) URL.")
        headers = server_config.get("headers", {})
        if headers is not None and not isinstance(headers, dict):
            raise ValueError(f"{server_name} MCP headers must be an object.")
        return

    raise ValueError(
        f"{server_name} MCP transport must be one of: stdio, streamable_http, sse."
    )
