"""Integration tests for the jawafdehi-mcp HTTP server bearer auth.

Exercises the ASGI middleware: bearer extraction, OIDC verification (mocked),
ContextVar lifecycle, 401 on bad tokens, anonymous fallback, and the health /
protected-resource-metadata endpoints.
"""

import json

import pytest

from jawafdehi_mcp import http_server
from jawafdehi_mcp.http_server import (
    JawafdehiMCPServer,
    _bearer_from_headers,
    _canonical_base_url,
    _max_request_body_size,
    _mode_from_headers,
    _protected_resource_metadata,
    _resource_metadata_url,
)
from jawafdehi_mcp.identity import (
    PUBLIC_HOST_TOOL_NAMES,
    current_request_mode,
    current_user_identity,
)
from jawafdehi_mcp.oidc import OIDCError
from jawafdehi_mcp.request_context import current_transport, jawafdehi_bearer_token
from jawafdehi_mcp.server import ALL_TOOL_NAMES, _get_allowed_tools


def _make_scope(headers=None, path="/", method="POST"):
    headers = list(headers or [])
    if not any(name.lower() == b"host" for name, _value in headers):
        headers.insert(0, (b"host", b"testserver"))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "http_version": "1.1",
        "headers": headers,
        "query_string": b"",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 12345),
    }


async def _dummy_receive():
    return {"type": "http.disconnect"}


class _SendRecorder:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self):
        for m in self.messages:
            if m["type"] == "http.response.body":
                return m["body"]
        return b""


@pytest.fixture
def mcp_server():
    return JawafdehiMCPServer()


@pytest.fixture
def captured(mcp_server, monkeypatch):
    """Replace handle_request with a recorder of the in-request context."""
    seen = {}

    async def _recorder(scope, receive, send):
        seen["identity"] = current_user_identity.get()
        seen["bearer"] = jawafdehi_bearer_token.get()
        seen["mode"] = current_request_mode.get()
        seen["transport"] = current_transport.get()
        seen["allowed_tools"] = {tool.name for tool in _get_allowed_tools()}

    monkeypatch.setattr(mcp_server.session_manager, "handle_request", _recorder)
    return seen


class TestBearerHelper:
    def test_extracts_bearer(self):
        assert _bearer_from_headers({b"authorization": b"Bearer abc"}) == "abc"

    def test_ignores_non_bearer(self):
        assert _bearer_from_headers({b"authorization": b"Token abc"}) is None

    def test_none_when_absent(self):
        assert _bearer_from_headers({}) is None


