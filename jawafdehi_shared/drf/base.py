"""Common DRF building blocks shared by platform services.

Keeps pagination/permission/auth defaults identical across nes, ngm, and
jawafdehi so the three APIs behave consistently. Services import these in their
settings / viewsets rather than re-declaring them.
"""

from __future__ import annotations

from rest_framework import permissions
from rest_framework.pagination import CursorPagination


class PlatformCursorPagination(CursorPagination):
    """Cursor pagination is the platform default (stable under inserts, and the
    shape the integration tests assert: ``{results: [...], next: ...}``)."""

    page_size = 50
    max_page_size = 500
    ordering = "-created_at"


class ReadOnlyOrAuthenticatedWrite(permissions.BasePermission):
    """Public reads (accountability data is public-domain), OIDC-authenticated
    writes. Pairs with the shared OIDCAuthentication; role checks layer on top
    via per-view permission classes."""

    def has_permission(self, request, view) -> bool:
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_authenticated)
