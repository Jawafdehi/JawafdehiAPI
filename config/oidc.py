import jwt
from django.conf import settings
from django.contrib.auth.models import User
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from rest_framework import authentication, exceptions

from config.roles import sync_user_roles

# Module-level cached PyJWKClient
_jwks_client = None


def _get_jwks_client():
    """Lazily initialize and cache the PyJWKClient for Zitadel JWKS."""
    global _jwks_client
    if _jwks_client is None:
        jwks_url = getattr(
            settings,
            "ZITADEL_JWKS_URL",
            f"{getattr(settings, 'ZITADEL_ISSUER', 'https://auth.jawafdehi.org')}/oauth/v2/keys",
        )
        _jwks_client = jwt.PyJWKClient(jwks_url)
    return _jwks_client


class ZitadelJWTAuthentication(authentication.BaseAuthentication):
    """
    DRF authentication class for Zitadel OIDC JWT validation.

    Validates JWT tokens issued by Zitadel, extracts user claims,
    and syncs Zitadel roles into Django Groups.
    """

    def authenticate(self, request):
        """
        Authenticate a request using a Bearer token from Zitadel.

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
            issuer = getattr(settings, "ZITADEL_ISSUER", "https://auth.jawafdehi.org")
            audience = getattr(settings, "ZITADEL_API_AUDIENCE", "377590446026654060")

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
            raise exceptions.AuthenticationFailed(f"Invalid token: {str(e)}")

        # Extract email (case-insensitive)
        email = (claims.get("email") or "").lower()
        if not email:
            raise exceptions.AuthenticationFailed("Token missing email claim")

        # Get or create user
        user, _created = User.objects.get_or_create(
            username=email,
            defaults={
                "email": email,
                "first_name": claims.get("given_name", ""),
                "last_name": claims.get("family_name", ""),
            },
        )

        # Sync roles from Zitadel
        sync_user_roles(user, claims.get("roles", []))

        return (user, claims)

    def authenticate_header(self, request):
        """Return the WWW-Authenticate header for 401 responses."""
        return 'Bearer realm="api"'


class JawafdehiOIDCBackend(OIDCAuthenticationBackend):
    """
    OIDC backend for Django admin SSO via Zitadel.

    Subclasses mozilla-django-oidc's OIDCAuthenticationBackend so the standard
    /oidc/authenticate -> /oidc/callback flow works, and overrides user
    resolution to key on email and sync Zitadel roles into Django Groups.
    """

    def filter_users_by_claims(self, claims):
        """Match existing users by email (case-insensitive)."""
        email = (claims.get("email") or "").lower()
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(email__iexact=email)

    def create_user(self, claims):
        """Create a new user from OIDC claims, keyed by email, then sync roles."""
        email = (claims.get("email") or "").lower()
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
        user.email = (claims.get("email") or "").lower()
        user.first_name = claims.get("given_name", "")
        user.last_name = claims.get("family_name", "")
        user.save(update_fields=["email", "first_name", "last_name"])
        sync_user_roles(user, claims.get("roles", []))
        return user
