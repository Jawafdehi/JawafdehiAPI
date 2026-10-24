"""Live contract checks for MCP on the shared platform process."""

import json
import os

import pytest

MCP_HEADERS = {"accept": "application/json, text/event-stream"}
MCP_E2E_BEARER = os.getenv("MCP_E2E_BEARER", "e2e-ngm-query-only")
FULL_TOOL_CATALOG = {
    # Document reading — authenticated only, like everything else here now.
    "list_document_sources",
    "get_document_overview",
    "list_document_contents",
    "read_document_part",
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
def test_mcp_health_and_anonymous_challenge(clients):
    """Against the real stack: no bearer, no protocol — and a usable challenge.

    The 401 is what makes an MCP client start its OAuth flow, so the two things
    worth proving end to end are that the refusal happens and that what comes
    back actually leads somewhere: a ``WWW-Authenticate`` pointing at metadata
    that is itself reachable unauthenticated.
    """
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
    assert initialize.status_code == 401, initialize.text
    assert initialize.json()["error"] == "unauthorized"

    challenge = initialize.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    # No error code for a caller that supplied nothing (RFC 6750 §3.1).
    assert "error=" not in challenge
    assert "resource_metadata=" in challenge

    metadata_url = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
    assert metadata_url.endswith("/.well-known/oauth-protected-resource/mcp")

    metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["authorization_servers"]

    listed = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 401, listed.text


@pytest.mark.live
@pytest.mark.smoke
def test_authenticated_mcp_catalog_and_forwarded_authorization(clients):
    client = clients["platform"]
    headers = {
        **MCP_HEADERS,
        "authorization": f"Bearer {MCP_E2E_BEARER}",
    }

    # The protocol handshake lives here now: it is the first thing a bearer
    # buys, and there is no unauthenticated path left that reaches it.
    initialize = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 9,
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
