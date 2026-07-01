"""Django-admin SSO via mozilla-django-oidc (session login at /django-admin/).

The Django admin logs in through Zitadel using the SAME public PKCE client the
SPA uses (no client secret). Roles arrive flattened as the userinfo ``roles``
claim (a Zitadel action) and are mapped to Django Groups + is_staff/is_superuser
with the shared role map, so the admin honours the same admin/moderator tiers as
the bearer API. This is separate from the DRF bearer auth
(``jawafdehi_shared.auth.oidc.OIDCAuthentication``): that gates the API, this
gates the session-based /django-admin/.
"""
from __future__ import annotations

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from jawafdehi_shared.auth.oidc import (
    DEFAULT_ROLE_TO_GROUP,
    DEFAULT_SUPERUSER_ROLE,
    extract_role_keys,
)

# Project roles that grant Django-admin access (is_staff). Superuser is admin-only.
STAFF_ROLES = {DEFAULT_SUPERUSER_ROLE, "moderator"}


def _roles_from_claims(claims: dict) -> set[str]:
    """Role keys for the caller. Zitadel's action flattens project roles into a
    ``roles`` list in userinfo; fall back to the raw project-roles map."""
    roles = set(claims.get("roles") or [])
    if not roles:
        roles = extract_role_keys(claims)
    return roles


def _apply_roles(user, claims: dict) -> None:
    roles = _roles_from_claims(claims)
    user.is_superuser = DEFAULT_SUPERUSER_ROLE in roles
    user.is_staff = user.is_superuser or bool(roles & STAFF_ROLES)
    user.save(update_fields=["is_staff", "is_superuser"])
    group_names = {name for key, name in DEFAULT_ROLE_TO_GROUP.items() if key in roles}
    user.groups.set(Group.objects.filter(name__in=group_names))


class AdminOIDCBackend(OIDCAuthenticationBackend):
    """mozilla-django-oidc backend for the Django admin session login."""

    def get_token(self, payload):
        # Public PKCE client (Zitadel app auth method = none): drop the empty
        # client_secret so the token request is a clean public-client call.
        if not payload.get("client_secret"):
            payload.pop("client_secret", None)
        return super().get_token(payload)

    def filter_users_by_claims(self, claims):
        email = (claims.get("email") or "").lower()
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(email__iexact=email)

    def create_user(self, claims):
        email = (claims.get("email") or "").lower()
        if not email:
            raise PermissionDenied("Email claim is required to create a user.")
        user = self.UserModel.objects.create_user(
            username=email,
            email=email,
            first_name=claims.get("given_name", ""),
            last_name=claims.get("family_name", ""),
        )
        _apply_roles(user, claims)
        return user

    def update_user(self, user, claims):
        email = (claims.get("email") or "").lower()
        if email:
            user.email = email
        user.first_name = claims.get("given_name", "")
        user.last_name = claims.get("family_name", "")
        user.save(update_fields=["email", "first_name", "last_name"])
        _apply_roles(user, claims)
        return user
