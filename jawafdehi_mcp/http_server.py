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

    Distinguishes "sent nothing" from "tried to authenticate and got it wrong".
    ``_bearer_from_headers`` returns ``None`` for both, and the two earn
    different RFC 6750 answers: a missing credential is challenged without an
    error code, a malformed one is told what was wrong with it.
    """
    return bool(headers.get(b"authorization", b"").strip())


def _anonymous_access_enabled() -> bool:
    """Whether to serve unauthenticated callers the read-only catalog.

    Off by default: every tool requires a verified bearer. The switch exists so
    the anonymous catalog can be restored by setting an environment variable and
    restarting, without a code change — this flipped a plane that had been open
    since the server shipped, and the rollback wants to be a config edit.
    Delete it (and ``identity.ANONYMOUS_TOOL_NAMES`` with it) once the
    mandatory-login behaviour has held in production long enough to be dull.
    """
    return os.getenv("MCP_ALLOW_ANONYMOUS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _bearer_challenge(error: str | None = None) -> str:
    """Build a ``WWW-Authenticate`` value, pointing at RFC 9728 metadata.

    ``error`` is omitted for a request that carried no credential at all:
    RFC 6750 §3.1 reserves the error codes for a credential that was supplied
    and failed, and MCP clients key their OAuth flow off ``resource_metadata``
    rather than off an error code.
    """
    challenge = 'Bearer realm="OAuth"' if error is None else f'Bearer error="{error}"'
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

        One door, one rule: every tool needs a verified bearer. A request
        without one is answered with ``401`` and a ``WWW-Authenticate``
        challenge naming the RFC 9728 metadata document, which is what makes an
        MCP client start its OAuth flow — clients begin one *only* on a 401, so
        while this server answered anonymous callers with a catalog it was
        impossible for one to know it should log in. Being anonymous is now a
        thing you are told, not a thing you have to think to check.

        A credential that is present but malformed or invalid is rejected the
        same way it always was, with an error code distinguishing it from a
        caller who simply has not logged in yet.

        ``/health`` and the metadata document stay open, above: the first is a
        kubelet probe, the second is how a challenged client learns where to
        authenticate.

        Setting ``MCP_ALLOW_ANONYMOUS`` restores the previous behaviour — the
        read-only ``identity.ANONYMOUS_TOOL_NAMES`` catalog, served without a
        challenge. See ``_anonymous_access_enabled``.
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
            # token verification does not depend on it, so a pod that has it
            # unset still serves every authenticated call, and failing the probe
            # would take the pod down for a fault that costs discovery rather
            # than service. The misconfiguration surfaces where it bites — the
            # metadata endpoint 503s, and the 401 challenge loses the
            # ``resource_metadata`` hint that points a client at it.
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
            # Unauthenticated by necessity: this is the document a challenged
            # client fetches to find out WHERE to authenticate, so gating it
            # behind authentication would close the only way in.
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

        # A credential we cannot parse is a malformed request, not a missing one
        # (RFC 6750 §3.1 `invalid_request`). Both now end in 401, but they are
        # kept apart deliberately: `invalid_request` tells a caller its header is
        # wrong, where the bare challenge below tells one that never sent a
        # header to go and log in. Collapsing them would send a client with a
        # typo'd scheme around the OAuth flow it has already completed.
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

        # No credential at all. This is the branch an MCP client is waiting for:
        # the 401 is what tells it to go and authenticate, and the challenge
        # carries the address of the metadata that says where.
        if token is None and not _anonymous_access_enabled():
            await self._send_json(
                send,
                401,
                {
                    "error": "unauthorized",
                    "detail": (
                        "Authentication is required. Supply an OIDC bearer "
                        "token; see the linked protected-resource metadata."
                    ),
                },
                extra_headers=[("www-authenticate", _bearer_challenge())],
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
        try:
            await self.session_manager.handle_request(scope, receive, send)
        finally:
            current_transport.reset(transport_ctx)
            if identity_ctx is not None:
                current_user_identity.reset(identity_ctx)
            if token_ctx is not None:
                jawafdehi_bearer_token.reset(token_ctx)
