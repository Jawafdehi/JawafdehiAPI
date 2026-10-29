"""Async-error reporting for the NATS clients.

nats-py reports asynchronous errors through one callback whose default is
``logger.error("nats: encountered error", exc_info=ex)``. With the Sentry
logging integration attached, that makes an issue out of each one — including
``DrainTimeoutError``, which this estate raises on *every* pod shutdown whose
drain does not finish inside the client's 30s budget. 86 Sentry events across
five ReplicaSet generations of ``platform-bus-consumers``: once per rollout, by
construction, all under a single title that named no exception.

Two things this does NOT do, recorded because both were claimed in an earlier
version of this file and are false:

* **It does not retire the ``nats: encountered error`` bucket.** nats-py logs
  that exact message from a second site — ``Client._read_loop`` calls
  ``_logger.error(...)`` directly, not through ``_error_cb`` — so a read-loop
  failure still lands there. Only the paths that go through the callback
  (drain, flusher, subscription errors) are covered here. A falling issue count
  is not evidence the condition stopped.
* **It does not group distinctly by exception class.** The log event name is a
  constant; the class rides in ``error_type`` and in the captured exception.

Uses **stdlib logging, not structlog**, and that is load-bearing. structlog's
configured chain runs ``format_exc_info``, which consumes ``exc_info`` into a
rendered string before the ``LogRecord`` is built — leaving
``record.exc_info is None``. Sentry's ``LoggingIntegration`` builds an
exception event only from ``record.exc_info``, so a structlog call here would
produce a message-only issue with no exception type and no frames: strictly
worse than the nats-py default it replaces. stdlib keeps the real exception.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

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

    Shutdown-class errors log at WARNING (visible, not alertable); everything
    else keeps the ERROR level and the exception that nats-py's default would
    have given Sentry.
    """
    level = logging.WARNING if is_shutdown_noise(exc) else logging.ERROR
    logger.log(
        level,
        "nats async error (%s): %s",
        type(exc).__name__,
        exc or type(exc).__name__,
        # The whole reason this is stdlib and not structlog — see module docstring.
        exc_info=exc,
        extra={"error_type": type(exc).__name__},
    )
