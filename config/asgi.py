"""ASGI entrypoint for the unified Django and MCP platform."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_application = get_asgi_application()

# Import MCP only after Django has initialized its app registry. The tools can
# then dispatch back into this Django application without a network hop.
from jawafdehi_mcp.api_transport import configure_embedded_api  # noqa: E402
from jawafdehi_mcp.http_server import (  # noqa: E402
    WELL_KNOWN_PROTECTED_RESOURCE,
    JawafdehiMCPServer,
)

MCP_PATH_PREFIX = "/mcp"
MCP_PROTECTED_RESOURCE_PATH = f"{WELL_KNOWN_PROTECTED_RESOURCE}{MCP_PATH_PREFIX}"
MCP_PROTOCOL_PATHS = frozenset({MCP_PATH_PREFIX, f"{MCP_PATH_PREFIX}/"})
MCP_AUXILIARY_PATHS = frozenset(
    {
        f"{MCP_PATH_PREFIX}/health",
        MCP_PROTECTED_RESOURCE_PATH,
    }
)


class PlatformASGIApplication:
    """Route MCP protocol traffic while leaving every other path to Django."""

    def __init__(self, django_app, mcp_app):
        self.django_app = django_app
        self.mcp_app = mcp_app

    @staticmethod
    def _is_mcp_request(scope) -> bool:
        path = scope.get("path", "")
        return path in MCP_PROTOCOL_PATHS or path in MCP_AUXILIARY_PATHS

    @staticmethod
    def _mcp_scope(scope):
        """Strip the monolith prefix so the original MCP route contract remains."""
        path = scope.get("path", "")
        if path == MCP_PROTECTED_RESOURCE_PATH:
            new_path = WELL_KNOWN_PROTECTED_RESOURCE
        elif path in MCP_PROTOCOL_PATHS:
            new_path = "/"
        elif path == f"{MCP_PATH_PREFIX}/health":
            new_path = "/health"
        else:
            return scope

        rebased = dict(scope)
        rebased["path"] = new_path
        if "raw_path" in rebased:
            rebased["raw_path"] = new_path.encode()
        if path in MCP_PROTOCOL_PATHS or path == f"{MCP_PATH_PREFIX}/health":
            root_path = scope.get("root_path", "").rstrip("/")
            rebased["root_path"] = f"{root_path}{MCP_PATH_PREFIX}"
        return rebased

    async def __call__(self, scope, receive, send):
        # Django does not consume ASGI lifespan events. The MCP session manager
        # does, and remains active for the lifetime of the shared worker.
        if scope["type"] == "lifespan":
            await self.mcp_app(scope, receive, send)
            return
        if scope["type"] == "http" and self._is_mcp_request(scope):
            await self.mcp_app(self._mcp_scope(scope), receive, send)
            return
        await self.django_app(scope, receive, send)


configure_embedded_api(django_application)
mcp_application = JawafdehiMCPServer(stateless=True)
application = PlatformASGIApplication(django_application, mcp_application)
