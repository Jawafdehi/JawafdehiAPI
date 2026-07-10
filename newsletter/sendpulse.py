"""Thin SendPulse REST client for newsletter subscribe.

SendPulse is the newsletter system of record: it stores subscribers, runs the
double opt-in confirmation email, hosts the unsubscribe link in its emails, and
owns list membership. This module wraps the address-book add call the subscribe
endpoint needs, plus OAuth token caching.

Design notes
------------
- **Config-gated.** When the ``SENDPULSE_*`` settings are unset (e.g. before the
  ESP is provisioned, or in CI), :func:`get_client` returns ``None`` and the
  views degrade gracefully rather than 500. This keeps the endpoints deployable
  and fully testable before credentials land.
- **Short timeouts.** Every request uses a small timeout so a slow/500 SendPulse
  can't hang a user's subscribe. Callers translate failures into a graceful
  ``202`` (the record is accepted; SendPulse sync is retried out of band).
- **Token cache.** The OAuth access token is cached in Django's cache under a
  fixed key until shortly before it expires; a 401 forces a one-shot refresh.
- **No PII logged.** Errors log status codes and SendPulse error bodies, never the
  subscriber's email address.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("newsletter.sendpulse")

_API_BASE = "https://api.sendpulse.com"
_TOKEN_CACHE_KEY = "newsletter:sendpulse:access_token"
# Refresh a little before the real expiry to avoid using a token that dies mid-flight.
_TOKEN_EXPIRY_SKEW_SECONDS = 60
_DEFAULT_TIMEOUT_SECONDS = 5.0


class SendPulseError(Exception):
    """Raised when a SendPulse call fails after (at most) one token refresh.

    ``status`` is the upstream HTTP status when available (``None`` for transport
    errors like a timeout), so callers can distinguish an already-exists conflict
    from a transient outage.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class SendPulseClient:
    """Minimal SendPulse Marketing API client (address-book membership only)."""

    def __init__(self, client_id: str, client_secret: str, addressbook_id: str,
                 timeout: float = _DEFAULT_TIMEOUT_SECONDS):
        self._client_id = client_id
        self._client_secret = client_secret
        self._addressbook_id = addressbook_id
        self._timeout = timeout

    # -- auth ---------------------------------------------------------------

    def _fetch_token(self) -> str:
        """Obtain a fresh OAuth access token and cache it until near expiry."""
        resp = requests.post(
            f"{_API_BASE}/oauth/access_token",
            json={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise SendPulseError(
                f"SendPulse token request failed ({resp.status_code})",
                status=resp.status_code,
            )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise SendPulseError("SendPulse token response missing access_token")
        ttl = int(data.get("expires_in", 3600)) - _TOKEN_EXPIRY_SKEW_SECONDS
        if ttl > 0:
            cache.set(_TOKEN_CACHE_KEY, token, ttl)
        return token

    def _token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = cache.get(_TOKEN_CACHE_KEY)
            if cached:
                return cached
        return self._fetch_token()

    def _request(self, method: str, path: str, *, json: Optional[dict] = None) -> requests.Response:
        """Issue an authenticated request, refreshing the token once on 401."""
        token = self._token()
        resp = requests.request(
            method,
            f"{_API_BASE}{path}",
            json=json,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
        )
        if resp.status_code == 401:
            token = self._token(force_refresh=True)
            resp = requests.request(
                method,
                f"{_API_BASE}{path}",
                json=json,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
        return resp

    # -- operations ---------------------------------------------------------

    def add_subscriber(self, email: str, *, name: str = "", variables: Optional[dict] = None) -> None:
        """Add an email to the configured address book.

        SendPulse triggers its own double opt-in confirmation for the address
        book when configured to do so. Raises :class:`SendPulseError` on failure;
        the ``status`` attribute carries the upstream HTTP status when available.
        """
        payload = {
            "emails": [
                {
                    "email": email,
                    "variables": {**({"name": name} if name else {}), **(variables or {})},
                }
            ]
        }
        try:
            resp = self._request(
                "POST", f"/addressbooks/{self._addressbook_id}/emails", json=payload
            )
        except requests.RequestException as exc:  # timeout / connection error
            raise SendPulseError(f"SendPulse request error: {exc}") from exc

        if resp.status_code >= 400:
            raise SendPulseError(
                f"SendPulse add_subscriber failed ({resp.status_code}): {resp.text[:200]}",
                status=resp.status_code,
            )


def get_client() -> Optional[SendPulseClient]:
    """Return a configured client, or ``None`` when SendPulse isn't provisioned.

    Views must treat ``None`` as "ESP not wired yet" and degrade gracefully — the
    subscription is still accepted so the flow works end-to-end before creds land.
    """
    client_id = getattr(settings, "SENDPULSE_CLIENT_ID", "") or ""
    client_secret = getattr(settings, "SENDPULSE_CLIENT_SECRET", "") or ""
    addressbook_id = getattr(settings, "SENDPULSE_ADDRESSBOOK_ID", "") or ""
    if not (client_id and client_secret and addressbook_id):
        return None
    timeout = float(getattr(settings, "SENDPULSE_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))
    return SendPulseClient(client_id, client_secret, addressbook_id, timeout=timeout)
