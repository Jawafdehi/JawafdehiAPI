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

from .configuration import get_oidc_config
from .identity import current_request_mode, current_user_identity
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
MCP_RESOURCE_PATH = "/mcp"
# Injected by the ingress (Traefik overwrites it, so a client value can't win):
#   "internal" -> OAuth-gated door; unauthenticated requests get a 401 challenge
#   "public"   -> anonymous door; restricted tools, OAuth never advertised
#   absent     -> falls back to MCP_DEFAULT_MODE (the per-deployment floor),
#                 else legacy/OWUI-facing in-cluster behavior (unchanged)
MODE_HEADER = b"x-mcp-mode"
# Per-deployment safe floor for the mode when the header is missing. Set to
# "public" on the internet-facing deployment so a stripped/absent header can
# never widen anonymous access to the fuller legacy read-only set (which
# includes OCR + SQL); unset for the in-cluster OWUI deploy (legacy behavior).
MODE_DEFAULT_ENV = "MCP_DEFAULT_MODE"
VALID_MODES = frozenset({"public", "internal"})
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


def _mode_from_headers(headers: dict[bytes, bytes]) -> str | None:
    """Resolve the door mode without weakening the deployment's mode floor."""
    default_raw = (os.getenv(MODE_DEFAULT_ENV) or "").strip().lower()
    if default_raw:
        default_mode = default_raw if default_raw in VALID_MODES else "public"
    else:
        default_mode = None

    # Internal is the stricter deployment posture. A request header may never
    # downgrade it to the anonymous public door, even when an ingress fails to
    # overwrite a client-supplied value.
    if default_mode == "internal":
        return "internal"

    raw = headers.get(MODE_HEADER, b"").decode(errors="replace").strip().lower()
    if raw:
        return raw if raw in VALID_MODES else "public"
    return default_mode


def _canonical_base_url(_headers: dict[bytes, bytes] | None = None) -> str | None:
    """Absolute base URL of this MCP server for RFC 9728 metadata.

    Only the trusted OIDC_RESOURCE configuration may define security metadata.
    Request Host and forwarding headers are intentionally never consulted."""
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


def _protected_resource_metadata(base_url: str | None = None) -> dict:
    """RFC 9728 Protected Resource Metadata so OAuth clients discover the
    authorization server (Zitadel, Design 1a) and the scopes to request.

    ``base_url`` is the absolute canonical URL of this MCP server (see
    _canonical_base_url); falls back to OIDC_API_AUDIENCE for hostless callers."""
    raw_audience = get_oidc_config("OIDC_API_AUDIENCE")
    if isinstance(raw_audience, (list, tuple)):
        audience = str(raw_audience[0]) if raw_audience else ""
    else:
        audience = str(raw_audience or "").strip()
    issuer = str(get_oidc_config("OIDC_ISSUER") or "").strip()
    resource = base_url or audience
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

        Behavior varies by the ingress-injected mode (see MODE_HEADER):
        ``internal`` challenges anonymous callers with a 401 so MCP clients
        start OAuth; ``public`` serves anonymous callers a restricted tool set
        and never advertises OAuth; absent = legacy behavior.
        """
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        headers = dict(scope.get("headers", []))
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
        mode = _mode_from_headers(headers)

        if path in {"/health", WELL_KNOWN_PROTECTED_RESOURCE} and method != "GET":
            await self._send_response(
                send,
                405,
                [("content-type", "text/plain"), ("allow", "GET")],
                b"method not allowed",
            )
            return
        if path == "/health":
            configured = mode != "internal" or bool(_canonical_base_url(headers))
            ready = self._ready and configured
            status_code = 200 if ready else 503
            body = b"ready" if ready else b"not ready"
            await self._send_response(
                send,
                status_code,
                [("content-type", "text/plain")],
                body,
            )
            return
        if path == WELL_KNOWN_PROTECTED_RESOURCE:
            if mode == "public":
                # The public door does not advertise OAuth.
                await self._send_response(
                    send, 404, [("content-type", "text/plain")], b"not found"
                )
                return
            base = _canonical_base_url(headers)
            if mode == "internal" and not base:
                await self._send_json(
                    send,
                    503,
                    {
                        "error": "server_configuration_error",
                        "detail": "OIDC_RESOURCE is required for internal MCP.",
                    },
                )
                return
            await self._send_json(send, 200, _protected_resource_metadata(base))
            return

        token = _bearer_from_headers(headers)

        if mode == "internal" and not _canonical_base_url(headers):
            await self._send_json(
                send,
                503,
                {
                    "error": "server_configuration_error",
                    "detail": "OIDC_RESOURCE is required for internal MCP.",
                },
            )
            return

        # Internal door: an anonymous request is challenged so the MCP client
        # begins the OAuth flow (clients only start auth on a 401/403).
        if not token and mode == "internal":
            rm_url = _resource_metadata_url(_canonical_base_url(headers))
            challenge = f'Bearer resource_metadata="{rm_url}"' if rm_url else "Bearer"
            await self._send_json(
                send,
                401,
                {"error": "unauthorized", "detail": "authentication required"},
                extra_headers=[("www-authenticate", challenge)],
            )
            return

        token_ctx = None
        identity_ctx = None
        transport_ctx = None

        if token:
            try:
                identity = await resolve_bearer_identity(token)
            except OIDCError as exc:
                challenge = 'Bearer error="invalid_token"'
                if mode == "internal":
                    rm_url = _resource_metadata_url(_canonical_base_url(headers))
                    if rm_url:
                        challenge += f', resource_metadata="{rm_url}"'
                await self._send_json(
                    send,
                    401,
                    {"error": "invalid_token", "detail": str(exc)},
                    extra_headers=[("www-authenticate", challenge)],
                )
                return
            token_ctx = jawafdehi_bearer_token.set(token)
            identity_ctx = current_user_identity.set(identity)

        mode_ctx = current_request_mode.set(mode)
        transport_ctx = current_transport.set("http")
        try:
            await self.session_manager.handle_request(scope, receive, send)
        finally:
            current_transport.reset(transport_ctx)
            current_request_mode.reset(mode_ctx)
            if identity_ctx is not None:
                current_user_identity.reset(identity_ctx)
            if token_ctx is not None:
                jawafdehi_bearer_token.reset(token_ctx)
