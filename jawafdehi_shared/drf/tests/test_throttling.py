"""Unit tests for the approximate distributed throttle.

These exercise the module's own logic (local counting + async reconciliation)
directly, without the DRF request stack — the novel, concurrency-sensitive part.
The stock DRF fall-back (THROTTLE_SYNC_URL unset) is covered by the integration
suite that hits a running container.
"""

import time

import pytest

from jawafdehi_shared.drf import throttling as T
from jawafdehi_shared.drf.throttling import SyncedAnonRateThrottle


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset process-wide state and pretend a sync target is configured, but don't
    let allow_request spawn the background flush thread (tests flush by hand)."""
    T._pending.clear()
    T._synced.clear()
    T._deadline.clear()
    monkeypatch.setattr(T, "_SYNC_URL", "redis://test:6379/0")
    monkeypatch.setattr(T, "_redis", None)
    monkeypatch.setattr(T, "_redis_broken", False)
    monkeypatch.setattr(T, "_ensure_flusher", lambda: None)
    yield
    T._pending.clear()
    T._synced.clear()
    T._deadline.clear()


def _throttle(limit, key="throttle_anon_1.2.3.4", duration=3600):
    """A Synced throttle with its rate/keying stubbed, bypassing __init__ (which
    would need DRF THROTTLE_RATES configured). Mirrors what SimpleRateThrottle
    .__init__ sets: rate, num_requests, duration."""
    t = SyncedAnonRateThrottle.__new__(SyncedAnonRateThrottle)
    t.rate = f"{limit}/hour"
    t.num_requests = limit
    t.duration = duration
    t.get_cache_key = lambda request, view: key
    return t


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.expires = {}

    def incrby(self, k, n):
        self.store[k] = self.store.get(k, 0) + n
        return self.store[k]

    def expire(self, k, s):
        self.expires[k] = s


def test_allows_up_to_limit_then_denies():
    t = _throttle(3)
    assert [t.allow_request(None, None) for _ in range(5)] == [True, True, True, False, False]


def test_denied_requests_do_not_consume_budget():
    """A blocked client must not keep inflating the counter (DRF semantics)."""
    t = _throttle(2)
    [t.allow_request(None, None) for _ in range(5)]  # 2 allowed, 3 denied
    window = int(time.time()) // t.duration
    assert T._pending[f"throttle_anon_1.2.3.4:{window}"] == 2  # only the successes


def test_synced_global_counts_against_local_estimate():
    t = _throttle(3)
    window = int(time.time()) // t.duration
    T._synced[f"throttle_anon_1.2.3.4:{window}"] = 2  # other workers already used 2
    assert [t.allow_request(None, None) for _ in range(3)] == [True, False, False]


def test_none_cache_key_is_never_throttled():
    """AnonRateThrottle returns None for authenticated users -> always allow."""
    t = _throttle(1, key=None)
    assert all(t.allow_request(None, None) for _ in range(10))


def test_unlimited_scope_is_never_throttled():
    """rate=None (no rate configured for the scope) -> never throttled, DRF parity."""
    t = _throttle(1)
    t.rate = None
    assert all(t.allow_request(None, None) for _ in range(10))


def test_falls_back_to_stock_when_no_sync_url(monkeypatch):
    monkeypatch.setattr(T, "_SYNC_URL", "")
    called = {}
    monkeypatch.setattr(
        T.AnonRateThrottle, "allow_request",
        lambda self, request, view: called.setdefault("stock", True) or True,
    )
    t = _throttle(3)
    assert t.allow_request(None, None) is True
    assert called.get("stock") is True


def test_malformed_sync_url_disables_sync_once(monkeypatch):
    """A bad URL is a permanent misconfig: disable once (no per-tick log spam)."""
    monkeypatch.setattr(T, "_SYNC_URL", "not-a-valid-url")
    monkeypatch.setattr(T, "_redis", None)
    monkeypatch.setattr(T, "_redis_broken", False)
    assert T._get_redis() is None
    assert T._redis_broken is True
    assert T._get_redis() is None  # short-circuits, does not retry construction


def test_flush_folds_pending_into_redis_and_refreshes_synced():
    t = _throttle(100)
    for _ in range(5):
        t.allow_request(None, None)
    window = int(time.time()) // t.duration
    rkey = f"throttle_anon_1.2.3.4:{window}"

    fake = FakeRedis()
    T._flush_once(client=fake)

    assert fake.store[rkey] == 5           # delta pushed to shared counter
    assert fake.expires[rkey] == 7200      # window self-cleans in Redis
    assert T._synced[rkey] == 5            # local view refreshed
    assert T._pending[rkey] == 0           # delta cleared


def test_flush_is_fail_open_and_retries_delta_next_tick():
    t = _throttle(100)
    for _ in range(4):
        t.allow_request(None, None)
    window = int(time.time()) // t.duration
    rkey = f"throttle_anon_1.2.3.4:{window}"

    class BoomRedis:
        def incrby(self, k, n):
            raise RuntimeError("valkey unreachable")

        def expire(self, k, s):
            pass

    T._flush_once(client=BoomRedis())      # must not raise

    assert T._pending[rkey] == 4           # delta returned to the pool
    assert rkey not in T._synced           # nothing was reconciled


def test_global_estimate_survives_flush():
    """After a flush, the reconciled total keeps counting toward the limit."""
    t = _throttle(3)
    t.allow_request(None, None)
    t.allow_request(None, None)
    T._flush_once(client=FakeRedis())      # synced=2, pending=0
    assert t.allow_request(None, None) is True   # 3rd allowed
    assert t.allow_request(None, None) is False  # 4th over the cap


def test_prune_drops_stale_windows_but_keeps_live_ones():
    t = _throttle(100)
    t.allow_request(None, None)
    window = int(time.time()) // t.duration
    rkey = f"throttle_anon_1.2.3.4:{window}"
    T._flush_once(client=FakeRedis())      # pending -> 0
    T._deadline[rkey] = time.time() - 1    # force it stale

    # A live (future-deadline) key with in-flight pending must survive prune.
    live = "throttle_anon_5.6.7.8:live"
    T._pending[live] = 1
    T._deadline[live] = time.time() + 3600

    T._prune()
    assert rkey not in T._pending and rkey not in T._synced and rkey not in T._deadline
    assert live in T._pending and live in T._deadline
