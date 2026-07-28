"""OIDC client-credentials token provider for the casework outbound clients.

Phase5 made the Jawafdehi API OIDC-only: the legacy DRF ``Authorization: Token
<key>`` scheme was removed. The poller and the review/ HTTP clients
(``review_poller``, ``jds_client``, ``ngm_client``) are *outbound* callers that
authenticate as a dedicated Zitadel **service account** (a machine user granted
the Caseworker / ReviewAssistant role). They obtain a JWT **access token** via
Zitadel's client-credentials grant and present it as ``Authorization: Bearer
<access_token>``.

This module is the single place that talks to the Zitadel token endpoint
(``${OIDC_ISSUER}/oauth/v2/token``). It POSTs ``grant_type=client_credentials``
with the service account's ``client_id`` / ``client_secret`` plus the mandatory
audience scope (``urn:zitadel:iam:org:project:id:{projectId}:aud``) and the role
scope (``urn:zitadel:iam:org:projects:roles`` — note the plural ``projects``) so
the receiver's ``aud`` check passes and roles land in the token. There is no
refresh_token for M2M, so the provider caches the access token in-process and
re-requests it ~60s before ``expires_in`` lapses.

See ``/damodaha-volunteer/think-big/shared/research/oidc-zitadel-integration.md``
§3 for the platform decision and the exact token request shape.
"""

from __future__ import annotations

import os
import threading
import time

import requests
from django.conf import settings

# Re-request the token this many seconds before it actually expires, so an
# in-flight request never races the expiry boundary (clock skew + network).
_EXPIRY_SKEW_SECONDS = 60

# Fall back to this lifetime if the token endpoint omits expires_in (it
# shouldn't, but never cache "forever").
_DEFAULT_LIFETIME_SECONDS = 3600


class OIDCTokenError(Exception):
    """Raised when a service-account access token cannot be obtained."""


