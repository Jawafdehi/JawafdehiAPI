"""MCP server for Jawafdehi and NGM judicial data queries."""

import time
import uuid
from typing import Any

import structlog
import structlog.contextvars
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
from prometheus_client import Counter, Histogram

from . import __version__
from .identity import (
    current_request_mode,
    current_user_identity,
    get_allowed_tool_names,
)
from .request_context import (
    current_transport,
    get_local_service_token,
    get_query_service_token,
)

from .tools import (
    BaseTool,
    BrowseCourtDataTool,
    BrowseMaterialsTool,
    CreateJawafdehiCaseTool,
    DateConverterTool,
    DeleteJawafdehiCaseTool,
    DeleteNESEntityTool,
    DocumentConverterTool,
    GetCurrentUserTool,
    GetJawafdehiCaseTool,
    GetNESEntityPrefixesTool,
    GetNESEntityVersionsTool,
    ManageCaseUpdateProposalsTool,
    ManageCaseworkReviewsTool,
    ManageCourtDataTool,
    ManageJobsTool,
    ManageMaterialTool,
    NGMExtractCaseDataTool,
    NGMJudicialTool,
    PatchJawafdehiCaseTool,
    SearchControlPlaneTool,
    SearchJawafdehiCasesTool,
    SubmitNESChangeTool,
    ToolExecutionResult,
    UploadMaterialFileTool,
)
from .tools.nes import (
    GetNESEntitiesTool,
    GetNESTagsTool,
    SearchNESEntitiesTool,
)

logger = structlog.get_logger()

app = Server("jawafdehi-mcp", version=__version__)

TOOL_CALLS = Counter(
    "jawafdehi_mcp_tool_calls_total",
    "MCP tool calls completed by outcome.",
    ("tool", "outcome"),
)
TOOL_DURATION = Histogram(
    "jawafdehi_mcp_tool_duration_seconds",
    "MCP tool execution time in seconds.",
    ("tool",),
)

TOOLS: list[BaseTool] = [
    GetCurrentUserTool(),
    SearchControlPlaneTool(),
    NGMJudicialTool(),
    NGMExtractCaseDataTool(),
    SearchJawafdehiCasesTool(),
    GetJawafdehiCaseTool(),
    CreateJawafdehiCaseTool(),
    PatchJawafdehiCaseTool(),
    DeleteJawafdehiCaseTool(),
    SubmitNESChangeTool(),
    UploadMaterialFileTool(),
    SearchNESEntitiesTool(),
    GetNESEntitiesTool(),
    GetNESEntityPrefixesTool(),
    GetNESTagsTool(),
    GetNESEntityVersionsTool(),
    DeleteNESEntityTool(),
    BrowseMaterialsTool(),
    ManageMaterialTool(),
    BrowseCourtDataTool(),
    ManageCourtDataTool(),
    ManageCaseUpdateProposalsTool(),
    ManageCaseworkReviewsTool(),
    ManageJobsTool(),
    DateConverterTool(),
    DocumentConverterTool(),
]

TOOL_MAP = {tool.name: tool for tool in TOOLS}
ALL_TOOL_NAMES: set[str] = set(TOOL_MAP.keys())


def _has_api_token() -> bool:
    """Whether a local stdio client may use the configured service token."""
    return get_local_service_token() is not None


def _get_runtime_allowed_tool_names() -> set[str]:
    identity = current_user_identity.get()
    if identity is None and _has_api_token():
        return ALL_TOOL_NAMES

    mode = current_request_mode.get()
    allowed = get_allowed_tool_names(identity, ALL_TOOL_NAMES, mode)
    if identity is None and get_query_service_token() is None:
        allowed.discard("ngm_query_judicial")
    return allowed


def _get_allowed_tools() -> list[BaseTool]:
    allowed = _get_runtime_allowed_tool_names()
    return [tool for tool in TOOLS if tool.name in allowed]


def _is_tool_allowed(name: str) -> bool:
    return name in _get_runtime_allowed_tool_names()


def _bind_audit_context(identity: dict | None) -> None:
    if identity:
        structlog.contextvars.bind_contextvars(
            jawafdehi_user_sub=str(identity.get("sub", "")),
            jawafdehi_user_email=identity.get("email", ""),
            jawafdehi_roles=identity.get("roles", []),
        )


def _unbind_audit_context() -> None:
    structlog.contextvars.unbind_contextvars(
        "jawafdehi_user_sub",
        "jawafdehi_user_email",
        "jawafdehi_roles",
    )


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools based on the current user's identity."""
    return [tool.to_tool() for tool in _get_allowed_tools()]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent] | CallToolResult:
    """Handle tool execution requests with per-user authorization."""
    if not _is_tool_allowed(name):
        identity = current_user_identity.get()
        user_info = ""
        if identity:
            user_info = (
                f" (email={identity.get('email')}, roles={identity.get('roles')})"
            )
        raise ValueError(
            f"Tool '{name}' is not available for the current user{user_info}. "
            "Authenticate with a bearer token to access it."
        )

    tool = TOOL_MAP.get(name)
    if not tool:
        logger.error("unknown_tool_requested", tool_name=name)
        raise ValueError(f"Unknown tool: {name}")

    identity = current_user_identity.get()
    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    outcome = "completed"
    structlog.contextvars.bind_contextvars(request_id=request_id)
    _bind_audit_context(identity)
    logger.info("tool_call_started", tool_name=name)
    try:
        result = await tool.execute(arguments)
        if isinstance(result, ToolExecutionResult):
            if result.is_error:
                outcome = "failed"
            return CallToolResult(
                content=list(result),
                isError=result.is_error,
            )
        return result
    except Exception:
        outcome = "failed"
        logger.exception("tool_execution_failed", tool_name=name)
        raise
    finally:
        TOOL_CALLS.labels(tool=name, outcome=outcome).inc()
        TOOL_DURATION.labels(tool=name).observe(time.perf_counter() - started)
        _unbind_audit_context()
        structlog.contextvars.unbind_contextvars("request_id")


def main() -> None:
    """Run the MCP server via stdio."""
    from .logging_setup import setup_logging

    setup_logging()
    logger.info("server_starting")

    import asyncio

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            transport_ctx = current_transport.set("stdio")
            try:
                await app.run(
                    read_stream, write_stream, app.create_initialization_options()
                )
            finally:
                current_transport.reset(transport_ctx)

    asyncio.run(run())


if __name__ == "__main__":
    main()
