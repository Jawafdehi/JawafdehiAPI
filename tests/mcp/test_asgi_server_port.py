"""The platform ASGI app repairs a null ``scope["server"]`` port.

Regression cover for an unhandled 500: uvicorn may report the server port as
``None``, Django stringifies it to the literal ``"None"``, and the first thing
that treats a port as a number raises. The reasoning lives on
``PlatformASGIApplication._with_known_server_port``.
"""

import io

import pytest
from django.core.handlers.asgi import ASGIRequest

from config.asgi import UNKNOWN_SERVER_PORT, PlatformASGIApplication


def _http_scope(server):
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/cases/no-such-slug/",
        "raw_path": b"/api/cases/no-such-slug/",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "client": ("10.42.0.1", 54321),
        "server": server,
        "headers": [(b"host", b"api.jawafdehi.org")],
    }


def _request(scope):
    return ASGIRequest(scope, io.BytesIO(b""))


def test_null_server_port_becomes_the_unknown_port():
    repaired = PlatformASGIApplication._with_known_server_port(
        _http_scope(("10.42.5.203", None))
    )

    assert repaired["server"] == ("10.42.5.203", UNKNOWN_SERVER_PORT)


def test_a_real_server_port_is_left_alone():
    scope = _http_scope(("10.42.5.203", 8080))

    assert PlatformASGIApplication._with_known_server_port(scope) is scope


def test_a_missing_server_key_is_left_alone():
    scope = _http_scope(None)

    assert PlatformASGIApplication._with_known_server_port(scope) is scope


def test_the_original_scope_is_not_mutated():
    """Other middleware may hold a reference to the scope we were handed."""
    scope = _http_scope(("10.42.5.203", None))

    PlatformASGIApplication._with_known_server_port(scope)

    assert scope["server"] == ("10.42.5.203", None)


def test_django_reads_a_numeric_port_from_the_repaired_scope():
    """The actual contract: ``get_port()`` must survive ``int()``."""
    repaired = PlatformASGIApplication._with_known_server_port(
        _http_scope(("10.42.5.203", None))
    )

    port = _request(repaired).get_port()

    assert port == str(UNKNOWN_SERVER_PORT)
    assert int(port) == UNKNOWN_SERVER_PORT


def test_unrepaired_scope_is_what_blew_up():
    """Pin the upstream behaviour this exists to work around.

    If Django ever stops stringifying ``None`` here, this fails and the
    workaround can go.
    """
    port = _request(_http_scope(("10.42.5.203", None))).get_port()

    assert port == "None"
    with pytest.raises(ValueError):
        int(port)


@pytest.mark.asyncio
async def test_dispatch_repairs_the_port_before_django_sees_it():
    seen = {}

    async def fake_django_app(scope, receive, send):
        seen["server"] = scope["server"]

    async def fake_mcp_app(scope, receive, send):  # pragma: no cover - not routed here
        raise AssertionError("MCP app should not receive a Django path")

    app = PlatformASGIApplication(fake_django_app, fake_mcp_app)

    await app(_http_scope(("10.42.5.203", None)), None, None)

    assert seen["server"] == ("10.42.5.203", UNKNOWN_SERVER_PORT)
