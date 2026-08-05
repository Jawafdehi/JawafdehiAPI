"""Integration coverage for MCP running inside the platform ASGI process."""

import json

import pytest
from asgiref.testing import ApplicationCommunicator
from mcp.shared.version import LATEST_PROTOCOL_VERSION

from config.asgi import PlatformASGIApplication, application, django_application
from jawafdehi_mcp import __version__
from jawafdehi_mcp import http_server
from jawafdehi_mcp.api_transport import (
    configure_embedded_api,
    embedded_api_client_kwargs,
)
from jawafdehi_mcp.configuration import get_oidc_config
from jawafdehi_mcp.request_context import current_transport
from jawafdehi_mcp.tools.nes import GetNESEntityPrefixesTool
from search.service import SearchService

MCP_HEADERS = [(b"accept", b"application/json, text/event-stream")]


async def _asgi_request(
    method,
    path,
    payload=None,
    extra_headers=None,
    asgi_application=application,
):
    body = json.dumps(payload).encode() if payload is not None else b""
    headers = [(b"host", b"testserver"), *MCP_HEADERS]
    headers.extend(extra_headers or [])
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
    communicator = ApplicationCommunicator(
        asgi_application,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
    )
    await communicator.send_input(
        {"type": "http.request", "body": body, "more_body": False}
    )
    start = await communicator.receive_output(timeout=5)
    chunks = []
    while True:
        message = await communicator.receive_output(timeout=5)
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await communicator.wait(timeout=5)
    return start["status"], b"".join(chunks)


@pytest.mark.asyncio
async def test_composite_asgi_serves_django_and_mcp(monkeypatch):
    monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
    monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", "http://testserver")
    monkeypatch.setenv("OIDC_RESOURCE", "https://api.example/mcp")
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example")
    monkeypatch.setenv("OIDC_API_AUDIENCE", "test-project")
    monkeypatch.setattr(
        SearchService,
        "search",
        lambda self, **kwargs: {
            "count": 0,
            "counts": {},
            "results": [],
            "page": 1,
            "page_size": kwargs["page_size"],
            "next_cursor": None,
        },
    )

    lifespan = ApplicationCommunicator(
        application,
        {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": {},
        },
    )
    await lifespan.send_input({"type": "lifespan.startup"})
    assert (await lifespan.receive_output(timeout=5))["type"] == (
        "lifespan.startup.complete"
    )
    try:
        mcp_health = await _asgi_request("GET", "/mcp/health")
        api_health = await _asgi_request(
            "GET",
            "/api/health",
            extra_headers=[(b"x-mcp-mode", b"internal")],
        )
        metadata = await _asgi_request(
            "GET",
            "/.well-known/oauth-protected-resource/mcp",
            extra_headers=[(b"x-mcp-mode", b"internal")],
        )
        initialize = await _asgi_request(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            },
        )
        tools = await _asgi_request(
            "POST",
            "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        date_call = await _asgi_request(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "convert_date",
                    "arguments": {
                        "dates": ["2023-01-15"],
                        "mode": "ad_to_bs",
                    },
                },
            },
        )
        control_plane_call = await _asgi_request(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_control_plane",
                    "arguments": {"q": "accountability"},
                },
            },
        )
        traversal_call = await _asgi_request(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "browse_court_data",
                    "arguments": {
                        "action": "get_court",
                        "court": "%252e%252e",
                    },
                },
            },
        )
    finally:
        await lifespan.send_input({"type": "lifespan.shutdown"})
        assert (await lifespan.receive_output(timeout=5))["type"] == (
            "lifespan.shutdown.complete"
        )
        await lifespan.wait(timeout=5)

    assert mcp_health[0] == 200
    assert json.loads(api_health[1]) == {"status": "ok", "service": "jawafdehi-api"}
    assert application._is_mcp_request({"path": "/mcp/not-a-route"}) is False
    assert json.loads(metadata[1])["resource"] == ("https://api.example/mcp")
    assert initialize[0] == 200
    server_info = json.loads(initialize[1])["result"]["serverInfo"]
    assert server_info["name"] == "jawafdehi-mcp"
    assert server_info["version"] == __version__

    tool_names = {tool["name"] for tool in json.loads(tools[1])["result"]["tools"]}
    assert "search_jawafdehi_cases" in tool_names
    assert "create_jawafdehi_case" not in tool_names
    assert "convert_to_markdown" not in tool_names
    assert "search_control_plane" in tool_names

    assert (
        "Converted AD 2023-01-15 to BS"
        in (json.loads(date_call[1])["result"]["content"][0]["text"])
    )
    control_result = json.loads(
        json.loads(control_plane_call[1])["result"]["content"][0]["text"]
    )
    assert control_result["success"] is True
    assert control_result["status_code"] == 200
    assert control_result["data"]["count"] == 0
    traversal_envelope = json.loads(traversal_call[1])["result"]
    assert traversal_envelope["isError"] is True
    traversal_result = json.loads(traversal_envelope["content"][0]["text"])
    assert traversal_result["success"] is False
    assert "traversal" in traversal_result["error"]


