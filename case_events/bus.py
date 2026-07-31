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
        self._starting = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _reset_locked(self):
        self._loop = None
        self._thread = None
        self._nc = None
        self._js = None
        self._pid = None
        # Must be cleared too: a child that inherited _starting=True from a
        # parent mid-connect would consider a start permanently in progress and
        # never connect again.
        self._starting = False

    def _ensure_started(self) -> bool:
        """Start the loop thread and connect if needed. False if unavailable.

        At most ONE thread ever performs the connect, and it does so **without
        holding the lock**. Everyone else returns False immediately rather than
        queueing behind it.

        That distinction is the whole point. The connect can take the full
        :data:`STARTUP_TIMEOUT_SECONDS`, and on a dead broker it always does:
        ``_connect`` passes ``max_reconnect_attempts=-1``, and nats-py treats a
        negative value as "never stop retrying" for the *initial* connect too
        (``nats/aio/client.py``), so it never fails fast. Holding the lock across
        that made one dead broker stall every publishing thread in the process
        for 5 seconds at a time — precisely the behaviour this module's docstring
        promises does not happen.
        """
        with self._lock:
            # A forked child inherits this object's state but NOT the parent's
            # threads, so the loop it points at is gone. Under gunicorn the fork
            # happens before any request, so in practice we start fresh in the
            # child — but only because we check.
            if self._pid is not None and self._pid != os.getpid():
                logger.info("case_events.bus_reset_after_fork", inherited_pid=self._pid)
                self._reset_locked()

            if self._js is not None:
                return True

            if self._starting:
                # Someone else is already paying the connect cost.
                logger.debug("case_events.connect_in_progress")
                return False

            if time.monotonic() - self._last_failure_at < CONNECT_RETRY_SECONDS:
                # Logged, because this window used to swallow every publish for
                # 30 seconds with no output at all — a broker blip looked
                # identical to a healthy bus with nothing to say.
                logger.debug("case_events.connect_suppressed", subject_hint="retry window")
                return False

            self._starting = True

        try:
            started = self._start()
        except Exception as exc:  # noqa: BLE001 - never propagate to a write path
            with self._lock:
                self._last_failure_at = time.monotonic()
                self._starting = False
            # str(TimeoutError()) is "", which made the one line explaining an
            # outage read `connect_failed error=`. The type is the useful part.
            logger.warning(
                "case_events.connect_failed",
                error=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
            )
            return False

        with self._lock:
            self._loop, self._thread, self._nc, self._js = started
            self._pid = os.getpid()
            # Clear the fail-fast window: it is a backoff against a broker that
            # was down, and this one demonstrably is not.
            self._last_failure_at = 0.0
            self._starting = False
        logger.info("case_events.connected", url=_redact(settings.NATS_URL))
        return True

    def _start(self):
        """Connect, returning the new state. Blocking; mutates nothing on self."""
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="events-bus", daemon=True)
        thread.start()

        future = asyncio.run_coroutine_threadsafe(self._connect(), loop)
        try:
            nc, js = future.result(timeout=STARTUP_TIMEOUT_SECONDS)
        except Exception:
            # Cancel before stopping the loop, or the abandoned _connect task is
            # destroyed while pending and nats-py logs it as an error.
            future.cancel()
            loop.call_soon_threadsafe(loop.stop)
            raise

        return loop, thread, nc, js

    async def _connect(self):
        import nats

        nc = await nats.connect(
            settings.NATS_URL,
            name="jawafdehi-platform",
            connect_timeout=STARTUP_TIMEOUT_SECONDS,
            # Reconnect forever rather than giving up: this process outlives any
            # broker restart, and a permanently-detached publisher that still
            # looks healthy is worse than one that keeps trying.
            max_reconnect_attempts=-1,
        )
        # Deliberately does NOT assert the stream topology. Doing that here would
        # require every publishing process to hold JetStream stream-CREATE
        # rights, which is broker-admin authority for something whose only job is
        # to publish — and it would undo the point of giving each identity its
        # own NATS user. Streams are asserted by `manage.py nats_bootstrap`; see
        # case_events/streams.py.
        return nc, nc.jetstream()

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
            logger.warning("case_events.close_failed", error=str(exc))
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
            logger.warning("case_events.publish_failed", subject=subject, error=str(exc))
            return False

        if wait:
            try:
                future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("case_events.publish_failed", subject=subject, error=str(exc))
                return False
        else:
            future.add_done_callback(lambda f: _log_result(f, subject))
        return True


def _log_result(future, subject: str):
    """Surface a fire-and-forget failure. Nothing else observes these."""
    try:
        future.result()
    except Exception as exc:  # noqa: BLE001
        logger.warning("case_events.publish_failed", subject=subject, error=str(exc))


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
        subject: See :mod:`case_events.subjects`.
        envelope: Built by :func:`case_events.envelope.build_envelope`.
        wait: Block for the JetStream ack (up to
            :data:`PUBLISH_TIMEOUT_SECONDS`). Leave False in a request path;
            useful in management commands and tests where you want the result.
    """
    if not enabled():
        logger.debug("case_events.publish_skipped", subject=subject, reason="NATS_URL unset")
        return False
    try:
        return _bus.publish(subject, envelope, wait=wait)
    except Exception as exc:  # noqa: BLE001 - the whole point of this module
        logger.warning("case_events.publish_failed", subject=subject, error=str(exc))
        return False


def close():
    """Drain and disconnect the process-wide connection."""
    _bus.close()
