"""Streamable HTTP transport for jawafdehi-mcp.

Wraps the MCP server with a Streamable HTTP transport using mcp's built-in
StreamableHTTPSessionManager, and authenticates each request as an OIDC
resource server: a verified ``Authorization: Bearer`` token resolves the
caller's identity and is forwarded to the embedded Django API. The HTTP
transport is hosted only by ``config.asgi:application``.
"""

import json
import os
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import INTERNAL_ERROR

from .configuration import get_oidc_config
from .identity import current_user_identity
from .oidc import OIDCError, resolve_bearer_identity
from .request_context import current_transport, jawafdehi_bearer_token
from .server import app as mcp_app

logger = structlog.get_logger()

# The three ASGI plumbing types, spelled out rather than imported from
# ``starlette.types``. mcp pulls starlette in transitively, but no first-party
# module imports it directly and this file should not be the reason it becomes a
# declared dependency (cf. the httpcore note in pyproject.toml, where a direct
# import IS declared). These are the ASGI spec's shapes and do not change.
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
#: ``(name, value)`` response headers, as this module builds them before encode.
Headers = Iterable[tuple[str, str]]

WELL_KNOWN_PROTECTED_RESOURCE = "/.well-known/oauth-protected-resource"
_LOCAL_ALLOWED_HOSTS = (
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
    "[::1]",
    "[::1]:*",
)
_MIB = 1024 * 1024


def _bearer_from_headers(headers: dict[bytes, bytes]) -> str | None:
    raw = headers.get(b"authorization", b"").decode(errors="replace").strip()
    if not raw:
        return None
    # Split on any run of whitespace (HTTP allows multiple spaces / tabs).
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


def _authorization_offered(headers: dict[bytes, bytes]) -> bool:
    """Whether the caller sent an ``Authorization`` header of any shape.

    Distinguishes "anonymous" from "tried to authenticate and got it wrong".
    ``_bearer_from_headers`` returns ``None`` for both, and with a single door
    those two must not be treated alike: an anonymous caller is served, while a
    malformed credential has to be told so.
    """
    return bool(headers.get(b"authorization", b"").strip())


def _bearer_challenge(error: str) -> str:
    """Build a ``WWW-Authenticate`` value, pointing at RFC 9728 metadata."""
    challenge = f'Bearer error="{error}"'
    rm_url = _resource_metadata_url(_canonical_base_url())
    if rm_url:
        challenge += f', resource_metadata="{rm_url}"'
    return challenge


def _canonical_base_url() -> str | None:
    """Absolute base URL of this MCP server for RFC 9728 metadata.

    Takes no request input on purpose. Only the trusted OIDC_RESOURCE
    configuration may define security metadata, so Host and forwarding headers
    cannot reach this decision rather than merely being ignored by it."""
    configured = (os.getenv("OIDC_RESOURCE") or "").strip()
    if not configured:
        return None
    parsed = urlsplit(configured)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return configured.rstrip("/")


def _positive_int_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


def _max_request_body_size() -> int:
    """Allow the configured raw document limit after URI/JSON encoding."""
    input_bytes = _positive_int_env(
        "MCP_DOCUMENT_MAX_INPUT_BYTES",
        25 * _MIB,
        100 * _MIB,
    )
    computed = input_bytes * 4 + _MIB
    return _positive_int_env(
        "MCP_HTTP_MAX_REQUEST_BODY_BYTES",
        computed,
        401 * _MIB,
    )


def _csv_env(name: str) -> list[str]:
    return [
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    ]


def _transport_security_settings() -> TransportSecuritySettings:
    hosts = set(_LOCAL_ALLOWED_HOSTS)
    origins = set(_csv_env("MCP_ALLOWED_ORIGINS"))
    hosts.update(_csv_env("MCP_ALLOWED_HOSTS"))

    if resource := _canonical_base_url():
        parsed = urlsplit(resource)
        hosts.add(parsed.netloc)
        origins.add(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))

    try:
        from django.conf import settings

        if settings.configured and getattr(settings, "TESTING", False):
            hosts.add("testserver")
    except ImportError:
        pass

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def _matches_allowed_header(value: str, allowed_values: list[str]) -> bool:
    if value in allowed_values:
        return True
    return any(
        allowed.endswith(":*")
        and value.startswith(f"{allowed[:-2]}:")
        for allowed in allowed_values
    )


