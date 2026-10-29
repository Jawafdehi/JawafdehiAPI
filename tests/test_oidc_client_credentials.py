"""Unit tests for the OIDC client-credentials token provider.

These mock ``requests.post`` so they run with no live Zitadel instance. They
assert the provider: POSTs the client-credentials grant to the right endpoint,
caches the access token, refreshes ~60s before expiry, sends/returns a Bearer
header, and raises ``OIDCTokenError`` with a clear message on auth failure.
"""

import time

import pytest
import requests

from review import oidc_client_credentials as cc


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


def _provider(**overrides):
    kwargs = {
        "issuer": "https://auth.example.test",
        "client_id": "svc-client",
        "client_secret": "svc-secret",
        "scope": "openid urn:zitadel:iam:org:project:id:123:aud",
        "audience": "",
    }
    kwargs.update(overrides)
    return cc.ClientCredentialsTokenProvider(**kwargs)


# ---------------------------------------------------------------------------
# token request shape
# ---------------------------------------------------------------------------


def test_posts_client_credentials_grant_to_token_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return FakeResponse(json_body={"access_token": "tok-1", "expires_in": 3600})

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()
    token = p.get_token()

    assert token == "tok-1"
    assert captured["url"] == "https://auth.example.test/oauth/v2/token"
    assert captured["data"]["grant_type"] == "client_credentials"
    assert captured["data"]["client_id"] == "svc-client"
    assert captured["data"]["client_secret"] == "svc-secret"
    assert captured["data"]["scope"].endswith(":aud")
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_audience_form_field_sent_only_when_configured(monkeypatch):
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen["data"] = data
        return FakeResponse(json_body={"access_token": "t", "expires_in": 3600})

    monkeypatch.setattr(requests, "post", fake_post)

    _provider().get_token()
    assert "audience" not in seen["data"]

    _provider(audience="my-api").get_token()
    assert seen["data"]["audience"] == "my-api"


# ---------------------------------------------------------------------------
# caching + refresh
# ---------------------------------------------------------------------------


