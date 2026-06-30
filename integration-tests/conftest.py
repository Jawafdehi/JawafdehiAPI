"""Shared fixtures: monolith service clients, OIDC token, health gating.

The platform is now a SINGLE monolith image — all former services run in one
process behind ONE host/port (the ``platform`` service, default
``http://localhost:48000``). The three former services are mounted under
distinct path prefixes on that one host:

  * Jawafdehi      -> ``/api/...``        (cases/, sources/, search/)
  * NES (entities) -> ``/api/nes/...``    (entities, entity_prefixes, health)
  * NGM (gov data) -> ``/api/ngm/...``    (courts/, cases/, query/, ingestion/)
  * Unified search -> ``/api/search/``    (replaces the old per-service search)
  * Discovery      -> ``/sitemap.xml``, ``/.well-known/resourcesync``, ``/robots.txt``

The ``clients`` fixture therefore yields one httpx client PER former service,
but they all share the same base host and only differ by their mounted path
prefix (so test bodies still read ``clients["nes"].get("/api/nes/health")``).

Auth is OIDC/Zitadel client-credentials only — DRF tokens are not used anywhere
on the platform (a legacy ``Token`` header is ignored, not honored).
"""

import os
import time

import httpx
import pytest


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# The live monolith applies DRF's AnonRateThrottle (default 1000/hour for
# anonymous callers — generous enough that this fast suite never trips it in one
# run). This retry/skip net is a belt-and-suspenders safety margin: should a
# shared bucket be exhausted by other traffic, we retry a few times with a short
# backoff, and only if a 429 persists do tests skip (see ``skip_if_throttled``)
# rather than report a false contract failure.
THROTTLE_MAX_RETRIES = int(_env("THROTTLE_MAX_RETRIES", "2"))
THROTTLE_BACKOFF_S = float(_env("THROTTLE_BACKOFF_S", "0.3"))


class _ThrottleRetryTransport(httpx.HTTPTransport):
    """An httpx transport that rides out the live anon rate-throttle.

    Retries a 429 a few times with short backoff; if it STILL 429s (the hourly
    anon bucket is exhausted — Retry-After can be ~an hour, far too long to wait
    in-suite), it raises a pytest *skip* so the test is skipped with a clear
    reason rather than reported as a false contract failure. Authenticated runs
    (OIDC) hit the higher user scope and won't trip this.
    """

    def handle_request(self, request):
        resp = super().handle_request(request)
        attempts = 0
        while resp.status_code == 429 and attempts < THROTTLE_MAX_RETRIES:
            resp.read()
            resp.close()
            time.sleep(THROTTLE_BACKOFF_S * (attempts + 1))
            attempts += 1
            resp = super().handle_request(request)
        if resp.status_code == 429:
            resp.read()
            ra = resp.headers.get("Retry-After", "?")
            resp.close()
            pytest.skip(
                f"rate-throttled by the monolith (429, Retry-After={ra}s) — "
                "shared anon quota exhausted; not a contract failure."
            )
        return resp


def make_client(base_url, headers=None) -> httpx.Client:
    """Build a live httpx client with throttle-retry + APPEND_SLASH-visible 301s."""
    return httpx.Client(
        base_url=str(base_url),
        headers=headers or {},
        timeout=30,
        follow_redirects=False,
        transport=_ThrottleRetryTransport(),
    )


def skip_if_throttled(resp) -> None:
    """Skip (not fail) when the live anon rate-throttle is still firing.

    Keeps a red suite meaningful: a 429 is shared-quota infrastructure noise,
    not a contract regression. Authenticated runs (OIDC) hit the higher user
    scope and won't trip this.
    """
    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After", "?")
        pytest.skip(f"rate-throttled by the monolith (429, Retry-After={ra}s)")


SKIP_IF_DOWN = _env("SKIP_IF_STACK_DOWN", "0") == "1"

# ONE host for the whole monolith. ``PLATFORM_BASE_URL`` is the single source of
# truth; the per-service ``*_API_BASE_URL`` vars are kept for back-compat but
# default to the SAME host (they are no longer separate ports).
PLATFORM_BASE_URL = _env("PLATFORM_BASE_URL", "http://localhost:48000")

# Every former service shares the one monolith host; they differ only by the
# path prefix their routes are mounted under (see config/urls.py).
SERVICES = {
    "platform": _env("PLATFORM_BASE_URL", PLATFORM_BASE_URL),
    "nes": _env("NES_API_BASE_URL", PLATFORM_BASE_URL),
    "ngm": _env("NGM_API_BASE_URL", PLATFORM_BASE_URL),
    "jawafdehi": _env("JAWAFDEHI_API_BASE_URL", PLATFORM_BASE_URL),
}


@pytest.fixture(scope="session")
def oidc_token() -> str:
    """Acquire a client-credentials access token from Zitadel.

    Returns "" if OIDC isn't configured/reachable so smoke tests of public
    endpoints can still run; authed tests should assert the token is present.
    """
    issuer = _env("OIDC_ISSUER", "http://localhost:48080")
    client_id = _env("OIDC_CLIENT_ID")
    client_secret = _env("OIDC_CLIENT_SECRET")
    if not (client_id and client_secret):
        return ""
    try:
        resp = httpx.post(
            f"{issuer}/oauth/v2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "openid",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("access_token", "")
    except Exception:
        return ""


@pytest.fixture(scope="session")
def clients(oidc_token):
    """One httpx client per former service, all pointing at the one monolith host.

    The clients are distinguished only by convention (which path prefix the
    tests pass), so they all share the same base URL. follow_redirects=False so
    Django ``APPEND_SLASH`` 301s surface rather than being silently followed —
    test paths carry the trailing slash where the route requires it.
    """
    headers = {"Authorization": f"Bearer {oidc_token}"} if oidc_token else {}
    pool = {name: make_client(url, headers) for name, url in SERVICES.items()}
    yield pool
    for c in pool.values():
        c.close()


def _is_up(base_url: str) -> bool:
    """The monolith is up iff its Jawafdehi API root answers (<500)."""
    for path in ("/api/", "/api/nes/health", "/"):
        try:
            if httpx.get(f"{base_url}{path}", timeout=3).status_code < 500:
                return True
        except Exception:
            continue
    return False


@pytest.fixture(scope="session")
def _stack_status() -> list[str]:
    """Empty if the monolith is reachable; otherwise names it as down."""
    return [] if _is_up(PLATFORM_BASE_URL) else ["platform"]


@pytest.fixture(autouse=True)
def require_stack(request, _stack_status):
    """Gate *live* tests on the monolith being reachable.

    Only tests marked ``@pytest.mark.live`` need a running stack. Pure-contract
    tests (e.g. the entity-id IRI shape checks against fixtures) run regardless,
    so ``pytest --collect-only`` and the fast contract subset work with no stack.

    For live tests: skip (if SKIP_IF_STACK_DOWN=1) or fail fast with a clear
    message otherwise, so a red suite means a real regression, not just
    "stack wasn't running."
    """
    if request.node.get_closest_marker("live") is None:
        return
    if _stack_status:
        msg = f"monolith unreachable at {PLATFORM_BASE_URL} — is the stack up?"
        if SKIP_IF_DOWN:
            pytest.skip(msg)
        pytest.fail(msg, pytrace=False)