def _resource_metadata_url(base_url: str | None) -> str:
    """Return the RFC 9728 metadata URL for a possibly path-based resource."""
    if not base_url:
        return ""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    resource_path = parsed.path.rstrip("/")
    metadata_path = WELL_KNOWN_PROTECTED_RESOURCE
    if resource_path:
        metadata_path = f"{metadata_path}{resource_path}"
    return urlunsplit((parsed.scheme, parsed.netloc, metadata_path, "", ""))


def _protected_resource_metadata(base_url: str) -> dict:
    """RFC 9728 Protected Resource Metadata so OAuth clients discover the
    authorization server (Zitadel, Design 1a) and the scopes to request.

    ``base_url`` is the absolute canonical URL of this MCP server (see
    :func:`_canonical_base_url`) and is REQUIRED. There is deliberately no
    fallback to OIDC_API_AUDIENCE: RFC 9728 defines ``resource`` as the
    resource's URL, and an audience identifier is not one. With a single endpoint
    there is exactly one canonical URL, so a caller that cannot supply it is a
    misconfigured deployment — which the sole caller reports as a 503 rather than
    advertising a document no client can use."""
    raw_audience = get_oidc_config("OIDC_API_AUDIENCE")
    if isinstance(raw_audience, (list, tuple)):
        audience = str(raw_audience[0]) if raw_audience else ""
    else:
        audience = str(raw_audience or "").strip()
    issuer = str(get_oidc_config("OIDC_ISSUER") or "").strip()
    resource = base_url
    # offline_access must be advertised for clients (e.g. Claude Code) to request
    # a refresh token; the project-aud urn puts our project id in the token
    # audience so the API + this server accept the bearer.
    scopes = ["openid", "email", "profile", "offline_access"]
    if audience:
        scopes.append(f"urn:zitadel:iam:org:project:id:{audience}:aud")
    return {
        "resource": resource,
        "authorization_servers": [issuer] if issuer else [],
        "bearer_methods_supported": ["header"],
        "scopes_supported": scopes,
    }


class _ResponseGuard:
    """Wrap ASGI ``send`` so an abandoned response can still be closed out.

    ``StreamableHTTPSessionManager`` runs the MCP server in a task on the
    session manager's task group and streams its output back over this ``send``.
    If that task dies — the tool raised something the protocol layer did not
    convert, or the task group was cancelled because the worker is shutting down
    mid-rollout — the transport's write stream closes and ``handle_request``
    returns *normally*, having started the response but never finished it.

    uvicorn then logs ``ASGI callable returned without completing response`` at
    ERROR and closes the socket. That message is what the client gets instead of
    an error: no status, no JSON-RPC error body, nothing naming the tool. It was
    the single largest real defect in the API's Sentry inbox (555 events across
    `tools/call list_document_sources` and `ngm_query_judicial`), and it carried
    no information, because by the time uvicorn notices, the context is gone.

    This records what reached the client so the caller can finish the response
    deliberately. It deliberately does not buffer or alter anything in flight.
    """

    __slots__ = ("_send", "started", "complete")

    def __init__(self, send: Send) -> None:
        self._send = send
        self.started = False
        self.complete = False

    async def __call__(self, message: MutableMapping[str, Any]) -> None:
        # Record only what actually went out. Setting the flags before the await
        # would mark a response complete that the send then failed to deliver
        # (a disconnected peer raises here), and the finisher's early-exit would
        # skip precisely the case it exists to catch.
        await self._send(message)
        message_type = message.get("type")
        if message_type == "http.response.start":
            self.started = True
        elif message_type == "http.response.body" and not message.get("more_body", False):
            self.complete = True


