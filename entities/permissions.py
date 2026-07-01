"""NES permission classes.

The shared :class:`jawafdehi_shared.auth.oidc.OIDCAuthentication` validates the
Zitadel JWT and syncs the token's project roles into Django Groups on every
request. So role checks here are just Django Group-membership checks — no token
re-parsing — exactly like NGM's ``HasNgmRole``.

NES write surface (create / patch / add-name / bulk-ingest) requires the
``nes_contributor`` role; admin reindex requires ``nes_admin``. These map to the
``NES_Contributor`` / ``NES_Admin`` Django Groups via the shared authenticator's
``DEFAULT_ROLE_TO_GROUP`` (entries added when this regression was fixed).

DECISION (match FastAPI semantics): the FastAPI NES service gated writes on the
*NES-specific* ``nes_contributor`` role only — the platform-wide ``Contributor``
/ ``Moderator`` roles could NOT write NES entities. We therefore DELIBERATELY do
NOT include the generic platform groups in the NES write-allow set; granting
them would be a privilege expansion over the ported behaviour. The write set is
``NES_Contributor`` + ``NES_Admin`` (admins can also write) + Django superuser.
Admin reindex is ``NES_Admin`` + superuser. Unauthenticated → 401 (the
authenticator sets WWW-Authenticate); authenticated-without-role → 403.
"""

from __future__ import annotations

from rest_framework import permissions

# Groups that grant the NES write surface (create/patch/add-name/bulk-ingest).
# NES_Admin is included because an NES admin is a superset of a contributor.
NES_CONTRIBUTOR_GROUPS = frozenset({"NES_Contributor", "NES_Admin"})

# Groups that grant the NES admin surface (reindex).
NES_ADMIN_GROUPS = frozenset({"NES_Admin"})


class _RequireGroups(permissions.BasePermission):
    groups: frozenset = frozenset()
    message = "An NES role is required."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        user_groups = set(user.groups.values_list("name", flat=True))
        return bool(user_groups & self.groups)


class HasNesContributorRole(_RequireGroups):
    """Require the ``nes_contributor`` role (or an elevated platform role)."""

    groups = NES_CONTRIBUTOR_GROUPS
    message = "The 'nes_contributor' role is required to write entities."


class HasNesAdminRole(_RequireGroups):
    """Require the ``nes_admin`` role (or platform Admin)."""

    groups = NES_ADMIN_GROUPS
    message = "The 'nes_admin' role is required."