@pytest.mark.asyncio
async def test_roleless_bearer_gets_catalog_but_django_rejects_write(monkeypatch):
    class EmptyGroups:
        def values_list(self, *args, **kwargs):
            return []

        def filter(self, *args, **kwargs):
            return self

        def exists(self):
            return False

    class RolelessUser:
        is_authenticated = True
        is_superuser = False
        is_staff = False
        username = "roleless"
        groups = EmptyGroups()

    async def resolve_identity(token):
        assert token == "verified-token"
        return {"sub": "roleless", "roles": []}

    monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", "http://testserver")
    monkeypatch.setenv("OIDC_RESOURCE", "https://api.example/mcp")
    monkeypatch.setattr(http_server, "resolve_bearer_identity", resolve_identity)
    monkeypatch.setattr(
        "jawafdehi_shared.auth.oidc.OIDCAuthentication.authenticate",
        lambda self, request: (RolelessUser(), {"sub": "roleless"}),
    )

    roleless_application = PlatformASGIApplication(
        django_application,
        http_server.JawafdehiMCPServer(stateless=True),
    )
    lifespan = ApplicationCommunicator(
        roleless_application,
        {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": {},
        },
    )
    await lifespan.send_input({"type": "lifespan.startup"})
    assert (await lifespan.receive_output(timeout=5))["type"] == (
        "lifespan.startup.complete"
    )
    try:
        response = await _asgi_request(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "manage_material",
                    "arguments": {
                        "action": "delete",
                        "iri": "https://jawafdehi.org/material/court/test",
                    },
                },
            },
            extra_headers=[
                (b"x-mcp-mode", b"internal"),
                (b"authorization", b"Bearer verified-token"),
            ],
            asgi_application=roleless_application,
        )
    finally:
        await lifespan.send_input({"type": "lifespan.shutdown"})
        assert (await lifespan.receive_output(timeout=5))["type"] == (
            "lifespan.shutdown.complete"
        )
        await lifespan.wait(timeout=5)

    tool_envelope = json.loads(response[1])["result"]
    assert tool_envelope["isError"] is True
    tool_result = json.loads(tool_envelope["content"][0]["text"])
    assert tool_result["success"] is False
    assert tool_result["status_code"] == 403


@pytest.mark.asyncio
async def test_anonymous_http_tool_does_not_forward_service_token(monkeypatch):
    seen = {}

    async def fake_django(scope, receive, send):
        seen["path"] = scope["path"]
        seen["authorization"] = dict(scope["headers"]).get(b"authorization")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"prefixes": [{"prefix": "person"}]}).encode(),
            }
        )

    configure_embedded_api(fake_django)
    transport_token = current_transport.set("http")
    monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", "http://testserver")
    monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "service-token")
    try:
        result = await GetNESEntityPrefixesTool().execute({})
    finally:
        current_transport.reset(transport_token)
        configure_embedded_api(django_application)

    assert json.loads(result[0].text)["prefixes"][0]["prefix"] == "person"
    assert seen == {
        "path": "/api/entity_prefixes",
        "authorization": None,
    }


def test_non_http_transport_keeps_remote_client_behavior():
    token = current_transport.set("stdio")
    try:
        assert embedded_api_client_kwargs() == {}
    finally:
        current_transport.reset(token)


def test_mcp_oidc_config_falls_back_to_django(monkeypatch, settings):
    monkeypatch.delenv("OIDC_API_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
    settings.OIDC_AUDIENCE = ["project-a", "project-b"]
    settings.OIDC_JWKS_URI = "https://auth.example/oauth/v2/keys"

    assert get_oidc_config("OIDC_API_AUDIENCE") == ["project-a", "project-b"]
    assert get_oidc_config("OIDC_JWKS_URL") == settings.OIDC_JWKS_URI


def test_mcp_oidc_config_does_not_override_django_with_legacy_env(
    monkeypatch, settings
):
    settings.OIDC_ISSUER = "https://authoritative.example"
    settings.OIDC_AUDIENCE = "authoritative-project"
    settings.OIDC_JWKS_URI = "https://authoritative.example/keys"
    monkeypatch.setenv("OIDC_ISSUER", "https://legacy-override.example")
    monkeypatch.setenv("OIDC_API_AUDIENCE", "legacy-project")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://legacy-override.example/keys")

    assert get_oidc_config("OIDC_ISSUER") == settings.OIDC_ISSUER
    assert get_oidc_config("OIDC_API_AUDIENCE") == settings.OIDC_AUDIENCE
    assert get_oidc_config("OIDC_JWKS_URL") == settings.OIDC_JWKS_URI
