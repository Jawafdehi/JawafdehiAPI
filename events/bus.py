# SPDX-License-Identifier: Hippocratic-3.0
"""Publishing to the bus from synchronous Django code.

``nats-py`` is asyncio-only; Django's request path is not. The bridge is one
background thread per process running an event loop, holding one long-lived
connection, started lazily on first publish. Publishes are handed to that loop
with ``run_coroutine_threadsafe``.

Three rules this module exists to enforce:

**Publishing is best-effort and never raises.** Every entry point swallows
its exceptions and logs. A broker outage must degrade enrichment, never a case
write — the bus is transport, the case record is the truth. If you find yourself
wanting to propagate an error from here, the thing you actually want is a job.

**One connection per process, not one per publish.** Connecting per call would
turn a burst of approvals into a burst of TCP handshakes, and this cluster has
already had an incident where a per-operation connection pattern exhausted a
server's connection cap. The loop thread and its connection are reused.

**A dead broker must not slow the request path.** The first publish after a
connection failure does not retry immediately; it fails fast for
:data:`CONNECT_RETRY_SECONDS` before trying again. Publishes are fire-and-forget
by default, so nothing in a web request waits on the network.

With ``settings.NATS_URL`` unset every publish is a logged no-op and no thread
is ever started, which is what lets this ship before the broker exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

#: After a failed connect, fail fast for this long before trying again. Without
#: it, every request on a broker outage pays a full connect timeout.
CONNECT_RETRY_SECONDS = 30

#: Ceiling on how long the lazy startup may block the calling thread. Deliberately
#: short: this runs inside a web request the first time.
STARTUP_TIMEOUT_SECONDS = 5

#: Ceiling for a publish when the caller opts into waiting (``wait=True``).
PUBLISH_TIMEOUT_SECONDS = 5


def enabled() -> bool:
    """True when a broker is configured. Everything here no-ops when False."""
    return bool(getattr(settings, "NATS_URL", ""))


class _Bus:
    """Owns the loop thread and the connection. One instance per process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._nc = None
        self._js = None
        self._pid: int | None = None
        self._last_failure_at: float = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _reset_locked(self):
        self._loop = None
        self._thread = None
        self._nc = None
        self._js = None
        self._pid = None

    def _ensure_started(self) -> bool:
        """Start the loop thread and connect if needed. False if unavailable."""
        with self._lock:
            # A forked child inherits this object's state but NOT the parent's
            # threads, so the loop it points at is gone. Under gunicorn the fork
            # happens before any request, so in practice we start fresh in the
            # child — but only because we check.
            if self._pid is not None and self._pid != os.getpid():
                logger.info("events.bus_reset_after_fork", inherited_pid=self._pid)
                self._reset_locked()

            if self._js is not None:
                return True

            if time.monotonic() - self._last_failure_at < CONNECT_RETRY_SECONDS:
                return False

            try:
                self._start_locked()
                return True
            except Exception as exc:  # noqa: BLE001 - never propagate to a write path
                self._last_failure_at = time.monotonic()
                self._reset_locked()
                logger.warning("events.connect_failed", error=str(exc))
                return False

    def _start_locked(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever, name="events-bus", daemon=True
        )
        thread.start()

        future = asyncio.run_coroutine_threadsafe(self._connect(), loop)
        try:
            self._nc, self._js = future.result(timeout=STARTUP_TIMEOUT_SECONDS)
        except Exception:
            loop.call_soon_threadsafe(loop.stop)
            raise

        self._loop = loop
        self._thread = thread
        self._pid = os.getpid()
        logger.info("events.connected", url=_redact(settings.NATS_URL))

    async def _connect(self):
        import nats

        from events.streams import ensure_streams

        nc = await nats.connect(
            settings.NATS_URL,
            name="jawafdehi-platform",
            connect_timeout=STARTUP_TIMEOUT_SECONDS,
            # Reconnect forever rather than giving up: this process outlives any
            # broker restart, and a permanently-detached publisher that still
            # looks healthy is worse than one that keeps trying.
            max_reconnect_attempts=-1,
        )
        js = nc.jetstream()
        await ensure_streams(js)
        return nc, js

    def close(self):
        """Drain and disconnect. For tests and orderly shutdown."""
        with self._lock:
            loop, nc = self._loop, self._nc
            self._reset_locked()
        if loop is None:
            return
        try:
            if nc is not None:
                asyncio.run_coroutine_threadsafe(nc.drain(), loop).result(timeout=5)
        except Exception as exc:  # noqa: BLE001 - shutdown is best-effort too
            logger.warning("events.close_failed", error=str(exc))
        finally:
            loop.call_soon_threadsafe(loop.stop)

    # ── publishing ───────────────────────────────────────────────────────────

    def publish(self, subject: str, envelope: dict[str, Any], wait: bool = False) -> bool:
        """Publish one envelope. Returns True if it was handed to the loop.

        A True return means accepted for delivery, not delivered — with
        ``wait=False`` the JetStream ack arrives after this returns, and a
        failure then surfaces in the logs via the done-callback.
        """
        if not self._ensure_started():
            return False

        body = json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8")
        # Nats-Msg-Id is what makes JetStream collapse a duplicate publish inside
        # its dedup window — the same idempotency spine as the proposal's
        # dedup_key. Omitted rather than sent empty when a producer has no key.
        headers = {}
        if envelope.get("dedup_key"):
            headers["Nats-Msg-Id"] = envelope["dedup_key"]

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._js.publish(subject, body, headers=headers or None), self._loop
            )
        except Exception as exc:  # noqa: BLE001 - loop may have died under us
            logger.warning("events.publish_failed", subject=subject, error=str(exc))
            return False

        if wait:
            try:
                future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("events.publish_failed", subject=subject, error=str(exc))
                return False
        else:
            future.add_done_callback(lambda f: _log_result(f, subject))
        return True


def _log_result(future, subject: str):
    """Surface a fire-and-forget failure. Nothing else observes these."""
    try:
        future.result()
    except Exception as exc:  # noqa: BLE001
        logger.warning("events.publish_failed", subject=subject, error=str(exc))


def _redact(url: str) -> str:
    """Strip credentials from a nats:// URL before logging it."""
    if not url or "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}" if scheme else url


_bus = _Bus()


def publish(subject: str, envelope: dict[str, Any], wait: bool = False) -> bool:
    """Best-effort publish. Never raises; returns False when nothing was sent.

    Args:
        subject: See :mod:`events.subjects`.
        envelope: Built by :func:`events.envelope.build_envelope`.
        wait: Block for the JetStream ack (up to
            :data:`PUBLISH_TIMEOUT_SECONDS`). Leave False in a request path;
            useful in management commands and tests where you want the result.
    """
    if not enabled():
        logger.debug("events.publish_skipped", subject=subject, reason="NATS_URL unset")
        return False
    try:
        return _bus.publish(subject, envelope, wait=wait)
    except Exception as exc:  # noqa: BLE001 - the whole point of this module
        logger.warning("events.publish_failed", subject=subject, error=str(exc))
        return False


def close():
    """Drain and disconnect the process-wide connection."""
    _bus.close()
