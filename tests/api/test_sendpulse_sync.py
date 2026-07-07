"""Tests for SendPulse newsletter synchronization."""

import json
import urllib.error

import pytest
from django.test import override_settings

from cases.models import NewsletterSubscription, NewsletterSubscriptionStatus
from cases.services import sendpulse
from cases.services.sendpulse import (
    SYNC_STATUS_FAILED,
    SYNC_STATUS_SUBSCRIBED,
    SYNC_STATUS_UNSUBSCRIBED,
    sync_subscription_to_sendpulse,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture
def sendpulse_settings():
    with override_settings(
        SENDPULSE_ENABLED=True,
        SENDPULSE_BASE_URL="https://api.sendpulse.test",
        SENDPULSE_ADDRESSBOOK_ID="12345",
        SENDPULSE_API_KEY="test-api-key",
        SENDPULSE_CLIENT_ID="",
        SENDPULSE_CLIENT_SECRET="",
        SENDPULSE_TIMEOUT_SECONDS=3,
    ):
        yield


@pytest.mark.django_db
def test_sync_subscription_adds_contact_to_sendpulse(monkeypatch, sendpulse_settings):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({"result": True})

    monkeypatch.setattr(sendpulse.urllib.request, "urlopen", fake_urlopen)
    subscription = NewsletterSubscription.objects.create(
        email="RAM@example.com",
        first_name="Ram",
        last_name="Bahadur",
        consent_accepted=True,
        consent_source="share_our_vision",
        privacy_version="2026-07-06",
        locale="en",
    )

    sync_subscription_to_sendpulse(subscription)

    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == "https://api.sendpulse.test/addressbooks/12345/emails"
    assert timeout == 3
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["emails"][0]["email"] == "ram@example.com"
    assert payload["emails"][0]["variables"]["first_name"] == "Ram"
    subscription.refresh_from_db()
    assert subscription.sendpulse_sync_status == SYNC_STATUS_SUBSCRIBED
    assert subscription.sendpulse_synced_at is not None
    assert subscription.sendpulse_sync_error == ""


@pytest.mark.django_db
def test_sync_unsubscribed_contact_to_sendpulse(monkeypatch, sendpulse_settings):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return FakeResponse({"result": True})

    monkeypatch.setattr(sendpulse.urllib.request, "urlopen", fake_urlopen)
    subscription = NewsletterSubscription.objects.create(
        email="sita@example.com",
        first_name="Sita",
        status=NewsletterSubscriptionStatus.UNSUBSCRIBED,
        consent_accepted=True,
    )

    sync_subscription_to_sendpulse(subscription)

    assert len(requests) == 1
    assert (
        requests[0].full_url
        == "https://api.sendpulse.test/addressbooks/12345/emails/unsubscribe"
    )
    payload = json.loads(requests[0].data.decode("utf-8"))
    assert payload == {"emails": ["sita@example.com"]}
    subscription.refresh_from_db()
    assert subscription.sendpulse_sync_status == SYNC_STATUS_UNSUBSCRIBED


@pytest.mark.django_db
def test_sync_failure_is_recorded(monkeypatch, sendpulse_settings):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(sendpulse.urllib.request, "urlopen", fake_urlopen)
    subscription = NewsletterSubscription.objects.create(
        email="fail@example.com",
        first_name="Fail",
        consent_accepted=True,
    )

    sync_subscription_to_sendpulse(subscription)

    subscription.refresh_from_db()
    assert subscription.sendpulse_sync_status == SYNC_STATUS_FAILED
    assert "network down" in subscription.sendpulse_sync_error
    assert subscription.sendpulse_last_attempt_at is not None
