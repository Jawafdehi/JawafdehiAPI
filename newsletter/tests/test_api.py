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


# -- Open House fields -------------------------------------------------------

OPEN_HOUSE_PAYLOAD = {
    "email": "diaspora@example.org",
    "firstName": "Sita",
    "consentAccepted": True,
    "consentSource": "openhouse_page",
    "privacyVersion": "2026-07-06",
    "region": "gulf-middle-east",
    "whatsapp": "+974 5512 3456",
    "organisation": "Nepali Forum Qatar",
}


def test_open_house_fields_forwarded_as_variables(client):
    """region/whatsapp/organisation reach SendPulse — they are what make a
    region-targeted invite possible later, and they cannot be backfilled."""
    fake = _mock_client()
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(
            reverse("newsletter:subscribe"), OPEN_HOUSE_PAYLOAD, format="json"
        )
    assert resp.status_code == 201
    variables = fake.add_subscriber.call_args.kwargs["variables"]
    assert variables["region"] == "gulf-middle-east"
    assert variables["organisation"] == "Nepali Forum Qatar"
    # Normalised to + and digits, so the stored number is dialable.
    assert variables["whatsapp"] == "+97455123456"


def test_first_name_is_sent_as_its_own_variable(client):
    """Campaign templates read {{first_name}}; unset it renders "Namaste ,"."""
    fake = _mock_client()
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    assert resp.status_code == 201
    assert fake.add_subscriber.call_args.kwargs["variables"]["first_name"] == "राम"


def test_open_house_fields_default_to_blank_for_newsletter_signups(client):
    """A newsletter signup still gets the keys, so contact shape is uniform."""
    fake = _mock_client()
    with mock.patch("newsletter.views.get_client", return_value=fake):
        client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    variables = fake.add_subscriber.call_args.kwargs["variables"]
    assert variables["region"] == ""
    assert variables["whatsapp"] == ""
    assert variables["organisation"] == ""


def test_unknown_region_is_rejected(client):
    """A closed list, because the point of the field is filtering the list."""
    payload = {**OPEN_HOUSE_PAYLOAD, "region": "atlantis"}
    with mock.patch("newsletter.views.get_client", return_value=_mock_client()):
        resp = client.post(reverse("newsletter:subscribe"), payload, format="json")
    assert resp.status_code == 400
    assert "region" in resp.data["details"]


@pytest.mark.parametrize("number", ["not a phone", "+977 98x 1234", "+++9779812", "123"])
def test_bad_whatsapp_numbers_are_rejected(client, number):
    payload = {**OPEN_HOUSE_PAYLOAD, "whatsapp": number}
    with mock.patch("newsletter.views.get_client", return_value=_mock_client()):
        resp = client.post(reverse("newsletter:subscribe"), payload, format="json")
    assert resp.status_code == 400
    assert "whatsapp" in resp.data["details"]


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("+977 9812 345678", "+9779812345678"),
        ("(977) 981-2345678", "9779812345678"),
        ("", ""),
    ],
)
def test_whatsapp_is_normalised(client, typed, stored):
    payload = {**OPEN_HOUSE_PAYLOAD, "whatsapp": typed}
    fake = _mock_client()
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(reverse("newsletter:subscribe"), payload, format="json")
    assert resp.status_code == 201
    assert fake.add_subscriber.call_args.kwargs["variables"]["whatsapp"] == stored


def test_open_house_signup_gets_the_open_house_welcome(client, settings):
    """One shared list, so consent_source is what picks the right welcome."""
    settings.SENDPULSE_WELCOME_EMAIL = True
    fake = _mock_client()
    fake.can_send_email = True
    with mock.patch("newsletter.views.get_client", return_value=fake):
        resp = client.post(
            reverse("newsletter:subscribe"), OPEN_HOUSE_PAYLOAD, format="json"
        )
    assert resp.status_code == 201
    subject, html = fake.send_email.call_args.args[1], fake.send_email.call_args.args[2]
    assert "Open House" in subject
    assert "Open House" in html
    # It must not greet them as a newsletter subscriber.
    assert "You're subscribed to the Jawafdehi newsletter" not in html


def test_newsletter_signup_still_gets_the_newsletter_welcome(client, settings):
    settings.SENDPULSE_WELCOME_EMAIL = True
    fake = _mock_client()
    fake.can_send_email = True
    with mock.patch("newsletter.views.get_client", return_value=fake):
        client.post(reverse("newsletter:subscribe"), VALID_PAYLOAD, format="json")
    html = fake.send_email.call_args.args[2]
    assert "You're subscribed to the Jawafdehi newsletter" in html
    assert "Open House" not in html