class TestMiddleware:
    pytestmark = pytest.mark.asyncio(loop_scope="function")

    async def test_valid_bearer_sets_identity_and_full_catalog(
        self, mcp_server, captured, monkeypatch
    ):
        identity = {"sub": "u1", "email": "a@x.org", "roles": []}

        async def _resolve(token):
            assert token == "good-token"
            return identity

        monkeypatch.setattr(http_server, "resolve_bearer_identity", _resolve)

        scope = _make_scope([(b"authorization", b"Bearer good-token")])
        await mcp_server._handle_http(scope, _dummy_receive, _SendRecorder())

        assert captured["identity"] == identity
        assert captured["bearer"] == "good-token"
        assert captured["transport"] == "http"
        assert captured["allowed_tools"] == ALL_TOOL_NAMES
        # contextvars reset after the request
        assert current_user_identity.get() is None
        assert current_transport.get() is None
        assert jawafdehi_bearer_token.get() is None

    async def test_invalid_bearer_returns_401(self, mcp_server, monkeypatch):
        async def _resolve(token):
            raise OIDCError("invalid token or signature")

        monkeypatch.setattr(http_server, "resolve_bearer_identity", _resolve)

        send = _SendRecorder()
        scope = _make_scope([(b"authorization", b"Bearer bad")])
        await mcp_server._handle_http(scope, _dummy_receive, send)

        assert send.status == 401
        assert json.loads(send.body)["error"] == "invalid_token"
        assert current_user_identity.get() is None

    async def test_no_bearer_is_anonymous(self, mcp_server, captured, monkeypatch):
        monkeypatch.setenv("MCP_DEFAULT_MODE", "public")
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", "query-only-token")
        scope = _make_scope([])
        await mcp_server._handle_http(scope, _dummy_receive, _SendRecorder())
        assert captured["identity"] is None
        assert captured["bearer"] is None
        assert captured["transport"] == "http"
        assert captured["allowed_tools"] == PUBLIC_HOST_TOOL_NAMES

    async def test_no_bearer_hides_sql_without_query_token(
        self, mcp_server, captured, monkeypatch
    ):
        monkeypatch.setenv("MCP_DEFAULT_MODE", "public")
        monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)

        await mcp_server._handle_http(
            _make_scope([]),
            _dummy_receive,
            _SendRecorder(),
        )

        assert captured["allowed_tools"] == PUBLIC_HOST_TOOL_NAMES - {
            "ngm_query_judicial"
        }

    @pytest.mark.security
    async def test_hostile_host_is_rejected(self, mcp_server):
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([(b"host", b"attacker.example")]),
            _dummy_receive,
            send,
        )
        assert send.status == 421

    @pytest.mark.security
    async def test_hostile_origin_is_rejected(self, mcp_server):
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([(b"origin", b"https://attacker.example")]),
            _dummy_receive,
            send,
        )
        assert send.status == 403

    @pytest.mark.parametrize(
        "host",
        [b"localhost", b"localhost:48000", b"127.0.0.1:48000", b"[::1]:48000"],
    )
    async def test_local_hosts_are_allowed(self, mcp_server, monkeypatch, host):
        called = False

        async def recorder(scope, receive, send):
            nonlocal called
            called = True

        monkeypatch.setattr(mcp_server.session_manager, "handle_request", recorder)
        await mcp_server._handle_http(
            _make_scope([(b"host", host)]),
            _dummy_receive,
            _SendRecorder(),
        )
        assert called is True

    async def test_canonical_host_and_origin_are_allowed(self, monkeypatch):
        monkeypatch.setenv("OIDC_RESOURCE", "https://mcp.example/mcp")
        server = JawafdehiMCPServer()
        called = False

        async def recorder(scope, receive, send):
            nonlocal called
            called = True

        monkeypatch.setattr(server.session_manager, "handle_request", recorder)
        await server._handle_http(
            _make_scope(
                [
                    (b"host", b"mcp.example"),
                    (b"origin", b"https://mcp.example"),
                ]
            ),
            _dummy_receive,
            _SendRecorder(),
        )
        assert called is True

    async def test_health_endpoint_reflects_lifespan_readiness(self, mcp_server):
        send = _SendRecorder()
        scope = _make_scope([], path="/health", method="GET")
        await mcp_server._handle_http(scope, _dummy_receive, send)
        assert send.status == 503

        mcp_server._ready = True
        send = _SendRecorder()
        await mcp_server._handle_http(scope, _dummy_receive, send)
        assert send.status == 200

    async def test_internal_health_requires_canonical_resource(
        self, mcp_server, monkeypatch
    ):
        monkeypatch.delenv("OIDC_RESOURCE", raising=False)
        monkeypatch.setenv("MCP_DEFAULT_MODE", "internal")
        mcp_server._ready = True
        send = _SendRecorder()

        await mcp_server._handle_http(
            _make_scope([], path="/health", method="GET"),
            _dummy_receive,
            send,
        )

        assert send.status == 503
        assert send.body == b"not ready"

    async def test_protected_resource_metadata(self, mcp_server, monkeypatch):
        monkeypatch.setenv("OIDC_ISSUER", "https://auth.x.org")
        monkeypatch.setenv("OIDC_API_AUDIENCE", "proj-1")
        send = _SendRecorder()
        scope = _make_scope(
            [],
            path="/.well-known/oauth-protected-resource",
            method="GET",
        )
        await mcp_server._handle_http(scope, _dummy_receive, send)
        assert send.status == 200
        meta = json.loads(send.body)
        assert meta["resource"] == "proj-1"
        assert meta["authorization_servers"] == ["https://auth.x.org"]

    @pytest.mark.parametrize(
        "path",
        ["/health", "/.well-known/oauth-protected-resource"],
    )
    async def test_auxiliary_endpoints_reject_non_get_methods(self, mcp_server, path):
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([], path=path, method="POST"),
            _dummy_receive,
            send,
        )
        assert send.status == 405

    async def test_lifespan_starts_and_stops_session_manager(self, mcp_server):
        messages = iter(
            [
                {"type": "lifespan.startup"},
                {"type": "lifespan.shutdown"},
            ]
        )
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await mcp_server._handle_lifespan({}, receive, send)

        assert [message["type"] for message in sent] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.complete",
        ]

    async def test_session_manager_is_stateless(self, mcp_server):
        assert mcp_server.session_manager.stateless is True


