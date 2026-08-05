"""Tests for authenticated, bounded in-process control-plane calls."""

import asyncio
import json
from time import monotonic

import httpx
import pytest

from config.asgi import django_application
from jawafdehi_mcp.api_transport import (
    configure_embedded_api,
    embedded_api_client_kwargs,
)
from jawafdehi_mcp.control_plane import request_control_plane
from jawafdehi_mcp.request_context import (
    current_transport,
    jawafdehi_bearer_token,
)


async def _consume_request(receive):
    while True:
        message = await receive()
        if not message.get("more_body"):
            return


@pytest.mark.asyncio
async def test_embedded_client_forwards_only_the_request_bearer(monkeypatch):
    seen = {}

    async def fake_api(scope, receive, send):
        await _consume_request(receive)
        seen["path"] = scope["path"]
        seen["authorization"] = dict(scope["headers"]).get(b"authorization")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"etag", b'"v1"'),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"results": []}).encode(),
            }
        )

    configure_embedded_api(fake_api)
    transport = current_transport.set("http")
    bearer = jawafdehi_bearer_token.set("caller-token")
    monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "must-not-be-used")
    try:
        result = await request_control_plane("GET", "/api/materials/")
    finally:
        jawafdehi_bearer_token.reset(bearer)
        current_transport.reset(transport)
        configure_embedded_api(django_application)

    assert seen == {
        "path": "/api/materials/",
        "authorization": b"Bearer caller-token",
    }
    assert result == {
        "success": True,
        "status_code": 200,
        "data": {"results": []},
        "etag": '"v1"',
    }


@pytest.mark.asyncio
async def test_control_plane_client_rejects_arbitrary_paths():
    with pytest.raises(ValueError, match="fixed /api/ path"):
        await request_control_plane("GET", "https://metadata.invalid/latest")


@pytest.mark.asyncio
@pytest.mark.security
async def test_control_plane_client_rejects_nested_encoded_traversal():
    with pytest.raises(ValueError, match="traversal"):
        await request_control_plane("GET", "/api/courts/%25252e%25252e/health")


@pytest.mark.asyncio
async def test_embedded_transport_enforces_deadline(monkeypatch):
    finished = asyncio.Event()

    async def slow_api(scope, receive, send):
        await _consume_request(receive)
        await asyncio.sleep(0.05)
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})
        finished.set()

    configure_embedded_api(slow_api)
    transport = current_transport.set("http")
    monkeypatch.setenv("MCP_EMBEDDED_API_TIMEOUT", "0.02")
    started = monotonic()
    try:
        async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.get("http://testserver/api/slow", timeout=5)
    finally:
        await asyncio.wait_for(finished.wait(), timeout=0.5)
        current_transport.reset(transport)
        configure_embedded_api(django_application)

    assert monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_timed_out_work_retains_its_concurrency_slot(monkeypatch):
    started = 0
    release = asyncio.Event()

    async def slow_api(scope, receive, send):
        nonlocal started
        await _consume_request(receive)
        started += 1
        await release.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    configure_embedded_api(slow_api)
    transport = current_transport.set("http")
    monkeypatch.setenv("MCP_EMBEDDED_API_TIMEOUT", "0.02")
    monkeypatch.setenv("MCP_EMBEDDED_API_MAX_CONCURRENCY", "1")
    try:
        async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.get("http://testserver/api/first")
            with pytest.raises(httpx.ReadTimeout):
                await client.get("http://testserver/api/second")
            assert started == 1
            release.set()
            await asyncio.sleep(0)
    finally:
        release.set()
        current_transport.reset(transport)
        configure_embedded_api(django_application)


@pytest.mark.asyncio
async def test_embedded_transport_enforces_shared_concurrency(monkeypatch):
    active = 0
    maximum = 0

    async def bounded_api(scope, receive, send):
        nonlocal active, maximum
        await _consume_request(receive)
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def make_request():
        async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
            return await client.get("http://testserver/api/work")

    configure_embedded_api(bounded_api)
    transport = current_transport.set("http")
    monkeypatch.setenv("MCP_EMBEDDED_API_MAX_CONCURRENCY", "1")
    try:
        responses = await asyncio.gather(*(make_request() for _ in range(3)))
    finally:
        current_transport.reset(transport)
        configure_embedded_api(django_application)

    assert [response.status_code for response in responses] == [204, 204, 204]
    assert maximum == 1
