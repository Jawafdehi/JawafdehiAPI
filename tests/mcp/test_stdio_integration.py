"""End-to-end contract for the retained local stdio MCP transport."""

import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jawafdehi_mcp.server import ALL_TOOL_NAMES


@pytest.mark.asyncio
async def test_stdio_subprocess_initialize_list_and_call():
    project_root = Path(__file__).resolve().parents[2]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jawafdehi_mcp.server"],
        cwd=project_root,
        env={
            # A configured local service bearer unlocks the complete stdio
            # catalog. The local date call does not send it over the network.
            "JAWAFDEHI_API_TOKEN": "stdio-contract-test",
            "STRUCTLOG_CONSOLE": "0",
        },
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool(
                "convert_date",
                {"dates": ["2023-01-15"], "mode": "ad_to_bs"},
            )

    assert initialized.serverInfo.name == "jawafdehi-mcp"
    assert {tool.name for tool in listed.tools} == ALL_TOOL_NAMES
    assert result.isError is False
    assert "Converted AD 2023-01-15 to BS" in result.content[0].text
