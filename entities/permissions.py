"""Entity API permission classes.

The shared :class:`jawafdehi_shared.auth.oidc.OIDCAuthentication` validates the
Zitadel JWT and syncs the token's project roles into Django Groups on every
request. So role checks here are just Django Group-membership checks — no token
re-parsing — exactly like NGM's ``HasNgmRole``.

Entity writes (create / patch / delete) require any platform *content* role —
``Caseworker`` / ``Moderator`` / ``Admin`` (the same set as
``cases.rules.predicates.has_role``) — plus Django superuser. Admin-only
operations (reindex) require ``Moderator`` / ``Admin``. The read-only roles
(``ReadOnly`` / ``Public``) are intentionally excluded from writes.

History: entity writes used to be gated on the NES-specific ``NES_Contributor``
/ ``NES_Admin`` groups — a carry-over from the standalone FastAPI NES service.
Post-monolith (all services in one Django project) that separate role namespace
is dropped in favour of the platform content roles, so a Caseworker/Moderator
who can author and moderate cases can also write the entities those cases
reference — no separate ``nes_contributor`` grant required.

Unauthenticated → 401 (the authenticator sets WWW-Authenticate);
authenticated-without-a-write-role → 403.
"""

from __future__ import annotations

from rest_framework import permissions

# Platform content roles that may write entities (create / patch / delete).
# Mirrors ``cases.rules.predicates.has_role``; superuser is short-circuited in
# ``_RequireGroups`` below.
ENTITY_WRITE_GROUPS = frozenset({"Caseworker", "Moderator", "Admin"})

# Elevated roles for admin-only entity operations (reindex).
ENTITY_ADMIN_GROUPS = frozenset({"Moderator", "Admin"})


class _RequireGroups(permissions.BasePermission):
    groups: frozenset = frozenset()
    message = "A content role is required."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        user_groups = set(user.groups.values_list("name", flat=True))
        return bool(user_groups & self.groups)


class HasEntityWriteRole(_RequireGroups):
    """Require a platform content role (Caseworker / Moderator / Admin) to write entities."""

    groups = ENTITY_WRITE_GROUPS
    message = "A content role (Caseworker, Moderator, or Admin) is required to write entities."


class HasEntityAdminRole(_RequireGroups):
    """Require Moderator / Admin for admin-only entity operations (reindex)."""

    groups = ENTITY_ADMIN_GROUPS
    message = "Moderator or Admin is required."
