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
