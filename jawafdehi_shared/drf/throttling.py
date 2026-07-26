"""Approximate distributed rate throttling (local counter + async Redis sync).

Why this exists
---------------
DRF's stock throttles do a synchronous ``cache.get`` + ``cache.set`` on every
request. That leaves two bad options on this cluster:

* backed by the per-process ``LocMemCache`` (the default), the cap is counted
  PER gunicorn worker, so the effective ceiling is ``rate x workers x replicas``
  and grows as you scale (threat-model F14); or
* backed by a shared Redis, the counter is global but the ``get``/``set`` sit on
  the request hot path — and our shared Valkey may be in the *other* cloud
  (Monal<->OCI WireGuard mesh), so that is a cross-cloud round-trip per request.

These throttles keep the decision in-process (no network on the hot path) and
reconcile to a single shared Redis on a BACKGROUND timer. The result is a cap
that is:

* **latency-neutral** — the cross-cloud hop happens off the request path;
* **approximately global** and independent of pod/worker count — the shared key
  is the source of truth; and
* **fail-open** — if Redis is unreachable the hot path is unaffected and no
  request 500s; the limit simply degrades toward the per-worker local count
  until sync resumes.

Accuracy trade-off
------------------
Between flushes a worker cannot see other workers' in-flight increments, so the
aggregate can OVERSHOOT by roughly ``workers x (requests per flush interval)``.
Tune with ``THROTTLE_SYNC_INTERVAL``. This is approximate by design.

Configuration (env)
-------------------
* ``THROTTLE_SYNC_URL``      redis:// URL of the shared reconciliation store. If
                            unset (dev / tests / off-cluster) these classes fall
                            back to DRF's stock per-process behaviour, so nothing
                            changes where no sync target is provisioned.
* ``THROTTLE_SYNC_INTERVAL`` seconds between background flushes (default 2.0).
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

logger = logging.getLogger("jawafdehi.throttling")

_SYNC_URL = os.getenv("THROTTLE_SYNC_URL", "").strip()
_SYNC_INTERVAL = float(os.getenv("THROTTLE_SYNC_INTERVAL", "2.0"))

# Process-wide reconciliation state. DRF builds a fresh throttle INSTANCE per
# request, so the counters must live at module scope (shared by every request
# and every thread in this worker process). Guarded by one re-entrant lock.
_lock = threading.RLock()
_pending: dict[str, int] = {}    # successful increments not yet flushed to Redis
_synced: dict[str, int] = {}     # last global total read back from Redis
_deadline: dict[str, float] = {}  # epoch after which a key is stale + prunable

_redis = None
_flusher_started = False


# ---------------------------------------------------------------------------
# Redis client + background reconciliation
# ---------------------------------------------------------------------------
def _get_redis():
    """Lazily build a Redis client with tight timeouts (the client is only ever
    used off the hot path, but bound it so a hung Valkey can't wedge the flush
    thread)."""
    global _redis
    if _redis is None and _SYNC_URL:
        import redis  # hard dependency (pyproject: redis>=5.0)

        _redis = redis.Redis.from_url(
            _SYNC_URL, socket_timeout=1.0, socket_connect_timeout=1.0
        )
    return _redis


def _flush_once(client=None) -> None:
    """Fold this worker's pending deltas into the shared counters and refresh the
    local view. Fail-open: on any Redis error the delta is returned to the
    pending pool for the next tick and nothing propagates to callers."""
    client = client or _get_redis()
    if client is None:
        return

    # Snapshot + zero the pending deltas under the lock, then do network I/O
    # outside it so request threads never block on Redis.
    with _lock:
        batch = {k: v for k, v in _pending.items() if v}
        for k in batch:
            _pending[k] = 0

    processed: set[str] = set()
    failure = None
    for rkey, delta in batch.items():
        try:
            total = client.incrby(rkey, delta)
            # Expire well past the window so stale windows self-clean in Redis
            # even if this worker never touches the key again.
            client.expire(rkey, 7200)
            with _lock:
                _synced[rkey] = int(total)
            processed.add(rkey)
        except Exception as exc:  # noqa: BLE001 — fail-open, never raise to caller
            failure = exc
            break  # Redis likely down — stop hammering it this tick

    if failure is not None:
        # Return EVERY delta we didn't confirm (the failed key plus all the keys
        # we hadn't reached yet) so a mid-batch failure loses nothing; retry next
        # tick. Fail-open: the hot path never saw any of this.
        with _lock:
            for k, d in batch.items():
                if k not in processed:
                    _pending[k] = _pending.get(k, 0) + d
        logger.warning(
            "throttle sync flush failed; %d/%d key(s) deferred (failing open): %s",
            len(batch) - len(processed), len(batch), failure,
        )

    _prune()


def _prune() -> None:
    """Drop local entries whose window is well past, so memory stays bounded by
    the count of active (ident, window) pairs rather than growing forever."""
    now = time.time()
    with _lock:
        stale = [k for k, dl in _deadline.items() if now > dl and _pending.get(k, 0) == 0]
        for k in stale:
            _pending.pop(k, None)
            _synced.pop(k, None)
            _deadline.pop(k, None)


def _flusher_loop() -> None:
    while True:
        time.sleep(_SYNC_INTERVAL)
        try:
            _flush_once()
        except Exception:  # noqa: BLE001 — the daemon must never die
            logger.exception("throttle flusher tick crashed; continuing")


def _ensure_flusher() -> None:
    """Start the per-worker background flush thread once, lazily. Lazy start (vs
    module import) means it runs in the post-fork gunicorn worker rather than the
    master, so each worker gets exactly one daemon thread."""
    global _flusher_started
    if _flusher_started or not _SYNC_URL:
        return
    with _lock:
        if _flusher_started:
            return
        threading.Thread(target=_flusher_loop, name="throttle-sync", daemon=True).start()
        atexit.register(_flush_once)  # best-effort final flush on graceful shutdown
        _flusher_started = True


# ---------------------------------------------------------------------------
# The throttle decision (hot path, in-process, no network)
# ---------------------------------------------------------------------------
class _AsyncSyncedMixin:
    """Replace DRF's synchronous cache read/write with a local-count decision
    reconciled asynchronously. Reuses the parent throttle's scope, rate and
    ``get_cache_key`` (so anon/user keying and the auth tiering are unchanged)."""

    def allow_request(self, request, view):
        # No sync target configured -> behave exactly like the stock DRF throttle
        # (per-process cache). Keeps dev/test/off-cluster behaviour unchanged.
        if not _SYNC_URL:
            return super().allow_request(request, view)

        if self.rate is None:
            # An unlimited scope (no rate configured) is never throttled — parity
            # with SimpleRateThrottle.allow_request, and avoids ``// None`` below.
            return True

        self.key = self.get_cache_key(request, view)
        if self.key is None:
            # e.g. AnonRateThrottle returns None for authenticated users -> the
            # UserRateThrottle handles them. Not our request to throttle.
            return True

        # Fixed window keyed to the current period so it resets and expires
        # cleanly. self.num_requests / self.duration are set by SimpleRateThrottle
        # .__init__ from the configured rate ("1000/hour" -> 1000, 3600).
        window = int(time.time()) // self.duration
        rkey = f"{self.key}:{window}"

        _ensure_flusher()
        with _lock:
            # Only a request we actually allow consumes budget (matches DRF, and
            # stops a blocked client from inflating the shared counter forever).
            projected = _synced.get(rkey, 0) + _pending.get(rkey, 0) + 1
            if projected > self.num_requests:
                return False
            _pending[rkey] = _pending.get(rkey, 0) + 1
            _deadline[rkey] = (window + 2) * self.duration
            return True

    def wait(self):
        # In fallback mode DRF's history is populated, so defer to its precise
        # Retry-After. In sync mode we keep no per-request history (that's the
        # point), so omit a misleading Retry-After.
        if not _SYNC_URL:
            return super().wait()
        return None


class SyncedAnonRateThrottle(_AsyncSyncedMixin, AnonRateThrottle):
    """Anonymous bucket (scope ``anon``), reconciled asynchronously."""


class SyncedUserRateThrottle(_AsyncSyncedMixin, UserRateThrottle):
    """Authenticated bucket (scope ``user``), reconciled asynchronously."""
