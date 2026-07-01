"""The casework outbound clients send `Authorization: Bearer <token>`.

Phase5 switched jds_client, ngm_client and the review_poller from the legacy
DRF `Authorization: Token <key>` scheme to a Zitadel client-credentials bearer
obtained from review.oidc_client_credentials. These tests mock both the token
provider and the outbound HTTP call to assert the Bearer header is used (and the
old Token scheme is gone), and that the poller fails clearly when only the
deprecated CASEWORK_POLLER_TOKEN is set.
"""

import pytest
import requests

from review import jds_client, ngm_client
from review import oidc_client_credentials as cc


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", headers=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text
        self.content = b""
        self.headers = headers or {}

    def json(self):
        return self._json_body


@pytest.fixture
def oidc_configured(settings):
    """Configure the shared provider with stub credentials + a stub token."""
    settings.OIDC_ISSUER = "https://auth.example.test"
    settings.CASEWORK_OIDC_CLIENT_ID = "svc"
    settings.CASEWORK_OIDC_CLIENT_SECRET = "secret"
    settings.CASEWORK_OIDC_SCOPE = "openid"
    settings.CASEWORK_OIDC_AUDIENCE = ""
    settings.JAWAFDEHI_API_BASE = "https://jds.example.test/api"
    cc.reset_provider()
    yield settings
    cc.reset_provider()


def _stub_token(monkeypatch, token="access-xyz"):
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: FakeResponse(
            json_body={"access_token": token, "expires_in": 3600}
        ),
    )


# ---------------------------------------------------------------------------
# jds_client
# ---------------------------------------------------------------------------


def test_jds_get_case_sends_bearer(oidc_configured, monkeypatch):
    _stub_token(monkeypatch)
    captured = {}

    def fake_get(url, headers=None, timeout=None, params=None):
        captured["headers"] = headers
        return FakeResponse(json_body={"slug": "x"})

    monkeypatch.setattr(requests, "get", fake_get)
    jds_client.get_case("some-slug")

    assert captured["headers"]["Authorization"] == "Bearer access-xyz"
    assert not captured["headers"]["Authorization"].startswith("Token ")


def test_jds_no_auth_header_when_unconfigured(settings, monkeypatch):
    settings.OIDC_ISSUER = ""
    settings.CASEWORK_OIDC_CLIENT_ID = ""
    settings.CASEWORK_OIDC_CLIENT_SECRET = ""
    settings.JAWAFDEHI_API_BASE = "https://jds.example.test/api"
    cc.reset_provider()
    captured = {}

    def fake_get(url, headers=None, timeout=None, params=None):
        captured["headers"] = headers
        return FakeResponse(json_body={"slug": "x"})

    monkeypatch.setattr(requests, "get", fake_get)
    jds_client.get_case("some-slug")
    assert "Authorization" not in captured["headers"]
    cc.reset_provider()


# ---------------------------------------------------------------------------
# ngm_client
# ---------------------------------------------------------------------------


def test_ngm_get_court_case_sends_bearer(oidc_configured, monkeypatch):
    _stub_token(monkeypatch)
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse(json_body={"entities": []})

    monkeypatch.setattr(requests, "get", fake_get)
    ngm_client.get_court_case("special:081-CR-0079")

    assert captured["headers"]["Authorization"] == "Bearer access-xyz"
    assert "Token " not in captured["headers"]["Authorization"]


def test_ngm_no_auth_header_when_unconfigured(settings, monkeypatch):
    settings.OIDC_ISSUER = ""
    settings.CASEWORK_OIDC_CLIENT_ID = ""
    settings.CASEWORK_OIDC_CLIENT_SECRET = ""
    settings.JAWAFDEHI_API_BASE = "https://jds.example.test/api"
    cc.reset_provider()
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse(json_body={"entities": []})

    monkeypatch.setattr(requests, "get", fake_get)
    ngm_client.get_court_case("special:081-CR-0079")
    assert "Authorization" not in captured["headers"]
    cc.reset_provider()


# ---------------------------------------------------------------------------
# review_poller
# ---------------------------------------------------------------------------


def test_poller_headers_use_bearer(oidc_configured, monkeypatch):
    from review.management.commands.review_poller import Command

    _stub_token(monkeypatch, token="poller-tok")
    cmd = Command()
    cmd.token_provider = cc.get_provider()
    headers = cmd._headers()
    assert headers["Authorization"] == "Bearer poller-tok"
    assert headers["Content-Type"] == "application/json"


def test_poller_errors_when_only_legacy_token_set(settings):
    from django.core.management import call_command

    from review.management.commands.review_poller import PollerError

    settings.CASEWORK_POLLER_TOKEN = "legacy-drf-token"
    settings.CASEWORK_OIDC_CLIENT_ID = ""
    settings.CASEWORK_OIDC_CLIENT_SECRET = ""
    cc.reset_provider()

    with pytest.raises(PollerError) as exc:
        call_command("review_poller")
    assert "deprecated" in str(exc.value)
    assert "CASEWORK_OIDC_CLIENT_ID" in str(exc.value)
    cc.reset_provider()


def test_poller_errors_when_no_credentials(settings):
    from django.core.management import call_command

    from review.management.commands.review_poller import PollerError

    settings.CASEWORK_POLLER_TOKEN = ""
    settings.OIDC_ISSUER = ""
    settings.CASEWORK_OIDC_CLIENT_ID = ""
    settings.CASEWORK_OIDC_CLIENT_SECRET = ""
    cc.reset_provider()

    with pytest.raises(PollerError) as exc:
        call_command("review_poller")
    assert "not configured" in str(exc.value)
    cc.reset_provider()