class TestProtectedResourceMetadata:
    def test_resource_defaults_to_audience(self, monkeypatch):
        monkeypatch.delenv("OIDC_RESOURCE", raising=False)
        monkeypatch.setenv("OIDC_API_AUDIENCE", "aud-1")
        monkeypatch.setenv("OIDC_ISSUER", "https://iss.x.org")
        meta = _protected_resource_metadata()
        assert meta["resource"] == "aud-1"
        assert meta["bearer_methods_supported"] == ["header"]

    def test_host_aware_resource_and_scopes(self, monkeypatch):
        monkeypatch.setenv("OIDC_API_AUDIENCE", "proj-9")
        monkeypatch.setenv("OIDC_ISSUER", "https://auth.x.org")
        meta = _protected_resource_metadata("https://mcp-internal.x.org")
        assert meta["resource"] == "https://mcp-internal.x.org"
        # Design 1a: point at Zitadel directly.
        assert meta["authorization_servers"] == ["https://auth.x.org"]
        # Refresh + project-audience scopes advertised.
        assert "offline_access" in meta["scopes_supported"]
        assert "urn:zitadel:iam:org:project:id:proj-9:aud" in meta["scopes_supported"]


class TestHeaderHelpers:
    def test_mode_from_headers(self):
        assert _mode_from_headers({b"x-mcp-mode": b"internal"}) == "internal"
        assert _mode_from_headers({b"x-mcp-mode": b"Public"}) == "public"
        assert _mode_from_headers({}) is None

    def test_mode_defaults_to_env_floor(self, monkeypatch):
        monkeypatch.setenv("MCP_DEFAULT_MODE", "public")
        # Missing header falls back to the deployment floor...
        assert _mode_from_headers({}) == "public"
        # ...but an explicit ingress-injected stricter mode still wins.
        assert _mode_from_headers({b"x-mcp-mode": b"internal"}) == "internal"

    @pytest.mark.security
    def test_internal_default_cannot_be_downgraded_by_header(self, monkeypatch):
        monkeypatch.setenv("MCP_DEFAULT_MODE", "internal")
        assert _mode_from_headers({b"x-mcp-mode": b"public"}) == "internal"
        assert _mode_from_headers({b"x-mcp-mode": b"legacy"}) == "internal"
        assert _mode_from_headers({b"x-mcp-mode": b"\xff"}) == "internal"

    @pytest.mark.security
    def test_invalid_default_mode_fails_closed(self, monkeypatch):
        monkeypatch.setenv("MCP_DEFAULT_MODE", "legacy")
        assert _mode_from_headers({}) == "public"

    def test_canonical_base_prefers_configured_over_client_host(self, monkeypatch):
        monkeypatch.setenv("OIDC_RESOURCE", "https://mcp-internal.jawafdehi.org/mcp")
        # A spoofed X-Forwarded-Host / Host must NOT steer the advertised URL.
        base = _canonical_base_url(
            {b"x-forwarded-host": b"evil.example", b"host": b"evil.example"}
        )
        assert base == "https://mcp-internal.jawafdehi.org/mcp"

    def test_canonical_base_ignores_host_when_unset(self, monkeypatch):
        monkeypatch.delenv("OIDC_RESOURCE", raising=False)
        base = _canonical_base_url({b"host": b"mcp-internal.x.org"})
        assert base is None

    def test_canonical_base_rejects_invalid_config(self, monkeypatch):
        monkeypatch.setenv("OIDC_RESOURCE", "https://user:pass@mcp.example/mcp")
        assert _canonical_base_url({}) is None

    def test_path_resource_uses_rfc9728_metadata_location(self):
        assert _resource_metadata_url("https://mcp-internal.x.org/mcp") == (
            "https://mcp-internal.x.org/.well-known/oauth-protected-resource/mcp"
        )

    def test_default_request_body_limit_covers_encoded_document(self, monkeypatch):
        monkeypatch.delenv("MCP_DOCUMENT_MAX_INPUT_BYTES", raising=False)
        monkeypatch.delenv("MCP_HTTP_MAX_REQUEST_BODY_BYTES", raising=False)
        assert _max_request_body_size() == 105_906_176

    def test_request_body_limit_tracks_document_limit(self, monkeypatch):
        monkeypatch.setenv("MCP_DOCUMENT_MAX_INPUT_BYTES", "10")
        monkeypatch.delenv("MCP_HTTP_MAX_REQUEST_BODY_BYTES", raising=False)
        assert _max_request_body_size() == 4 * 10 + 1024 * 1024


