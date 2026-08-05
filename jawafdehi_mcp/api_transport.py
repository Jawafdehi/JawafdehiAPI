"""In-process transport for MCP calls to the colocated Django API."""

from __future__ import annotations

import asyncio
import math
import os
import threading
import weakref
from typing import Any

import httpx

from .request_context import current_transport

_embedded_api_application: Any | None = None
_limiter_lock = threading.Lock()
_limiters: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _positive_env_float(name: str, default: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return min(value, maximum)


def _positive_env_int(name: str, default: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


def _embedded_timeout_seconds() -> float:
    return _positive_env_float("MCP_EMBEDDED_API_TIMEOUT", 30.0, 300.0)


def _embedded_max_concurrency() -> int:
    return _positive_env_int("MCP_EMBEDDED_API_MAX_CONCURRENCY", 16, 256)


def _loop_limiter(limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _limiter_lock:
        existing = _limiters.get(loop)
        if existing is None or existing[0] != limit:
            existing = (limit, asyncio.Semaphore(limit))
            _limiters[loop] = existing
        return existing[1]


def _release_limiter_when_done(
    task: asyncio.Task[httpx.Response],
    limiter: asyncio.Semaphore,
) -> None:
    """Release retained capacity and consume a detached task's result."""
    limiter.release()
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:  # noqa: BLE001 — retrieving it IS the point; see below
        # The request already returned a timeout/cancellation to its caller.
        # Retrieving the exception prevents a detached-task warning.
        pass


class BoundedASGITransport(httpx.AsyncBaseTransport):
    """ASGI transport with a real deadline and shared concurrency bound.

    HTTPX's stock ``ASGITransport`` does not enforce its timeout extension.
    Without this wrapper, an embedded API call can wait forever even when the
    caller supplied ``timeout=30``.
    """

    def __init__(self, application: Any) -> None:
        self._transport = httpx.ASGITransport(
            app=application,
            raise_app_exceptions=False,
        )

    @staticmethod
    def _deadline_for(request: httpx.Request) -> float:
        configured = _embedded_timeout_seconds()
        request_timeouts = request.extensions.get("timeout")
        if not isinstance(request_timeouts, dict):
            return configured
        try:
            read_timeout = float(request_timeouts.get("read"))
        except (TypeError, ValueError):
            return configured
        if not math.isfinite(read_timeout) or read_timeout <= 0:
            return configured
        return min(read_timeout, configured)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        deadline = self._deadline_for(request)
        limiter = _loop_limiter(_embedded_max_concurrency())
        worker: asyncio.Task[httpx.Response] | None = None
        acquired = False
        try:
            async with asyncio.timeout(deadline):
                await limiter.acquire()
                acquired = True
                worker = asyncio.create_task(
                    self._transport.handle_async_request(request)
                )
                return await asyncio.shield(worker)
        except TimeoutError as exc:
            raise httpx.ReadTimeout(
                f"Embedded API call exceeded {deadline:g} seconds",
                request=request,
            ) from exc
        finally:
            if acquired:
                if worker is None or worker.done():
                    limiter.release()
                else:
                    # Sync Django work may outlive cancellation. Keep its capacity
                    # slot until it actually exits so repeated timeouts cannot
                    # create unbounded work behind the shared ASGI process.
                    worker.add_done_callback(
                        lambda task: _release_limiter_when_done(task, limiter)
                    )

    async def aclose(self) -> None:
        await self._transport.aclose()


def configure_embedded_api(application: Any | None) -> None:
    """Set the Django ASGI application used by embedded HTTP MCP requests."""
    global _embedded_api_application
    _embedded_api_application = application


def embedded_api_client_kwargs() -> dict[str, Any]:
    """Return AsyncClient kwargs for an in-memory API call when embedded.

    Stdio retains normal network behavior. Requests entering through the
    monolith's MCP HTTP surface are dispatched to Django in-process.
    """
    if current_transport.get() != "http" or _embedded_api_application is None:
        return {}
    return {"transport": BoundedASGITransport(_embedded_api_application)}
