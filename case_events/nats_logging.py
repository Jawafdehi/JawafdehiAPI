"""Async-error reporting for the NATS clients.

nats-py reports every asynchronous error through one callback, and its default
is ``logger.error("nats: encountered error", exc_info=ex)``. With the Sentry
logging integration attached, that makes an issue out of each one — including
``DrainTimeoutError``, which this estate raises on *every* pod shutdown whose
drain does not finish inside the client's 30s budget.

That accounted for 86 Sentry events across five ReplicaSet generations of
``platform-bus-consumers``: once per rollout, by construction. All of it
grouped under a single title (``nats: encountered error``) that says nothing
about which error it was, so a genuine async failure — a stale credential, a
broken subscription — would land in the same bucket as the shutdown noise and
be invisible.

The split here keeps every error in the logs at full detail, and reserves
ERROR (and therefore Sentry) for the ones that are not an expected consequence
of the process going away.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

#: Errors that are an expected part of an orderly shutdown. A drain timeout
#: means in-flight work outlived the client's drain budget while the process was
#: already on its way out — worth seeing in the logs, not worth an alert.
#:
#: Matched by class *name* rather than by import: the exception lives in
#: ``nats.errors``, and this module is imported from Django settings-adjacent
#: code paths that must not require nats-py to be installed.
_SHUTDOWN_ERROR_NAMES = frozenset({"DrainTimeoutError", "ConnectionDrainingError"})


def is_shutdown_noise(exc: BaseException) -> bool:
    return type(exc).__name__ in _SHUTDOWN_ERROR_NAMES


async def error_cb(exc: Exception) -> None:
    """``error_cb`` for ``nats.connect``.

    Replaces nats-py's default, which logs everything at ERROR under one
    message. Note the event name carries the exception class, so distinct
    failures group distinctly instead of collapsing into one issue.
    """
    detail = {
        "error_type": type(exc).__name__,
        "error": str(exc) or type(exc).__name__,
    }
    if is_shutdown_noise(exc):
        logger.warning("case_events.nats_shutdown_error", **detail)
    else:
        logger.error("case_events.nats_error", **detail)
