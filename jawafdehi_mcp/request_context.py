"""Request-scoped context for MCP transports and bearer-token forwarding."""

import os
from contextvars import ContextVar

current_transport: ContextVar[str | None] = ContextVar(
    "current_transport", default=None
)

jawafdehi_bearer_token: ContextVar[str | None] = ContextVar(
    "jawafdehi_bearer_token", default=None
)


def is_local_stdio_transport() -> bool:
    """Return whether process-local capabilities are safe for this request."""
    return current_transport.get() == "stdio"


def get_local_service_token() -> str | None:
    """Return the configured API token only to a local stdio request."""
    if not is_local_stdio_transport():
        return None
    return os.getenv("JAWAFDEHI_API_TOKEN", "").strip() or None


def get_query_service_token() -> str | None:
    """Return the least-privilege SQL token, with stdio-only full-token fallback."""
    token = os.getenv("MCP_QUERY_API_TOKEN", "").strip()
    return token or get_local_service_token()


def get_forwarded_headers() -> dict[str, str]:
    """Headers forwarded to upstream jawafdehi-api calls.

    Forwards the caller's verified OIDC bearer so the API authenticates as the
    same user (OIDCJWTAuthentication). Empty in stdio/dev, where tools fall back
    to a service token.
    """
    token = jawafdehi_bearer_token.get()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}
