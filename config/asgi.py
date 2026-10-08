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

# Stand-in for a server port the ASGI server could not report. Matches the value
# Django falls back to when ``scope["server"]`` is missing altogether
# (``django/core/handlers/asgi.py:85``). See ``_with_known_server_port``.
UNKNOWN_SERVER_PORT = 0


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
    def _with_known_server_port(scope):
        """Repair ``scope["server"] == (host, None)`` before Django reads it.

        An ASGI server MAY report the port as ``None`` — the spec allows it, and
        uvicorn does exactly that when the socket has no meaningful port. Django
        does not defend against it. ``ASGIRequest.__init__`` stringifies the
        element unconditionally::

            # django/core/handlers/asgi.py:82
            self.META["SERVER_PORT"] = str(self.scope["server"][1])

        so a ``None`` port becomes the literal string ``"None"``, not the
        ``"0"`` that the ``else`` branch two lines down would have supplied —
        that branch only runs when ``server`` is absent ENTIRELY, which is why
        this slips through. ``HttpRequest.get_port()`` then hands ``"None"`` to
        anything that treats a port as a number.

        It reached production as an unhandled 500 (Sentry JAWAFDEHI-API-TR, first
        seen 2026-08-13). Wagtail's redirect middleware runs on every 404 and
        calls ``Site.find_for_request`` -> ``get_site_for_hostname``, which puts
        the value straight into an ``IntegerField`` lookup::

            ValueError: invalid literal for int() with base 10: 'None'
            ValueError: Field 'port' expected a number but got 'None'.

        So any 404 — an unknown case slug, say — returned 500 instead on every
        request whose scope carried a null port.

        ``USE_X_FORWARDED_PORT`` is NOT the fix. ``get_port()`` only consults
        ``X-Forwarded-Port`` when the header is present, and the reported events
        carry no ``X-Forwarded-*`` headers at all (in-cluster callers reaching
        the Service directly, bypassing Traefik) — it would fall straight back to
        the same broken ``SERVER_PORT``.

        Normalising to ``0`` rather than inferring 80/443 from the scheme is
        deliberate, on two counts. It is the value Django itself uses for an
        unknown port, so nothing downstream meets a number it would not already
        have met. And it cannot collide: Wagtail matches ``Q(port=port) |
        Q(is_default_site=True)``, so a guessed 80 or 443 could silently select a
        DIFFERENT ``Site`` row and reroute the request, whereas ``0`` matches no
        row and falls back to the default site — which is the correct answer when
        the port is genuinely unknown.
        """
        server = scope.get("server")
        if not server or server[1] is not None:
            return scope
        repaired = dict(scope)
        repaired["server"] = (server[0], UNKNOWN_SERVER_PORT)
        return repaired

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
        if scope["type"] == "http":
            scope = self._with_known_server_port(scope)
            if self._is_mcp_request(scope):
                await self.mcp_app(self._mcp_scope(scope), receive, send)
                return
        await self.django_app(scope, receive, send)


configure_embedded_api(django_application)
mcp_application = JawafdehiMCPServer(stateless=True)
application = PlatformASGIApplication(django_application, mcp_application)
