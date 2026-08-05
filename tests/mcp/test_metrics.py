"""Prometheus instrumentation for embedded MCP tool execution."""

import pytest
from mcp.types import CallToolResult

from jawafdehi_mcp.identity import current_user_identity
from jawafdehi_mcp.request_context import current_transport
from jawafdehi_mcp.server import TOOL_CALLS, TOOL_DURATION, call_tool


def _sample_value(metric, sample_name, labels):
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == sample_name and sample.labels == labels:
                return sample.value
    return 0


@pytest.mark.asyncio
async def test_completed_tool_call_records_count_and_duration():
    labels = {"tool": "convert_date", "outcome": "completed"}
    duration_labels = {"tool": "convert_date"}
    count_before = _sample_value(
        TOOL_CALLS,
        "jawafdehi_mcp_tool_calls_total",
        labels,
    )
    duration_count_before = _sample_value(
        TOOL_DURATION,
        "jawafdehi_mcp_tool_duration_seconds_count",
        duration_labels,
    )

    identity_token = current_user_identity.set(None)
    transport_token = current_transport.set("http")
    try:
        result = await call_tool(
            "convert_date",
            {"dates": ["2023-01-15"], "mode": "ad_to_bs"},
        )
    finally:
        current_transport.reset(transport_token)
        current_user_identity.reset(identity_token)

    assert "Converted AD 2023-01-15 to BS" in result[0].text
    assert (
        _sample_value(
            TOOL_CALLS,
            "jawafdehi_mcp_tool_calls_total",
            labels,
        )
        == count_before + 1
    )
    assert (
        _sample_value(
            TOOL_DURATION,
            "jawafdehi_mcp_tool_duration_seconds_count",
            duration_labels,
        )
        == duration_count_before + 1
    )


@pytest.mark.asyncio
async def test_error_result_records_failed_outcome():
    labels = {"tool": "convert_date", "outcome": "failed"}
    count_before = _sample_value(
        TOOL_CALLS,
        "jawafdehi_mcp_tool_calls_total",
        labels,
    )

    identity_token = current_user_identity.set(None)
    transport_token = current_transport.set("http")
    try:
        result = await call_tool("convert_date", {})
    finally:
        current_transport.reset(transport_token)
        current_user_identity.reset(identity_token)

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert "required parameters" in result.content[0].text
    assert (
        _sample_value(
            TOOL_CALLS,
            "jawafdehi_mcp_tool_calls_total",
            labels,
        )
        == count_before + 1
    )