def test_caches_token_across_calls(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(
            json_body={"access_token": f"tok-{calls['n']}", "expires_in": 3600}
        )

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()

    assert p.get_token() == "tok-1"
    assert p.get_token() == "tok-1"  # served from cache
    assert p.get_token() == "tok-1"
    assert calls["n"] == 1  # endpoint hit exactly once


def test_refreshes_near_expiry(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        # 30s lifetime: with the 60s skew the deadline is already in the past,
        # so the very next call must refresh.
        return FakeResponse(
            json_body={"access_token": f"tok-{calls['n']}", "expires_in": 30}
        )

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()

    assert p.get_token() == "tok-1"
    # lifetime(30) - skew(60) < 0 -> expires immediately -> refresh
    assert p.get_token() == "tok-2"
    assert calls["n"] == 2


def test_does_not_refresh_before_skew_window(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(
            json_body={"access_token": f"tok-{calls['n']}", "expires_in": 3600}
        )

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()
    p.get_token()

    # Advance time to just inside the validity window (well before exp - 60s).
    real_monotonic = time.monotonic
    monkeypatch.setattr(cc.time, "monotonic", lambda: real_monotonic() + 100)
    assert p.get_token() == "tok-1"
    assert calls["n"] == 1


def test_force_refresh_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(
            json_body={"access_token": f"tok-{calls['n']}", "expires_in": 3600}
        )

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()
    assert p.get_token() == "tok-1"
    assert p.get_token(force_refresh=True) == "tok-2"
    assert calls["n"] == 2


def test_invalidate_forces_refetch(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(
            json_body={"access_token": f"tok-{calls['n']}", "expires_in": 3600}
        )

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()
    assert p.get_token() == "tok-1"
    p.invalidate()
    assert p.get_token() == "tok-2"


def test_missing_expires_in_uses_default_lifetime(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return FakeResponse(json_body={"access_token": "tok"})  # no expires_in

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()
    assert p.get_token() == "tok"
    # cached for ~ default lifetime - skew, i.e. well into the future
    assert p._expires_at > time.monotonic() + 60


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_unconfigured_provider_errors_clearly(monkeypatch):
    p = _provider(client_id="", client_secret="")
    with pytest.raises(cc.OIDCTokenError) as exc:
        p.get_token()
    assert "not configured" in str(exc.value)
    assert "CASEWORK_OIDC_CLIENT_ID" in str(exc.value)


def test_http_error_raises_oidc_token_error(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return FakeResponse(status_code=401, text="invalid_client")

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()
    with pytest.raises(cc.OIDCTokenError) as exc:
        p.get_token()
    assert "HTTP 401" in str(exc.value)
    assert "invalid_client" in str(exc.value)


def test_network_error_raises_oidc_token_error(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()
    with pytest.raises(cc.OIDCTokenError) as exc:
        p.get_token()
    assert "failed" in str(exc.value)


def test_response_without_access_token_errors(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return FakeResponse(json_body={"token_type": "Bearer"})

    monkeypatch.setattr(requests, "post", fake_post)
    p = _provider()
    with pytest.raises(cc.OIDCTokenError) as exc:
        p.get_token()
    assert "access_token" in str(exc.value)


# ---------------------------------------------------------------------------
# module-level provider + bearer helpers (read from settings)
# ---------------------------------------------------------------------------


def test_get_provider_reads_settings_and_caches(settings, monkeypatch):
    settings.OIDC_ISSUER = "https://auth.example.test"
    settings.CASEWORK_OIDC_CLIENT_ID = "svc"
    settings.CASEWORK_OIDC_CLIENT_SECRET = "secret"
    settings.CASEWORK_OIDC_SCOPE = "openid"
    settings.CASEWORK_OIDC_AUDIENCE = ""
    cc.reset_provider()

    p1 = cc.get_provider()
    p2 = cc.get_provider()
    assert p1 is p2
    assert p1.token_endpoint == "https://auth.example.test/oauth/v2/token"


def test_bearer_header_returns_bearer_scheme(settings, monkeypatch):
    settings.OIDC_ISSUER = "https://auth.example.test"
    settings.CASEWORK_OIDC_CLIENT_ID = "svc"
    settings.CASEWORK_OIDC_CLIENT_SECRET = "secret"
    settings.CASEWORK_OIDC_SCOPE = "openid"
    settings.CASEWORK_OIDC_AUDIENCE = ""
    cc.reset_provider()

    def fake_post(url, data=None, headers=None, timeout=None):
        return FakeResponse(json_body={"access_token": "abc", "expires_in": 3600})

    monkeypatch.setattr(requests, "post", fake_post)
    header = cc.bearer_header()
    assert header == {"Authorization": "Bearer abc"}
    cc.reset_provider()


# ---------------------------------------------------------------------------
# transport vs configuration: which failures are worth retrying
#
# The poller used to treat every OIDCTokenError as fatal before entering its
# loop, so one read timeout against the issuer killed a whole CronJob run while
# auth.jawafdehi.org was serving thousands of requests with no errors. These
# pin the split that makes that distinction possible.
# ---------------------------------------------------------------------------


def test_network_error_is_a_transport_error(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cc.OIDCTransportError):
        _provider().get_token()


def test_read_timeout_is_a_transport_error(monkeypatch):
    """The exact shape that killed the poller in production."""

    def fake_post(url, data=None, headers=None, timeout=None):
        raise requests.ReadTimeout("Read timed out. (read timeout=30)")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cc.OIDCTransportError):
        _provider().get_token()


@pytest.mark.parametrize("status", [400, 401])
def test_a_credential_verdict_is_a_configuration_error(monkeypatch, status):
    """RFC 6749 5.2 defines exactly these two for the token endpoint."""
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse(status_code=status, text="invalid_client")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cc.OIDCConfigurationError):
        _provider().get_token()
    # A credential the issuer has already refused is not re-sent: retrying it
    # invites a lockout and buries the real error.
    assert len(calls) == 1


@pytest.mark.parametrize("status", [403, 404, 405, 408, 429, 500, 502, 503, 504])
def test_anything_that_is_not_a_verdict_is_a_transport_error(monkeypatch, status):
    """403 is a WAF, 404 is an ingress whose issuer route is not published yet.

    Neither is the token endpoint refusing the credential, and treating them as
    fatal is the same mistake this split exists to end — just moved one hop in
    front of the issuer.
    """
    def fake_post(url, data=None, headers=None, timeout=None):
        return FakeResponse(status_code=status, text="upstream unavailable")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cc.OIDCTransportError):
        _provider().get_token()


def test_unconfigured_is_a_configuration_error():
    with pytest.raises(cc.OIDCConfigurationError):
        _provider(client_id="", client_secret="").get_token()


def test_non_json_200_is_a_transport_error(monkeypatch):
    """A 200 of HTML is a proxy talking, not the issuer."""

    def fake_post(url, data=None, headers=None, timeout=None):
        return FakeResponse(json_body=None, text="<html>504 Gateway Timeout</html>")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cc.OIDCTransportError):
        _provider().get_token()


def test_200_without_access_token_is_a_configuration_error(monkeypatch):
    """Well-formed JSON means the issuer answered, and declined."""

    def fake_post(url, data=None, headers=None, timeout=None):
        return FakeResponse(json_body={"token_type": "Bearer"})

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cc.OIDCConfigurationError):
        _provider().get_token()


def test_the_token_endpoint_is_called_exactly_once(monkeypatch):
    """No retry here, on purpose.

    `_refresh_locked` runs under `self._lock`, so a retry loop multiplies every
    caller's worst case by the retry count AND serialises every other thread
    behind it. `review/ngm_client` and `review/jds_client` both do
    `try: bearer_header() except OIDCTokenError: pass` on each outbound read —
    they degrade to an unauthenticated call by design, and making them wait 3x
    the timeout first would stall the worker pool to no purpose. Callers that
    can afford to wait retry at their own cadence; the classification is what
    tells them it is worth waiting on.
    """
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(url)
        raise requests.ReadTimeout("read timeout")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cc.OIDCTransportError):
        _provider().get_token()
    assert len(calls) == 1


def test_both_subclasses_remain_catchable_as_the_base_error(monkeypatch):
    """Existing `except OIDCTokenError` handlers must keep working."""
    assert issubclass(cc.OIDCTransportError, cc.OIDCTokenError)
    assert issubclass(cc.OIDCConfigurationError, cc.OIDCTokenError)

    def fake_post(url, data=None, headers=None, timeout=None):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cc.OIDCTokenError):
        _provider().get_token()
