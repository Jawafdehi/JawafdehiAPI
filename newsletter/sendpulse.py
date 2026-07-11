"""Thin SendPulse REST client for newsletter subscribe.

SendPulse is the newsletter system of record: it stores subscribers, runs the
double opt-in confirmation email, hosts the unsubscribe link in its emails, and
owns list membership. This module wraps the address-book add call the subscribe
endpoint needs, plus SendPulse auth (a static API key used directly as a Bearer,
or the OAuth client-credentials flow with token caching).

Design notes
------------
- **Two auth modes.** SendPulse accepts either a static API key (newer scheme:
  ``Authorization: Bearer <sp_apikey_...>``, no token exchange) or the classic
  OAuth ``client_credentials`` pair. When ``SENDPULSE_API_KEY`` is set it is used
  directly and the OAuth round-trip / token cache are skipped entirely; otherwise
  the client falls back to ``SENDPULSE_CLIENT_ID`` / ``_CLIENT_SECRET``.
- **Config-gated.** When neither auth mode is configured (e.g. before the ESP is
  provisioned, or in CI), :func:`get_client` returns ``None`` and the views
  degrade gracefully rather than 500. This keeps the endpoints deployable and
  fully testable before credentials land.
- **Short timeouts.** Every request uses a small timeout so a slow/500 SendPulse
  can't hang a user's subscribe. Callers translate failures into a graceful
  ``202`` (the record is accepted; SendPulse sync is retried out of band).
- **Token cache.** In OAuth mode the access token is cached in Django's cache
  under a fixed key until shortly before it expires; a 401 forces a one-shot
  refresh. A static API key is long-lived, so it is never cached or refreshed.
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

    def __init__(self, addressbook_id: str, *, api_key: str = "",
                 client_id: str = "", client_secret: str = "",
                 timeout: float = _DEFAULT_TIMEOUT_SECONDS):
        self._addressbook_id = addressbook_id
        self._api_key = api_key
        self._client_id = client_id
        self._client_secret = client_secret
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
        # Static API key: use it verbatim, no exchange/cache/refresh.
        if self._api_key:
            return self._api_key
        if not force_refresh:
            cached = cache.get(_TOKEN_CACHE_KEY)
            if cached:
                return cached
        return self._fetch_token()

    def _request(self, method: str, path: str, *, json: Optional[dict] = None) -> requests.Response:
        """Issue an authenticated request, refreshing the token once on 401.

        A 401 refresh only helps in OAuth mode (an expired access token); a static
        API key would just be re-sent unchanged, so the retry is skipped for it.
        """
        token = self._token()
        resp = requests.request(
            method,
            f"{_API_BASE}{path}",
            json=json,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
        )
        if resp.status_code == 401 and not self._api_key:
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

    Auth is configured by either a static ``SENDPULSE_API_KEY`` (preferred: one
    secret, no token exchange) or the OAuth ``SENDPULSE_CLIENT_ID`` /
    ``_CLIENT_SECRET`` pair. An address book id is required for both.
    """
    # str()+strip(): tolerate an int address book id and drop stray whitespace/
    # newlines a copy-pasted key or a Bao→env round-trip can leave on the value.
    api_key = str(getattr(settings, "SENDPULSE_API_KEY", "") or "").strip()
    client_id = str(getattr(settings, "SENDPULSE_CLIENT_ID", "") or "").strip()
    client_secret = str(getattr(settings, "SENDPULSE_CLIENT_SECRET", "") or "").strip()
    addressbook_id = str(getattr(settings, "SENDPULSE_ADDRESSBOOK_ID", "") or "").strip()
    has_auth = bool(api_key) or bool(client_id and client_secret)
    if not (addressbook_id and has_auth):
        return None
    timeout = float(getattr(settings, "SENDPULSE_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))
    return SendPulseClient(
        addressbook_id,
        api_key=api_key,
        client_id=client_id,
        client_secret=client_secret,
        timeout=timeout,
    )
