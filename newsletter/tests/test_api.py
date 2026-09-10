"""HTTP-surface tests for the newsletter subscribe endpoint.

The `newsletter` app is a model-less proxy to SendPulse, so these tests mock the
SendPulse client (via ``newsletter.views.get_client``) and assert the status-code
contract the merged frontend depends on:

  subscribe → 201 ok · 202 ESP-down/unconfigured · 409 conflict · 400 invalid · 429 throttled
"""

from unittest import mock

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from newsletter.sendpulse import SendPulseError

VALID_PAYLOAD = {
    "email": "Reader@Example.org",
    "firstName": "राम",
    "lastName": "बहादुर",
    "consentAccepted": True,
    "consentSource": "newsletter_modal",
    "privacyVersion": "2026-07-06",
    "locale": "ne",
}


@pytest.fixture
def client():
    return APIClient()


def _mock_client():
    """A stand-in SendPulse client whose methods are inspectable mocks."""
    c = mock.Mock()
    c.add_subscriber.return_value = None
    return c


# -- subscribe ---------------------------------------------------------------


def test_subscribe_success_calls_sendpulse(client):
    fake = _mock_client()
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    assert resp.status_code == 201
    assert resp.data["status"] == "subscribed"
    # Email is normalized (trimmed + lowercased) before hitting the ESP.
    args, kwargs = fake.add_subscriber.call_args
    assert args[0] == "reader@example.org"
    # Consent metadata is forwarded as SendPulse variables (not dropped).
    assert kwargs["variables"]["consent_source"] == "newsletter_modal"
    assert kwargs["variables"]["privacy_version"] == "2026-07-06"
    # The given name lands in first_name, which is the variable every newsletter
    # template greets with. Without it the issue renders "Namaste ,".
    assert kwargs["variables"]["first_name"] == "राम"
    # The joined display name still rides along separately for SendPulse's own
    # Name column, and must not be what first_name picks up.
    assert kwargs["name"] == "राम बहादुर"


def test_subscribe_sets_first_name_without_last_name(client):
    """lastName is optional; first_name must still be populated without it."""
    fake = _mock_client()
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "lastName"}
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), payload, format="json")
    assert resp.status_code == 201
    _, kwargs = fake.add_subscriber.call_args
    assert kwargs["variables"]["first_name"] == "राम"
    assert kwargs["name"] == "राम"


def test_subscribe_unconfigured_esp_returns_202(client):
    """No SendPulse creds → accept locally (202) so the flow still works."""
    with mock.patch("newsletter.views.get_client", return_value=None):
        resp = client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    assert resp.status_code == 202
    assert resp.data["status"] == "accepted"


def test_subscribe_esp_outage_returns_202(client):
    """A transient SendPulse failure degrades to 202, not 500."""
    fake = _mock_client()
    fake.add_subscriber.side_effect = SendPulseError("timeout", status=None)
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    assert resp.status_code == 202


def test_subscribe_conflict_maps_to_409(client):
    """SendPulse 409 (already exists / previously unsubscribed) → local 409."""
    fake = _mock_client()
    fake.add_subscriber.side_effect = SendPulseError("exists", status=409)
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    assert resp.status_code == 409


def test_subscribe_requires_consent(client):
    fake = _mock_client()
    payload = {**VALID_PAYLOAD, "consentAccepted": False}
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), payload, format="json")
    assert resp.status_code == 400
    assert "consentAccepted" in resp.data["details"]
    fake.add_subscriber.assert_not_called()


def test_subscribe_rejects_bad_email(client):
    payload = {**VALID_PAYLOAD, "email": "not-an-email"}
    with mock.patch("newsletter.views.get_client", return_value=_mock_client()):
        resp = client.post(reverse("newsletter:subscribe"), payload, format="json")
    assert resp.status_code == 400
    assert "email" in resp.data["details"]


def test_subscribe_missing_required_field(client):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "firstName"}
    with mock.patch("newsletter.views.get_client", return_value=_mock_client()):
        resp = client.post(reverse("newsletter:subscribe"), payload, format="json")
    assert resp.status_code == 400
    assert "firstName" in resp.data["details"]


def test_subscribe_without_optional_fields(client):
    """lastName/locale are optional — a minimal payload still subscribes."""
    fake = _mock_client()
    payload = {
        "email": "a@b.co",
        "firstName": "Sita",
        "consentAccepted": True,
        "consentSource": "share_our_vision",
        "privacyVersion": "2026-07-06",
    }
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), payload, format="json")
    assert resp.status_code == 201


# -- throttle ----------------------------------------------------------------


def test_throttle_enforced_when_enabled(client, settings):
    """With TESTING off and creds unset, the 11th request in the window 429s."""
    settings.TESTING = False
    from django.core.cache import cache

    cache.clear()
    with mock.patch("newsletter.views.get_client", return_value=None):
        codes = [
            client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json").status_code
            for _ in range(11)
        ]
    assert codes.count(429) >= 1
    cache.clear()


# -- welcome email -----------------------------------------------------------


def test_subscribe_sends_welcome_when_enabled(client, settings):
    settings.SENDPULSE_WELCOME_EMAIL = True
    fake = _mock_client()
    fake.can_send_email = True
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    assert resp.status_code == 201
    fake.send_email.assert_called_once()
    # Sent to the (normalized) subscriber address, greeting them by first name.
    args, kwargs = fake.send_email.call_args
    assert args[0] == "reader@example.org"
    assert kwargs["to_name"] == "राम"


def test_subscribe_skips_welcome_when_disabled(client, settings):
    settings.SENDPULSE_WELCOME_EMAIL = False
    fake = _mock_client()
    fake.can_send_email = True
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    assert resp.status_code == 201
    fake.send_email.assert_not_called()


def test_welcome_failure_does_not_break_subscribe(client, settings):
    """A welcome-send error is swallowed — the subscribe still returns 201."""
    settings.SENDPULSE_WELCOME_EMAIL = True
    fake = _mock_client()
    fake.can_send_email = True
    fake.send_email.side_effect = SendPulseError("smtp down")
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    assert resp.status_code == 201
