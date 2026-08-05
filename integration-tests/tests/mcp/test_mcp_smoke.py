"""Live contract checks for MCP on the shared platform process."""

import json
import os

import pytest

MCP_HEADERS = {"accept": "application/json, text/event-stream"}
MCP_E2E_BEARER = os.getenv("MCP_E2E_BEARER", "e2e-ngm-query-only")
ANONYMOUS_TOOL_CATALOG = {
    "get_current_user",
    "search_control_plane",
    "ngm_query_judicial",
    "search_jawafdehi_cases",
    "get_jawafdehi_case",
    "search_nes_entities",
    "get_nes_entities",
    "get_nes_entity_prefixes",
    "get_nes_tags",
    "get_nes_entity_versions",
    "browse_materials",
    "browse_court_data",
    "convert_date",
}
FULL_TOOL_CATALOG = {
    "get_current_user",
    "search_control_plane",
    "ngm_query_judicial",
    "ngm_extract_case_data",
    "search_jawafdehi_cases",
    "get_jawafdehi_case",
    "create_jawafdehi_case",
    "patch_jawafdehi_case",
    "delete_jawafdehi_case",
    "submit_nes_change",
    "upload_material_file",
    "search_nes_entities",
    "get_nes_entities",
    "get_nes_entity_prefixes",
    "get_nes_tags",
    "get_nes_entity_versions",
    "delete_nes_entity",
    "browse_materials",
    "manage_material",
    "browse_court_data",
    "manage_court_data",
    "manage_case_update_proposals",
    "manage_casework_reviews",
    "manage_jobs",
    "convert_date",
    "convert_to_markdown",
}


@pytest.mark.live
@pytest.mark.smoke
def test_mcp_health_and_protocol(clients):
    client = clients["platform"]

    health = client.get("/mcp/health")
    assert health.status_code == 200

    initialize = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "integration-tests", "version": "1.0"},
            },
        },
    )
    assert initialize.status_code == 200, initialize.text
    assert initialize.json()["result"]["serverInfo"]["name"] == "jawafdehi-mcp"

    listed = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200, listed.text
    tool_names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert tool_names == ANONYMOUS_TOOL_CATALOG

    hidden = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "create_jawafdehi_case",
                "arguments": {},
            },
        },
    )
    assert hidden.status_code == 200, hidden.text
    hidden_result = hidden.json()["result"]
    assert hidden_result["isError"] is True
    assert "not available for the current user" in hidden_result["content"][0]["text"]

    query = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "ngm_query_judicial",
                "arguments": {
                    "query": (
                        "SELECT identifier FROM courts "
                        "ORDER BY identifier LIMIT 1"
                    )
                },
            },
        },
    )
    assert query.status_code == 200, query.text
    tool_payload = query.json()["result"]["content"][0]["text"]
    result = json.loads(tool_payload)
    assert result["success"] is True, result
    assert result["data"]["row_count"] == 1


@pytest.mark.live
@pytest.mark.smoke
def test_authenticated_mcp_catalog_and_forwarded_authorization(clients):
    client = clients["platform"]
    headers = {
        **MCP_HEADERS,
        "authorization": f"Bearer {MCP_E2E_BEARER}",
    }

    listed = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200, listed.text
    tool_names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert tool_names == FULL_TOOL_CATALOG

    query = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "ngm_query_judicial",
                "arguments": {
                    "query": (
                        "SELECT identifier FROM courts "
                        "ORDER BY identifier LIMIT 1"
                    )
                },
            },
        },
    )
    assert query.status_code == 200, query.text
    query_result = json.loads(query.json()["result"]["content"][0]["text"])
    assert query_result["success"] is True, query_result
    assert query_result["data"]["row_count"] == 1

    denied_write = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "manage_material",
                "arguments": {
                    "action": "delete",
                    "iri": "https://jawafdehi.org/material/court/not-present",
                },
            },
        },
    )
    assert denied_write.status_code == 200, denied_write.text
    assert denied_write.json()["result"]["isError"] is True
    write_result = json.loads(
        denied_write.json()["result"]["content"][0]["text"]
    )
    assert write_result["success"] is False
    assert write_result["status_code"] == 401