class ClientCredentialsTokenProvider:
    """Fetch + cache a Zitadel service-account access token (client-credentials).

    Thread-safe: a single lock guards the cached token so concurrent callers
    share one token and at most one of them hits the token endpoint at a time.
    Construct once and reuse; ``get_token()`` returns the cached token until it
    is within ``_EXPIRY_SKEW_SECONDS`` of expiry, then transparently refreshes.
    """

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
        audience: str = "",
        timeout: float = 30.0,
    ):
        self._issuer = (issuer or "").rstrip("/")
        self._client_id = client_id or ""
        self._client_secret = client_secret or ""
        self._scope = scope or ""
        self._audience = audience or ""
        self._timeout = timeout

        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0  # monotonic deadline (skew already applied)

    @property
    def token_endpoint(self) -> str:
        return f"{self._issuer}/oauth/v2/token"

    def _configured(self) -> bool:
        return bool(self._issuer and self._client_id and self._client_secret)

    def get_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, fetching/refreshing as needed.

        Raises ``OIDCTokenError`` with a clear message when the provider is not
        configured or the token endpoint rejects the credentials.
        """
        if not self._configured():
            raise OIDCTokenError(
                "OIDC client-credentials are not configured. Set OIDC_ISSUER, "
                "CASEWORK_OIDC_CLIENT_ID and CASEWORK_OIDC_CLIENT_SECRET to the "
                "Zitadel service account that the casework poller/clients should "
                "authenticate as (client-credentials grant)."
            )

        with self._lock:
            if (
                not force_refresh
                and self._access_token is not None
                and time.monotonic() < self._expires_at
            ):
                return self._access_token
            return self._refresh_locked()

    def _refresh_locked(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        # The audience scope is load-bearing: without it the receiver's `aud`
        # check fails. Roles scope (plural `projects`) carries the role claim.
        if self._scope:
            data["scope"] = self._scope
        if self._audience:
            # Zitadel reads the target project from the scope, but some
            # deployments also honour an explicit `audience` form field; send it
            # when configured so either wiring works.
            data["audience"] = self._audience

        try:
            resp = requests.post(
                self.token_endpoint,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise OIDCTokenError(
                f"OIDC token request to {self.token_endpoint} failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise OIDCTokenError(
                f"OIDC token request to {self.token_endpoint} returned "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise OIDCTokenError(
                f"OIDC token endpoint returned a non-JSON body: {resp.text[:300]}"
            ) from exc

        access_token = body.get("access_token")
        if not access_token:
            raise OIDCTokenError(
                f"OIDC token response did not contain an access_token: {body!r}"
            )

        expires_in = body.get("expires_in")
        try:
            lifetime = int(expires_in)
        except (TypeError, ValueError):
            lifetime = _DEFAULT_LIFETIME_SECONDS

        # Cache until shortly before the real expiry. Never go negative for very
        # short-lived tokens — refresh on the next call instead.
        self._access_token = access_token
        self._expires_at = time.monotonic() + max(0, lifetime - _EXPIRY_SKEW_SECONDS)
        return access_token

    def invalidate(self) -> None:
        """Drop the cached token so the next ``get_token()`` re-fetches.

        Useful if a caller gets a 401 despite a non-expired cached token (e.g.
        the token was revoked server-side)."""
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0


# Module-level singleton built lazily from settings, so the three clients share
# one cached token and management commands can import this module without
# requiring OIDC settings at import time.
_provider: ClientCredentialsTokenProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> ClientCredentialsTokenProvider:
    """Return the process-wide token provider, building it from settings once."""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = ClientCredentialsTokenProvider(
                    issuer=getattr(settings, "OIDC_ISSUER", ""),
                    client_id=getattr(settings, "CASEWORK_OIDC_CLIENT_ID", ""),
                    client_secret=getattr(settings, "CASEWORK_OIDC_CLIENT_SECRET", ""),
                    scope=getattr(settings, "CASEWORK_OIDC_SCOPE", ""),
                    audience=getattr(settings, "CASEWORK_OIDC_AUDIENCE", ""),
                )
    return _provider


def reset_provider() -> None:
    """Reset the module-level provider (test hook; re-reads settings)."""
    global _provider
    with _provider_lock:
        _provider = None


def get_access_token(force_refresh: bool = False) -> str:
    """Convenience: get a bearer access token from the shared provider."""
    return get_provider().get_token(force_refresh=force_refresh)


def bearer_header(force_refresh: bool = False) -> dict[str, str]:
    """Return ``{"Authorization": "Bearer <token>"}`` from the shared provider."""
    return {"Authorization": f"Bearer {get_access_token(force_refresh=force_refresh)}"}


def resolve_service_bearer(explicit_token: str | None = None) -> str | None:
    """Resolve the outbound Bearer for a service-account HTTP client.

    Used by the scraper cron commands (``scrape_ppmo_blacklist``,
    ``scrape_ciaa_press_releases``) which POST to the NGM-role-gated write API. In
    priority order:

    1. an explicit token (``--api-token``) or a static ``INGESTION_API_TOKEN`` env
       — local dev / tests / a pre-minted bearer;
    2. the dedicated **sa-ingestion** identity, when ``INGESTION_OIDC_CLIENT_ID`` +
       ``INGESTION_OIDC_CLIENT_SECRET`` are set (client-credentials grant, scope/
       audience default to the casework settings);
    3. the shared **casework** service account (``CASEWORK_OIDC_*`` settings) — the
       zero-extra-config fallback, already provisioned on the consumer image.

    Returns ``None`` when nothing is configured, so the caller decides whether that
    is fatal (a dry run needs no token; ``--write`` does). Propagates
    ``OIDCTokenError`` when a *configured* grant fails, so a real auth error
    surfaces instead of masquerading as "no token".
    """
    if explicit_token:
        return explicit_token
    static = os.environ.get("INGESTION_API_TOKEN")
    if static:
        return static

    client_id = os.environ.get("INGESTION_OIDC_CLIENT_ID")
    client_secret = os.environ.get("INGESTION_OIDC_CLIENT_SECRET")
    if client_id and client_secret:
        provider = ClientCredentialsTokenProvider(
            issuer=os.environ.get("OIDC_ISSUER", "") or getattr(settings, "OIDC_ISSUER", ""),
            client_id=client_id,
            client_secret=client_secret,
            scope=os.environ.get("INGESTION_OIDC_SCOPE")
            or getattr(settings, "CASEWORK_OIDC_SCOPE", ""),
            audience=os.environ.get("INGESTION_OIDC_AUDIENCE")
            or getattr(settings, "CASEWORK_OIDC_AUDIENCE", ""),
        )
        return provider.get_token()

    # Fall back to the shared casework service account, when it is configured.
    if getattr(settings, "CASEWORK_OIDC_CLIENT_ID", "") and getattr(
        settings, "CASEWORK_OIDC_CLIENT_SECRET", ""
    ):
        return get_access_token()
    return None
