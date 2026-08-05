"""Bounded Uvicorn worker for the platform's Gunicorn ASGI deployment."""

from __future__ import annotations

import os

from uvicorn_worker import UvicornWorker

DEFAULT_ASGI_LIMIT_CONCURRENCY = 16
MAX_ASGI_LIMIT_CONCURRENCY = 256


def asgi_limit_concurrency() -> int:
    try:
        value = int(
            os.getenv(
                "ASGI_LIMIT_CONCURRENCY",
                str(DEFAULT_ASGI_LIMIT_CONCURRENCY),
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_ASGI_LIMIT_CONCURRENCY
    if value <= 0:
        return DEFAULT_ASGI_LIMIT_CONCURRENCY
    return min(value, MAX_ASGI_LIMIT_CONCURRENCY)


class BoundedUvicornWorker(UvicornWorker):
    """Reject excess concurrent requests with 503 instead of queuing forever."""

    CONFIG_KWARGS = {
        **UvicornWorker.CONFIG_KWARGS,
        "limit_concurrency": asgi_limit_concurrency(),
    }
