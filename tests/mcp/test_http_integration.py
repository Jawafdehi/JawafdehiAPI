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
    _protected_resource_metadata,
    _resource_metadata_url,
)
from jawafdehi_mcp.identity import (
    ANONYMOUS_TOOL_NAMES,
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
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", "query-only-token")
        scope = _make_scope([])
        await mcp_server._handle_http(scope, _dummy_receive, _SendRecorder())
        assert captured["identity"] is None
        assert captured["bearer"] is None
        assert captured["transport"] == "http"
        assert captured["allowed_tools"] == ANONYMOUS_TOOL_NAMES

    async def test_no_bearer_hides_sql_without_query_token(
        self, mcp_server, captured, monkeypatch
    ):
        monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)

        await mcp_server._handle_http(
            _make_scope([]),
            _dummy_receive,
            _SendRecorder(),
        )

        assert captured["allowed_tools"] == ANONYMOUS_TOOL_NAMES - {
            "ngm_query_judicial"
        }

    @pytest.mark.security
    async def test_anonymous_cannot_reach_the_document_converter(
        self, mcp_server, captured, monkeypatch
    ):
        """convert_to_markdown needs authentication; no /api/ route gates it."""
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", "query-only-token")

        await mcp_server._handle_http(
            _make_scope([]),
            _dummy_receive,
            _SendRecorder(),
        )

        assert "convert_to_markdown" not in captured["allowed_tools"]

    async def test_roleless_bearer_reaches_the_document_converter(
        self, mcp_server, captured, monkeypatch
    ):
        """...but any verified bearer does, with no role required."""

        async def _resolve(token):
            return {"sub": "u2", "email": "noroles@x.org", "roles": []}

        monkeypatch.setattr(http_server, "resolve_bearer_identity", _resolve)

        await mcp_server._handle_http(
            _make_scope([(b"authorization", b"Bearer good-token")]),
            _dummy_receive,
            _SendRecorder(),
        )

        assert "convert_to_markdown" in captured["allowed_tools"]

    @pytest.mark.security
    async def test_spoofed_mode_header_has_no_effect(
        self, mcp_server, captured, monkeypatch
    ):
        """The doors are gone: x-mcp-mode is now just an unknown header."""
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", "query-only-token")

        await mcp_server._handle_http(
            _make_scope([(b"x-mcp-mode", b"internal")]),
            _dummy_receive,
            _SendRecorder(),
        )

        assert captured["identity"] is None
        assert captured["allowed_tools"] == ANONYMOUS_TOOL_NAMES

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

    async def test_protected_resource_metadata(self, mcp_server, monkeypatch):
        monkeypatch.setenv("OIDC_ISSUER", "https://auth.x.org")
        monkeypatch.setenv("OIDC_API_AUDIENCE", "proj-1")
        # Required now: the single door advertises one canonical resource URL.
        # There is no longer an audience-identifier fallback, which RFC 9728 did
        # not permit anyway (``resource`` must be the resource's URL).
        monkeypatch.setenv("OIDC_RESOURCE", "https://api.jawafdehi.org/mcp")
        send = _SendRecorder()
        scope = _make_scope(
            [],
            path="/.well-known/oauth-protected-resource",
            method="GET",
        )
        await mcp_server._handle_http(scope, _dummy_receive, send)
        assert send.status == 200
        meta = json.loads(send.body)
        assert meta["resource"] == "https://api.jawafdehi.org/mcp"
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
    def test_resource_is_the_url_never_the_audience(self, monkeypatch):
        """RFC 9728 `resource` is a URL; the old audience fallback is gone.

        It was unreachable anyway — the only caller 503s on a falsy base — but it
        was still documented and asserted, which is how a non-conformant document
        would have come back.
        """
        monkeypatch.setenv("OIDC_API_AUDIENCE", "aud-1")
        monkeypatch.setenv("OIDC_ISSUER", "https://iss.x.org")
        meta = _protected_resource_metadata("https://api.jawafdehi.org/mcp")
        assert meta["resource"] == "https://api.jawafdehi.org/mcp"
        assert meta["resource"] != "aud-1"
        assert meta["bearer_methods_supported"] == ["header"]

    def test_host_aware_resource_and_scopes(self, monkeypatch):
        monkeypatch.setenv("OIDC_API_AUDIENCE", "proj-9")
        monkeypatch.setenv("OIDC_ISSUER", "https://auth.x.org")
        meta = _protected_resource_metadata("https://api.jawafdehi.org/mcp")
        assert meta["resource"] == "https://api.jawafdehi.org/mcp"
        # Design 1a: point at Zitadel directly.
        assert meta["authorization_servers"] == ["https://auth.x.org"]
        # Refresh + project-audience scopes advertised.
        assert "offline_access" in meta["scopes_supported"]
        assert "urn:zitadel:iam:org:project:id:proj-9:aud" in meta["scopes_supported"]


