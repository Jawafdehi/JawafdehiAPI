"""Tests for the public newsletter subscription API endpoint."""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from cases.models import NewsletterSubscription, NewsletterSubscriptionStatus


def newsletter_payload(**overrides):
    payload = {
        "firstName": "Ram",
        "email": "ram@example.com",
        "consentAccepted": True,
        "consentSource": "share_our_vision",
        "privacyVersion": "2026-07-06",
        "locale": "en",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestNewsletterSubscription:
    def test_subscribe_to_newsletter(self, api_client):
        response = api_client.post(
            "/api/newsletter/subscriptions/",
            newsletter_payload(lastName="Bahadur", email="RAM@example.COM"),
            format="json",
            REMOTE_ADDR="192.168.1.10",
            HTTP_X_FORWARDED_FOR="203.0.113.5",
            HTTP_USER_AGENT="pytest",
        )

        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "ram@example.com"
        assert data["status"] == NewsletterSubscriptionStatus.SUBSCRIBED
        assert "message" in data

        subscription = NewsletterSubscription.objects.get(email="ram@example.com")
        assert subscription.first_name == "Ram"
        assert subscription.last_name == "Bahadur"
        assert subscription.status == NewsletterSubscriptionStatus.SUBSCRIBED
        assert subscription.consent_accepted is True
        assert subscription.consented_at is not None
        assert subscription.consent_source == "share_our_vision"
        assert subscription.privacy_version == "2026-07-06"
        assert subscription.locale == "en"
        assert subscription.ip_address == "192.168.1.10"
        assert subscription.user_agent == "pytest"
        assert subscription.unsubscribe_token is not None

    def test_last_name_is_optional(self, api_client):
        response = api_client.post(
            "/api/newsletter/subscriptions/",
            newsletter_payload(firstName="Sita", email="sita@example.com"),
            format="json",
        )

        assert response.status_code == 201
        subscription = NewsletterSubscription.objects.get(email="sita@example.com")
        assert subscription.last_name == ""

    def test_subscribe_updates_existing_record_case_insensitively(self, api_client):
        existing = NewsletterSubscription.objects.create(
            email="ram@example.com",
            first_name="Old",
            last_name="Name",
            consent_accepted=True,
        )

        response = api_client.post(
            "/api/newsletter/subscriptions/",
            newsletter_payload(firstName="Ram", email="RAM@example.com"),
            format="json",
        )

        assert response.status_code == 201
        assert NewsletterSubscription.objects.count() == 1
        existing.refresh_from_db()
        assert existing.first_name == "Ram"
        assert existing.last_name == ""
        assert existing.status == NewsletterSubscriptionStatus.SUBSCRIBED

    def test_unsubscribed_record_is_not_restored_by_bare_signup(self, api_client):
        subscription = NewsletterSubscription.objects.create(
            email="ram@example.com",
            first_name="Old",
            last_name="Name",
            status=NewsletterSubscriptionStatus.UNSUBSCRIBED,
            consent_accepted=True,
        )

        response = api_client.post(
            "/api/newsletter/subscriptions/",
            newsletter_payload(firstName="Ram", email="ram@example.com"),
            format="json",
        )

        assert response.status_code == 409
        assert response.json()["code"] == "newsletter_unsubscribed"
        subscription.refresh_from_db()
        assert subscription.first_name == "Old"
        assert subscription.last_name == "Name"
        assert subscription.status == NewsletterSubscriptionStatus.UNSUBSCRIBED

    def test_unsubscribe_by_token(self, api_client):
        subscription = NewsletterSubscription.objects.create(
            email="ram@example.com",
            first_name="Ram",
            consent_accepted=True,
        )

        response = api_client.post(
            f"/api/newsletter/unsubscribe/{subscription.unsubscribe_token}/",
            {},
            format="json",
        )

        assert response.status_code == 200
        assert response.json()["status"] == NewsletterSubscriptionStatus.UNSUBSCRIBED
        subscription.refresh_from_db()
        assert subscription.status == NewsletterSubscriptionStatus.UNSUBSCRIBED
        assert subscription.unsubscribed_at is not None

    def test_invalid_unsubscribe_token_returns_404(self, api_client):
        response = api_client.post(
            "/api/newsletter/unsubscribe/00000000-0000-0000-0000-000000000000/",
            {},
            format="json",
        )

        assert response.status_code == 404

    def test_validation_errors(self, api_client):
        response = api_client.post(
            "/api/newsletter/subscriptions/",
            newsletter_payload(
                firstName="",
                email="not-an-email",
                consentAccepted=False,
            ),
            format="json",
        )

        assert response.status_code == 400
        data = response.json()
        assert data["error"] == "Validation error"
        assert "email" in data["details"]
        assert "firstName" in data["details"]
        assert "consentAccepted" in data["details"]

    def test_rate_limit_blocks_eleventh_submission(self, api_client):
        for i in range(10):
            response = api_client.post(
                "/api/newsletter/subscriptions/",
                newsletter_payload(firstName=f"User {i}", email=f"user{i}@example.com"),
                format="json",
                REMOTE_ADDR="192.168.1.20",
            )
            assert response.status_code == 201

        response = api_client.post(
            "/api/newsletter/subscriptions/",
            newsletter_payload(firstName="User 11", email="user11@example.com"),
            format="json",
            REMOTE_ADDR="192.168.1.20",
        )

        assert response.status_code == 429
        assert NewsletterSubscription.objects.count() == 10
