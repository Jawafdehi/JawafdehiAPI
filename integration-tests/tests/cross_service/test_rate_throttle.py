"""Live integration test for the approximate distributed rate-throttle.

The unit tests (jawafdehi_shared/drf/tests/test_throttling.py) prove the counting
logic. This proves the property that actually matters in prod and that unit tests
CAN'T show: with the container running multiple gunicorn workers, the anon cap is
enforced ~GLOBALLY (once the background sync reconciles) rather than per-worker —
i.e. the threat-model F14 fix holds end-to-end.

It is opt-in because it needs the container started with a low, fast-resetting
anon limit AND a sync target, e.g.:

    THROTTLE_RATE_ANON=30/min
    THROTTLE_SYNC_URL=redis://valkey:6379/0
    THROTTLE_SYNC_INTERVAL=1

then run with THROTTLE_IT_LIMIT=30 pointing at that container. Without
THROTTLE_IT_LIMIT set the test skips (the default suite runs a high anon rate so
it never trips the throttle — see conftest).
"""

import os

import httpx
import pytest

from conftest import PLATFORM_BASE_URL  # pytest puts the integration-tests dir on sys.path

# The numeric part of the container's configured anon rate. Skip unless set.
_LIMIT = os.environ.get("THROTTLE_IT_LIMIT")
# A cheap, public, anon-throttled GET (the surface the scraper hammered).
_PATH = os.environ.get("THROTTLE_IT_PATH", "/api/search/?type=material&page_size=1")
# Seconds to let the background flush reconcile worker-local counts to Redis.
_SETTLE = float(os.environ.get("THROTTLE_SYNC_SETTLE", "3"))

pytestmark = [
    pytest.mark.cross_service,
    pytest.mark.slow,
    pytest.mark.live,
    pytest.mark.skipif(
        _LIMIT is None,
        reason="set THROTTLE_IT_LIMIT (and start the container with a low "
        "THROTTLE_RATE_ANON + THROTTLE_SYNC_URL) to run the throttle IT",
    ),
]


def _raw_client() -> httpx.Client:
    """A plain client that does NOT ride out 429s — unlike the shared ``clients``
    fixture we WANT to observe throttling here."""
    return httpx.Client(base_url=PLATFORM_BASE_URL, timeout=15, follow_redirects=False)


def _fire(client, n):
    """Fire n GETs, return the list of status codes."""
    return [client.get(_PATH).status_code for _ in range(n)]


def test_anon_cap_is_global_across_workers():
    import time

    limit = int(_LIMIT)
    with _raw_client() as c:
        # Burst 1: consume roughly the whole global budget for this window.
        burst1 = _fire(c, limit)
        # Let the per-worker deltas reconcile into the shared counter.
        time.sleep(_SETTLE)
        # Burst 2: the global counter is now at/over the limit, so with a shared
        # (not per-worker) cap these should be overwhelmingly rejected.
        burst2 = _fire(c, limit)

    # No request may 5xx — throttling must reject cleanly, never error.
    assert not [s for s in burst1 + burst2 if s >= 500], "throttle path 500'd"
    # Burst 1 proves the endpoint was actually reachable/allowed to begin with.
    assert 200 in burst1, f"expected some 200s in burst1, got {set(burst1)}"

    allowed_after_reconcile = sum(1 for s in burst2 if s == 200)
    # The crux: after reconciliation the cap is global. A per-worker bug would let
    # ~limit/worker more through in burst2; a global cap lets almost none.
    assert allowed_after_reconcile <= max(2, limit // 5), (
        f"cap is not global: {allowed_after_reconcile}/{limit} still allowed after "
        f"the budget was spent and sync settled ({_SETTLE}s) — looks per-worker (F14)"
    )
    # And 429 is the rejection code (not 403/400/etc.).
    assert 429 in burst2, f"expected 429s once over the cap, got {set(burst2)}"
