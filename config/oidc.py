import logging

import jwt
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from rest_framework import authentication, exceptions

from config.roles import sync_user_roles

logger = logging.getLogger(__name__)

# Module-level cached PyJWKClient
_jwks_client = None


def _get_jwks_client():
    """Lazily initialize and cache the PyJWKClient for the OIDC provider's JWKS."""
    global _jwks_client
    if _jwks_client is None:
        jwks_url = settings.OIDC_JWKS_URL
        _jwks_client = jwt.PyJWKClient(jwks_url)
    return _jwks_client


class OIDCJWTAuthentication(authentication.BaseAuthentication):
    """
    DRF authentication class for OIDC bearer-token (JWT) validation.

    Validates JWTs issued by the OIDC provider, extracts user claims, and syncs
    the caller's roles into Django Groups.
    """

    def authenticate(self, request):
        """
        Authenticate a request using a Bearer token from the OIDC provider.

        Returns:
            Tuple of (user, claims) or None if no valid token is present.

        Raises:
            AuthenticationFailed: If the token is invalid or malformed.
        """
        auth_header = request.META.get("HTTP_AUTHORIZATION", "").strip()
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Check for JWE (encrypted token): if it has 5 dot-separated segments,
        # it's a JWE and we reject it since we only validate JWT (3 segments).
        if token.count(".") != 2:
            raise exceptions.AuthenticationFailed(
                "encrypted (JWE) tokens are not accepted"
            )

        try:
            issuer = settings.OIDC_ISSUER
            audience = settings.OIDC_API_AUDIENCE

            # Get signing key from cached PyJWKClient
            signing_key = _get_jwks_client().get_signing_key_from_jwt(token)

            # Validate and decode the JWT
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=audience,
                issuer=issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as e:
            # Don't leak the raw library/connection error to the client; log it
            # internally and return a generic authentication failure.
            logger.warning("JWT validation failed: %s", e)
            raise exceptions.AuthenticationFailed("Invalid token or signature")

        # Extract email (case-insensitive)
        email = (claims.get("email") or "").lower()
        if not email:
            raise exceptions.AuthenticationFailed("Token missing email claim")

        # Resolve the user by email (case-insensitive) — same lookup key the admin
        # OIDC backend uses, so the two auth paths can't create split accounts.
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User.objects.create_user(
                username=email,
                email=email,
                first_name=claims.get("given_name", ""),
                last_name=claims.get("family_name", ""),
            )

        # Honor local deactivation, like the other auth backends do.
        if not user.is_active:
            raise exceptions.AuthenticationFailed("User account is disabled")

        # Sync roles from the OIDC token claims
        sync_user_roles(user, claims.get("roles", []))

        return (user, claims)

    def authenticate_header(self, request):
        """Return the WWW-Authenticate header for 401 responses."""
        return 'Bearer realm="api"'


class JawafdehiOIDCBackend(OIDCAuthenticationBackend):
    """
    OIDC backend for Django admin SSO.

    Subclasses mozilla-django-oidc's OIDCAuthenticationBackend so the standard
    /oidc/authenticate -> /oidc/callback flow works, and overrides user
    resolution to key on email and sync the caller's roles into Django Groups.
    """

    def get_token(self, payload):
        """Public PKCE client: drop the empty client_secret so the token request
        is a clean public-client call (Zitadel app auth method = none)."""
        if not payload.get("client_secret"):
            payload.pop("client_secret", None)
        return super().get_token(payload)

    def filter_users_by_claims(self, claims):
        """Match existing users by email (case-insensitive)."""
        email = (claims.get("email") or "").lower()
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(email__iexact=email)

    def create_user(self, claims):
        """Create a new user from OIDC claims, keyed by email, then sync roles."""
        email = (claims.get("email") or "").lower()
        if not email:
            raise PermissionDenied("Email claim is required to create a user.")
        user = self.UserModel.objects.create_user(
            username=email,
            email=email,
            first_name=claims.get("given_name", ""),
            last_name=claims.get("family_name", ""),
        )
        sync_user_roles(user, claims.get("roles", []))
        return user

    def update_user(self, user, claims):
        """Refresh profile fields and re-sync roles on each login."""
        email = (claims.get("email") or "").lower()
        if not email:
            raise PermissionDenied("Email claim is required to update a user.")
        user.email = email
        user.first_name = claims.get("given_name", "")
        user.last_name = claims.get("family_name", "")
        user.save(update_fields=["email", "first_name", "last_name"])
        sync_user_roles(user, claims.get("roles", []))
        return user
