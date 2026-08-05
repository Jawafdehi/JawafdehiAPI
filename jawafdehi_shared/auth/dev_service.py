"""Test-only bearer authentication for the Zitadel-free E2E stack."""

from __future__ import annotations

import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import authentication, exceptions


class DevelopmentQueryTokenAuthentication(authentication.BaseAuthentication):
    """Map one configured E2E bearer to a scope-only query principal.

    This class is installed only when both TESTING and DEV_AUTH are true. It is
    not part of the production authentication list, and it deliberately ignores
    every endpoint except the gated judicial query plane.
    """

    keyword = b"bearer"
    query_path = "/api/query/"

    def authenticate(self, request):
        if not (getattr(settings, "TESTING", False) and settings.DEV_AUTH):
            return None
        if request.path_info != self.query_path:
            return None

        configured = getattr(settings, "DEV_NGM_QUERY_TOKEN", "")
        if not configured:
            return None

        header = authentication.get_authorization_header(request).split()
        if len(header) != 2 or header[0].lower() != self.keyword:
            return None
        try:
            candidate = header[1].decode()
        except UnicodeDecodeError:
            return None
        if not secrets.compare_digest(candidate, configured):
            return None

        username = getattr(settings, "DEV_NGM_QUERY_USERNAME", "mcp-query-e2e")
        try:
            user = get_user_model().objects.get(username=username, is_active=True)
        except get_user_model().DoesNotExist as exc:
            raise exceptions.AuthenticationFailed(
                "Configured development query principal does not exist."
            ) from exc
        if (
            user.is_staff
            or user.is_superuser
            or user.groups.exists()
            or user.user_permissions.exists()
        ):
            raise exceptions.AuthenticationFailed(
                "Development query principal must not have roles or permissions."
            )
        return user, {"sub": user.username, "scope": "ngm.query"}

    def authenticate_header(self, request):
        return "Bearer"