class JawafdehiMCPServer:
    """Minimal ASGI app wrapping StreamableHTTPSessionManager with per-request
    OIDC bearer authentication (see resolve_bearer_identity)."""

    def __init__(self, *, stateless: bool = True) -> None:
        self._ready = False
        self.security_settings = _transport_security_settings()
        self.session_manager = StreamableHTTPSessionManager(
            app=mcp_app,
            json_response=True,
            # The tools hold no protocol-session state. Stateless mode lets the
            # monolith retain multiple gunicorn workers and pod replicas without
            # requiring load-balancer affinity for MCP session IDs.
            stateless=stateless,
            security_settings=self.security_settings,
            max_request_body_size=_max_request_body_size(),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        elif scope["type"] == "http":
            await self._handle_http(scope, receive, send)

    @staticmethod
    async def _send_response(
        send: Send,
        status_code: int,
        headers: Headers,
        body: bytes = b"",
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [(k.encode(), v.encode()) for k, v in headers],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_json(
        self,
        send: Send,
        status_code: int,
        payload: dict[str, Any],
        extra_headers: Headers | None = None,
    ) -> None:
        body = json.dumps(payload).encode()
        headers = [("content-type", "application/json")]
        if extra_headers:
            headers.extend(extra_headers)
        await self._send_response(send, status_code, headers, body)

    async def _handle_lifespan(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Handle ASGI lifespan protocol."""
        started = False
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                logger.info("http_server_starting")
                self._lifespan_ctx = self.session_manager.run()
                try:
                    await self._lifespan_ctx.__aenter__()
                except BaseException as exc:
                    self._ready = False
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": str(exc),
                        }
                    )
                    raise
                started = True
                self._ready = True
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                self._ready = False
                if started:
                    await self._lifespan_ctx.__aexit__(None, None, None)
                logger.info("http_server_stopped")
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _handle_http(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Authenticate the request's bearer token, then delegate to MCP.

        One door, one rule. A verified bearer resolves an identity and receives
        the full catalog; an anonymous request proceeds with the read-only
        anonymous catalog (see ``identity.ANONYMOUS_TOOL_NAMES``) rather than a
        401, because every tool in that set wraps an ``AllowAny`` REST route that
        ``/api/`` already serves anonymously. A bearer that is *present but
        invalid* is still rejected with a challenge — that is a broken caller,
        not an anonymous one.

        Consequence worth knowing: MCP clients begin an OAuth flow only on a
        401, so an anonymous client is never prompted to log in. It gets the
        read-only catalog, and ``get_current_user`` is how it can tell. The
        protected-resource metadata below is what makes authenticating
        discoverable.
        """
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        headers = dict(scope.get("headers", []))

        # Readiness is answered BEFORE the Host allowlist, deliberately. A
        # Kubernetes httpGet probe defaults Host to the pod IP, which is not in
        # MCP_ALLOWED_HOSTS and should not have to be — validating it here would
        # 421 every probe and the pod would never be marked ready. That would
        # also contradict the rule just below, where a missing OIDC_RESOURCE is
        # deliberately kept from failing the probe.
        #
        # Safe because this endpoint takes no credential, reads no request data,
        # and returns one of two fixed strings. DNS-rebinding protection exists
        # to keep a browser off the MCP *protocol* endpoint, which is still
        # guarded below.
        if path == "/health":
            if method != "GET":
                await self._send_response(
                    send,
                    405,
                    [("content-type", "text/plain"), ("allow", "GET")],
                    b"method not allowed",
                )
                return
            # Lifespan readiness only. A missing OIDC_RESOURCE is NOT unready:
            # the anonymous read catalog serves correctly without it, and failing
            # the probe would take down a pod that is doing its job. The
            # misconfiguration surfaces on the metadata endpoint, which 503s, and
            # on every authenticated call.
            ready = self._ready
            await self._send_response(
                send,
                200 if ready else 503,
                [("content-type", "text/plain")],
                b"ready" if ready else b"not ready",
            )
            return

        host = headers.get(b"host", b"").decode(errors="replace").strip()
        if not _matches_allowed_header(
            host,
            self.security_settings.allowed_hosts,
        ):
            await self._send_response(
                send,
                421,
                [("content-type", "text/plain")],
                b"invalid host header",
            )
            return
        origin = headers.get(b"origin", b"").decode(errors="replace").strip()
        if origin and not _matches_allowed_header(
            origin,
            self.security_settings.allowed_origins,
        ):
            await self._send_response(
                send,
                403,
                [("content-type", "text/plain")],
                b"invalid origin header",
            )
            return

        if path == WELL_KNOWN_PROTECTED_RESOURCE and method != "GET":
            await self._send_response(
                send,
                405,
                [("content-type", "text/plain"), ("allow", "GET")],
                b"method not allowed",
            )
            return
        if path == WELL_KNOWN_PROTECTED_RESOURCE:
            # Always advertised. This is now the only way a client discovers how
            # to authenticate, since an anonymous request is not challenged.
            base = _canonical_base_url()
            if not base:
                await self._send_json(
                    send,
                    503,
                    {
                        "error": "server_configuration_error",
                        "detail": "OIDC_RESOURCE is required to serve MCP metadata.",
                    },
                )
                return
            await self._send_json(send, 200, _protected_resource_metadata(base))
            return

        token = _bearer_from_headers(headers)

        # A credential we cannot parse is a malformed request, not an anonymous
        # one (RFC 6750 §3.1 `invalid_request`). Without this branch a wrong
        # scheme or an empty `Bearer` would fall through to the anonymous catalog
        # with a 200, and the caller would never learn its header was wrong —
        # exactly the silent degradation this door already risks.
        if token is None and _authorization_offered(headers):
            await self._send_json(
                send,
                401,
                {
                    "error": "invalid_request",
                    "detail": "Authorization header is not a Bearer credential.",
                },
                extra_headers=[
                    ("www-authenticate", _bearer_challenge("invalid_request"))
                ],
            )
            return

        token_ctx = None
        identity_ctx = None

        if token:
            try:
                identity = await resolve_bearer_identity(token)
            except OIDCError as exc:
                await self._send_json(
                    send,
                    401,
                    {"error": "invalid_token", "detail": str(exc)},
                    extra_headers=[
                        ("www-authenticate", _bearer_challenge("invalid_token"))
                    ],
                )
                return
            token_ctx = jawafdehi_bearer_token.set(token)
            identity_ctx = current_user_identity.set(identity)

        transport_ctx = current_transport.set("http")
        guard = _ResponseGuard(send)
        try:
            await self.session_manager.handle_request(scope, receive, guard)
        except Exception as exc:  # noqa: BLE001 - see _finish_abandoned_response
            # Deliberately not re-raised. Letting it reach uvicorn produces
            # either a bare 500 with no MCP framing or, once the stream has
            # started, a closed socket — in both cases the client is told
            # nothing useful. Report it here, where the request is still in
            # scope, and answer the caller properly below.
            logger.exception(
                "mcp_request_failed",
                path=path,
                method=method,
                error=str(exc) or type(exc).__name__,
            )
            await self._finish_abandoned_response(
                guard, reason="handler raised", path=path, method=method
            )
        else:
            await self._finish_abandoned_response(
                guard, reason="handler returned early", path=path, method=method
            )
        finally:
            current_transport.reset(transport_ctx)
            if identity_ctx is not None:
                current_user_identity.reset(identity_ctx)
            if token_ctx is not None:
                jawafdehi_bearer_token.reset(token_ctx)

    async def _finish_abandoned_response(
        self,
        guard: _ResponseGuard,
        *,
        reason: str,
        path: str = "",
        method: str = "",
    ) -> None:
        """Complete a response the MCP transport started but did not finish.

        A no-op on the overwhelming majority of requests, which complete
        normally. When it is not a no-op it converts an opaque uvicorn error and
        a truncated stream into a JSON-RPC error the client can act on.

        This is the HTTP framing only. It is deliberately NOT the whole story on
        the "handler returned early" path, where the MCP server task died inside
        ``StreamableHTTPSessionManager``'s task group: that task's exception is
        caught there and logged by ``mcp.server.streamable_http_manager`` as
        ``"Stateless session crashed"``, with the traceback. Django's LOGGING
        sets ``disable_existing_loggers: False`` and a root handler at INFO, so
        that record reaches both the console and Sentry as its own event —
        which is where the failing tool is actually named. Correlate the two by
        timestamp and pod; do not expect this message alone to tell you which
        tool broke.
        """
        if guard.complete:
            return

        # ERROR, not WARNING, and that is load-bearing: sentry_sdk's
        # LoggingIntegration reports at `logging.ERROR` and turns anything
        # lower into a breadcrumb. The dominant production signature takes the
        # "handler returned early" path, where there is no exception to report,
        # so this log IS the whole signal. Downgrading it would close the
        # 555-event issue by making the condition invisible rather than by
        # fixing it — which is the opposite of the point.
        logger.error(
            "mcp_response_abandoned",
            reason=reason,
            started=guard.started,
            path=path,
            method=method,
        )

        # Both sends below can themselves fail: the peer may be gone, which is
        # one of the reasons the response was abandoned in the first place.
        # Letting that escape would replace the original failure with an
        # opaque "Exception in ASGI application" — exactly what this is here to
        # prevent.
        try:
            if not guard.started:
                # Nothing reached the client yet, so a complete JSON-RPC error
                # can still be framed. id is null per JSON-RPC 2.0 for a request
                # whose id could not be determined.
                await self._send_json(
                    guard,
                    500,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": INTERNAL_ERROR,
                            "message": (
                                "MCP request failed before a response was produced."
                            ),
                        },
                    },
                )
                return
            # Headers are already on the wire, so the status cannot be changed.
            # End the body cleanly rather than letting uvicorn drop the socket:
            # a client reading a truncated stream cannot tell a server fault
            # from a network one, and will often retry a call that already ran.
            await guard({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception as exc:  # noqa: BLE001 - the peer is gone; nothing left to do
            logger.warning(
                "mcp_response_finish_failed",
                reason=reason,
                error=str(exc) or type(exc).__name__,
            )