class TestHeaderHelpers:
    def test_no_mode_resolver_exists(self):
        """The two doors are gone, and so is anything that resolved between them."""
        assert not hasattr(http_server, "_mode_from_headers")
        assert not hasattr(http_server, "MODE_HEADER")
        assert not hasattr(http_server, "MCP_DEFAULT_MODE")
        assert not hasattr(http_server, "VALID_MODES")

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


class TestSingleDoor:
    """One endpoint, no header-selected doors.

    The behavioral pivot from the two-door design: an anonymous request is
    SERVED the read-only catalog rather than challenged, so the metadata document
    below becomes the only way a client discovers how to authenticate.
    """

    pytestmark = pytest.mark.asyncio(loop_scope="function")

    async def test_anonymous_is_served_not_challenged(self, mcp_server, captured):
        send = _SendRecorder()
        await mcp_server._handle_http(_make_scope([]), _dummy_receive, send)
        # Reached the session manager instead of short-circuiting on a 401.
        assert send.status is None
        assert captured["identity"] is None

    async def test_metadata_is_always_served(self, mcp_server, monkeypatch):
        monkeypatch.setenv("OIDC_API_AUDIENCE", "proj-1")
        monkeypatch.setenv("OIDC_ISSUER", "https://auth.x.org")
        monkeypatch.setenv("OIDC_RESOURCE", "https://api.jawafdehi.org/mcp")
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope(
                [],
                path="/.well-known/oauth-protected-resource",
                method="GET",
            ),
            _dummy_receive,
            send,
        )
        assert send.status == 200
        meta = json.loads(send.body)
        assert meta["resource"] == "https://api.jawafdehi.org/mcp"
        assert meta["authorization_servers"] == ["https://auth.x.org"]

    async def test_metadata_requires_canonical_resource(self, mcp_server, monkeypatch):
        monkeypatch.delenv("OIDC_RESOURCE", raising=False)
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope(
                [(b"x-forwarded-host", b"evil.example")],
                path="/.well-known/oauth-protected-resource",
                method="GET",
            ),
            _dummy_receive,
            send,
        )
        assert send.status == 503
        assert json.loads(send.body)["error"] == "server_configuration_error"
        assert b"evil.example" not in send.body

    @pytest.mark.security
    async def test_metadata_ignores_spoofed_host(self, mcp_server, monkeypatch):
        monkeypatch.setenv("OIDC_RESOURCE", "https://api.jawafdehi.org/mcp")
        monkeypatch.setenv("OIDC_ISSUER", "https://auth.x.org")
        monkeypatch.setenv("OIDC_API_AUDIENCE", "proj-1")
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope(
                [(b"x-forwarded-host", b"evil.example")],
                path="/.well-known/oauth-protected-resource",
                method="GET",
            ),
            _dummy_receive,
            send,
        )
        assert json.loads(send.body)["resource"] == "https://api.jawafdehi.org/mcp"

    @pytest.mark.parametrize(
        "host",
        [
            b"10.42.3.17:8080",  # k8s httpGet defaults Host to the pod IP
            b"jawafdehi-api-7d9f.default.svc",
            b"portal.jawafdehi.org",  # some other name fronting the same pod
        ],
    )
    async def test_health_answers_any_host(self, mcp_server, monkeypatch, host):
        """A readiness probe must not have to satisfy the Host allowlist.

        The regression this guards: the Host check used to run first, so a k8s
        probe (Host = pod IP) got 421 and the pod was never marked ready. Invisible
        locally, because the Dockerfile and compose healthchecks both use localhost.
        """
        monkeypatch.setenv("OIDC_RESOURCE", "https://api.jawafdehi.org/mcp")
        mcp_server._ready = True
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([(b"host", host)], path="/health", method="GET"),
            _dummy_receive,
            send,
        )
        assert send.status == 200
        assert send.body == b"ready"

    @pytest.mark.security
    async def test_host_allowlist_still_guards_the_protocol_endpoint(self, mcp_server):
        """Exempting /health must not have exempted anything else."""
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([(b"host", b"10.42.3.17:8080")]),
            _dummy_receive,
            send,
        )
        assert send.status == 421

    async def test_health_rejects_non_get(self, mcp_server):
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([(b"host", b"10.42.3.17:8080")], path="/health", method="POST"),
            _dummy_receive,
            send,
        )
        assert send.status == 405

    async def test_health_does_not_depend_on_oidc_resource(
        self, mcp_server, monkeypatch
    ):
        """A pod serving anonymous reads is ready even with OAuth unconfigured."""
        monkeypatch.delenv("OIDC_RESOURCE", raising=False)
        mcp_server._ready = True
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([], path="/health", method="GET"),
            _dummy_receive,
            send,
        )
        assert send.status == 200
        assert send.body == b"ready"

    async def test_invalid_token_challenge_points_at_metadata(
        self, mcp_server, monkeypatch
    ):
        """A broken bearer is still rejected — and told where to authenticate."""
        monkeypatch.setenv("OIDC_RESOURCE", "https://api.jawafdehi.org/mcp")

        async def _resolve(token):
            raise OIDCError("expired")

        monkeypatch.setattr(http_server, "resolve_bearer_identity", _resolve)
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([(b"authorization", b"Bearer stale")]),
            _dummy_receive,
            send,
        )
        assert send.status == 401
        challenge = dict(
            (k.decode(), v.decode())
            for k, v in next(
                m["headers"]
                for m in send.messages
                if m["type"] == "http.response.start"
            )
        )["www-authenticate"]
        assert 'error="invalid_token"' in challenge
        assert "resource_metadata=" in challenge
        assert "api.jawafdehi.org" in challenge

    @pytest.mark.security
    @pytest.mark.parametrize(
        "authorization",
        [b"Token abc", b"Bearer", b"Bearer   ", b"garbage", b"Basic dXNlcjpwdw=="],
    )
    async def test_malformed_authorization_is_rejected_not_downgraded(
        self, mcp_server, monkeypatch, authorization
    ):
        """A caller that tried to authenticate is told it failed.

        The regression this guards: ``_bearer_from_headers`` returns None for
        both "absent" and "unparseable", so without an explicit check a wrong
        scheme silently received the ANONYMOUS catalog and a 200.
        """
        monkeypatch.setenv("OIDC_RESOURCE", "https://api.jawafdehi.org/mcp")
        send = _SendRecorder()
        await mcp_server._handle_http(
            _make_scope([(b"authorization", authorization)]),
            _dummy_receive,
            send,
        )
        assert send.status == 401
        assert json.loads(send.body)["error"] == "invalid_request"

    async def test_absent_authorization_is_still_anonymous(self, mcp_server, captured):
        """The other half: no header at all is anonymous, not an error."""
        send = _SendRecorder()
        await mcp_server._handle_http(_make_scope([]), _dummy_receive, send)
        assert send.status is None
        assert captured["identity"] is None

    async def test_valid_token_proceeds(self, mcp_server, captured, monkeypatch):
        monkeypatch.setenv("OIDC_RESOURCE", "https://api.jawafdehi.org/mcp")
        identity = {"sub": "u1", "email": "cw@x.org", "roles": ["contributor"]}

        async def _resolve(token):
            return identity

        monkeypatch.setattr(http_server, "resolve_bearer_identity", _resolve)
        await mcp_server._handle_http(
            _make_scope([(b"authorization", b"Bearer good")]),
            _dummy_receive,
            _SendRecorder(),
        )
        assert captured["identity"] == identity
        assert captured["allowed_tools"] == ALL_TOOL_NAMES
