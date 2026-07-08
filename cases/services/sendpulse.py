"""SendPulse sync for Jawafdehi newsletter subscriptions."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils import timezone

from cases.models import NewsletterSubscription, NewsletterSubscriptionStatus

logger = logging.getLogger(__name__)

SYNC_STATUS_DISABLED = "disabled"
SYNC_STATUS_SUBSCRIBED = "subscribed"
SYNC_STATUS_UNSUBSCRIBED = "unsubscribed"
SYNC_STATUS_FAILED = "failed"

_oauth_token: str | None = None
_oauth_expires_at = 0.0


class SendPulseConfigError(RuntimeError):
    """Raised when SendPulse sync is enabled but missing required settings."""


class SendPulseAPIError(RuntimeError):
    """Raised when SendPulse returns an error or malformed response."""


@dataclass(frozen=True)
class SendPulseConfig:
    enabled: bool
    base_url: str
    addressbook_id: str
    api_key: str
    client_id: str
    client_secret: str
    timeout: float

    @classmethod
    def from_settings(cls) -> "SendPulseConfig":
        return cls(
            enabled=settings.SENDPULSE_ENABLED,
            base_url=settings.SENDPULSE_BASE_URL,
            addressbook_id=str(settings.SENDPULSE_ADDRESSBOOK_ID).strip(),
            api_key=settings.SENDPULSE_API_KEY.strip(),
            client_id=settings.SENDPULSE_CLIENT_ID.strip(),
            client_secret=settings.SENDPULSE_CLIENT_SECRET.strip(),
            timeout=float(settings.SENDPULSE_TIMEOUT_SECONDS),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.addressbook_id:
            raise SendPulseConfigError("SENDPULSE_ADDRESSBOOK_ID is required.")
        if not self.api_key and not (self.client_id and self.client_secret):
            raise SendPulseConfigError(
                "Set SENDPULSE_API_KEY or both SENDPULSE_CLIENT_ID and SENDPULSE_CLIENT_SECRET."
            )


class SendPulseClient:
    """Minimal client for the SendPulse Email Service API."""

    def __init__(self, config: SendPulseConfig | None = None):
        self.config = config or SendPulseConfig.from_settings()
        self.config.validate()

    def add_subscription(self, data: dict[str, Any]) -> None:
        variables = {
            "first_name": data["first_name"],
            "last_name": data["last_name"],
            "source": data["consent_source"],
            "privacy_version": data["privacy_version"],
            "locale": data["locale"],
            "jawafdehi_status": data["status"],
        }
        self._request(
            "POST",
            f"/addressbooks/{self.config.addressbook_id}/emails",
            {
                "emails": [
                    {
                        "email": data["email"],
                        "variables": {
                            key: value for key, value in variables.items() if value
                        },
                    }
                ]
            },
        )

    def unsubscribe(self, data: dict[str, Any]) -> None:
        self._request(
            "POST",
            f"/addressbooks/{self.config.addressbook_id}/emails/unsubscribe",
            {"emails": [data["email"]]},
        )

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        return self._open_json(request)

    def _access_token(self) -> str:
        if self.config.api_key:
            return self.config.api_key

        global _oauth_token, _oauth_expires_at
        if _oauth_token and time.monotonic() < _oauth_expires_at - 60:
            return _oauth_token

        request = urllib.request.Request(
            f"{self.config.base_url}/oauth/access_token",
            data=json.dumps(
                {
                    "grant_type": "client_credentials",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        data = self._open_json(request)
        token = data.get("access_token")
        if not token:
            raise SendPulseAPIError("SendPulse token response did not include access_token.")
        _oauth_token = token
        _oauth_expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        return token

    def _open_json(self, request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SendPulseAPIError(
                f"SendPulse HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SendPulseAPIError(f"SendPulse request failed: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SendPulseAPIError("SendPulse returned non-JSON response.") from exc
        if isinstance(data, dict) and data.get("result") is False:
            raise SendPulseAPIError(f"SendPulse returned failure: {data}")
        return data


def subscription_payload(subscription: NewsletterSubscription) -> dict[str, Any]:
    """Snapshot the fields SendPulse needs into a plain dict.

    This is what the jobs consumer carries in ``job.payload`` so the worker can
    push to SendPulse without touching the database (see ``cases.job_handlers``).
    """
    return {
        "email": subscription.email,
        "first_name": subscription.first_name,
        "last_name": subscription.last_name,
        "consent_source": subscription.consent_source,
        "privacy_version": subscription.privacy_version,
        "locale": subscription.locale,
        "status": subscription.status,
    }


def push_subscription(data: dict[str, Any]) -> str:
    """Push one subscription's current state to SendPulse; return the sync status.

    Database-free (operates only on ``data`` + settings) so it is safe to run in
    the out-of-process jobs consumer. Raises on provider/config errors so the
    queue can retry; the disabled case returns ``SYNC_STATUS_DISABLED`` instead.
    """
    config = SendPulseConfig.from_settings()
    if not config.enabled:
        return SYNC_STATUS_DISABLED

    client = SendPulseClient(config)
    if data["status"] == NewsletterSubscriptionStatus.UNSUBSCRIBED:
        client.unsubscribe(data)
        return SYNC_STATUS_UNSUBSCRIBED
    client.add_subscription(data)
    return SYNC_STATUS_SUBSCRIBED


def sync_subscription_to_sendpulse(subscription: NewsletterSubscription) -> None:
    """Best-effort in-process sync that records its own outcome; never raises.

    Used by the ``sync_newsletter_sendpulse`` management command (a manual/cron
    resync where blocking is acceptable). The request path enqueues a
    ``newsletter_sendpulse`` job instead of calling this directly.
    """
    try:
        status = push_subscription(subscription_payload(subscription))
    except Exception as exc:  # noqa: BLE001 - provider sync must not break signup
        logger.warning(
            "sendpulse newsletter sync failed for subscription %s",
            subscription.pk,
            exc_info=True,
        )
        mark_sync_status(subscription.pk, SYNC_STATUS_FAILED, str(exc))
        return
    mark_sync_status(subscription.pk, status)


def mark_sync_status(subscription_id: int, status: str, error: str = "") -> None:
    """Record a subscription's latest SendPulse sync outcome (by primary key)."""
    now = timezone.now()
    update = {
        "sendpulse_sync_status": status,
        "sendpulse_sync_error": error[:4000],
        "sendpulse_last_attempt_at": now,
    }
    if status in {SYNC_STATUS_SUBSCRIBED, SYNC_STATUS_UNSUBSCRIBED}:
        update["sendpulse_synced_at"] = now
    NewsletterSubscription.objects.filter(pk=subscription_id).update(**update)