class TestModeDoors:
    pytestmark = pytest.mark.asyncio(loop_scope="function")

    async def test_internal_anonymous_gets_401_challenge(self, mcp_server, monkeypatch):
        monkeypatch.setenv("OIDC_RESOURCE", "https://mcp-internal.x.org/mcp")
        send = _SendRecorder()
        scope = _make_scope([(b"x-mcp-mode", b"internal")])
        await mcp_server._handle_http(scope, _dummy_receive, send)
        assert send.status == 401
        wa = dict(
            (k.decode(), v.decode())
            for k, v in next(
                m["headers"]
                for m in send.messages
                if m["type"] == "http.response.start"
            )
        )
        assert "resource_metadata=" in wa["www-authenticate"]
        assert "mcp-internal.x.org" in wa["www-authenticate"]

    async def test_internal_mode_requires_canonical_resource(
        self, mcp_server, monkeypatch
    ):
        monkeypatch.delenv("OIDC_RESOURCE", raising=False)
        send = _SendRecorder()
        scope = _make_scope(
            [
                (b"x-mcp-mode", b"internal"),
                (b"x-forwarded-host", b"evil.example"),
            ]
        )
        await mcp_server._handle_http(scope, _dummy_receive, send)
        assert send.status == 503
        assert json.loads(send.body)["error"] == "server_configuration_error"
        assert b"evil.example" not in send.body

    async def test_public_anonymous_proceeds(self, mcp_server, captured):
        scope = _make_scope([(b"x-mcp-mode", b"public")])
        await mcp_server._handle_http(scope, _dummy_receive, _SendRecorder())
        assert captured["identity"] is None
        assert captured["mode"] == "public"
        # contextvar reset after request
        assert current_request_mode.get() is None

    async def test_legacy_anonymous_proceeds(self, mcp_server, captured):
        scope = _make_scope([])
        await mcp_server._handle_http(scope, _dummy_receive, _SendRecorder())
        assert captured["identity"] is None
        assert captured["mode"] is None

    @pytest.mark.security
    async def test_invalid_mode_is_restricted_to_public(self, mcp_server, captured):
        scope = _make_scope([(b"x-mcp-mode", b"legacy")])
        await mcp_server._handle_http(scope, _dummy_receive, _SendRecorder())
        assert captured["identity"] is None
        assert captured["mode"] == "public"

    async def test_metadata_ignores_spoofed_host_when_configured(
        self, mcp_server, monkeypatch
    ):
        monkeypatch.setenv("OIDC_RESOURCE", "https://mcp-internal.jawafdehi.org/mcp")
        monkeypatch.setenv("OIDC_ISSUER", "https://auth.x.org")
        monkeypatch.setenv("OIDC_API_AUDIENCE", "proj-1")
        send = _SendRecorder()
        scope = _make_scope(
            [
                (b"x-mcp-mode", b"internal"),
                (b"x-forwarded-host", b"evil.example"),
            ],
            path="/.well-known/oauth-protected-resource",
            method="GET",
        )
        await mcp_server._handle_http(scope, _dummy_receive, send)
        meta = json.loads(send.body)
        assert meta["resource"] == "https://mcp-internal.jawafdehi.org/mcp"

    async def test_public_hides_oauth_metadata(self, mcp_server):
        send = _SendRecorder()
        scope = _make_scope(
            [(b"x-mcp-mode", b"public")],
            path="/.well-known/oauth-protected-resource",
            method="GET",
        )
        await mcp_server._handle_http(scope, _dummy_receive, send)
        assert send.status == 404

    async def test_internal_serves_canonical_metadata(self, mcp_server, monkeypatch):
        monkeypatch.setenv("OIDC_API_AUDIENCE", "proj-1")
        monkeypatch.setenv("OIDC_ISSUER", "https://auth.x.org")
        monkeypatch.setenv("OIDC_RESOURCE", "https://mcp-internal.x.org/mcp")
        send = _SendRecorder()
        scope = _make_scope(
            [(b"x-mcp-mode", b"internal")],
            path="/.well-known/oauth-protected-resource",
            method="GET",
        )
        await mcp_server._handle_http(scope, _dummy_receive, send)
        assert send.status == 200
        meta = json.loads(send.body)
        assert meta["resource"] == "https://mcp-internal.x.org/mcp"
        assert meta["authorization_servers"] == ["https://auth.x.org"]

    async def test_internal_valid_token_proceeds(
        self, mcp_server, captured, monkeypatch
    ):
        monkeypatch.setenv("OIDC_RESOURCE", "https://mcp-internal.x.org/mcp")
        identity = {"sub": "u1", "email": "cw@x.org", "roles": ["contributor"]}

        async def _resolve(token):
            return identity

        monkeypatch.setattr(http_server, "resolve_bearer_identity", _resolve)
        scope = _make_scope(
            [(b"x-mcp-mode", b"internal"), (b"authorization", b"Bearer good")]
        )
        await mcp_server._handle_http(scope, _dummy_receive, _SendRecorder())
        assert captured["identity"] == identity
        assert captured["mode"] == "internal"
