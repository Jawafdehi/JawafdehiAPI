"""NGM permission classes.

The shared :class:`jawafdehi_shared.auth.oidc.OIDCAuthentication` validates the
Zitadel JWT and SYNCS the token's project roles into Django Groups on every
request (``moderator``/``contributor`` -> ``Caseworker``, etc.). So role checks
here are just Django Group-membership checks on ``request.user.groups`` — no
token re-parsing.

``HasNgmRole`` gates the internal WRITE plane (ingestion + mutations).
Unauthenticated requests fail with 401 (the authenticator returns a
``WWW-Authenticate`` header so DRF answers 401, not 403); an authenticated user
without an NGM-capable role gets 403.

The raw-SQL SELECT plane is NOT gated from this module — see the note below.
"""

from __future__ import annotations

from rest_framework import permissions

# Django Group names that grant access to the court-data internal planes. v3:
# the single content-staff role (Caseworker) — human or service account — may
# run the gated query / ingestion surface. Superuser is short-circuited in the
# permission classes below. The old court-data rate tiers are retired.
NGM_ROLE_GROUPS = frozenset(
    {
        "Caseworker",
    }
)

# NOTE: the raw-SQL SELECT plane (``POST /query``) is deliberately NOT gated
# here. It reads the same rows the public REST read plane already serves
# anonymously (``courtcases`` + its hearings/entities sub-resources), so it
# requires only authentication — DRF's stock ``IsAuthenticated`` on
# ``courts.views.QueryView``, with no role and no ``ngm.query`` scope. Requiring
# an admin-granted role there made our own published analysis impossible for an
# outside reader to reproduce, which defeats the point of publishing it. What
# bounds that surface is ``courts.query_guard`` plus the row cap and statement
# timeout, not a permission class.
#
# This module therefore gates WRITES only. Keep it that way: ``NGM_ROLE_GROUPS``
# must never widen to a read role.


class HasNgmRole(permissions.BasePermission):
    """Require an authenticated principal in an NGM-capable Django Group.

    Pairs with the shared OIDCAuthentication (which maps Zitadel roles -> Groups).
    Unauthenticated -> 401; authenticated but no NGM role -> 403.
    """

    message = "An NGM role (caseworker or an NGM tier) is required."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        user_groups = set(user.groups.values_list("name", flat=True))
        return bool(user_groups & NGM_ROLE_GROUPS)
