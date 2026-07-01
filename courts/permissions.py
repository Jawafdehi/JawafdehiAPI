"""NGM permission classes.

The shared :class:`jawafdehi_shared.auth.oidc.OIDCAuthentication` validates the
Zitadel JWT and SYNCS the token's project roles into Django Groups on every
request (``contributor`` -> ``Contributor``, ``ngm_gold`` -> ``NGM_GoldTier``,
etc.). So role checks here are just Django Group-membership checks on
``request.user.groups`` — no token re-parsing.

``HasNgmRole`` gates the internal write/query planes (the gated SQL endpoint and
ingestion). Unauthenticated requests fail with 401 (the authenticator returns a
``WWW-Authenticate`` header so DRF answers 401, not 403); an authenticated user
without an NGM-capable role gets 403.
"""

from __future__ import annotations

from rest_framework import permissions

# Django Group names that grant access to the NGM internal planes. These mirror
# the OIDC role->group mapping in the shared authenticator: a caseworker (human
# or service account) or any NGM rate-limit tier may run the gated query /
# ingestion surface. ("Contributor" was renamed to "Caseworker" platform-wide.)
NGM_ROLE_GROUPS = frozenset(
    {
        "Admin",
        "Moderator",
        "Caseworker",
        "NGM_SilverTier",
        "NGM_GoldTier",
        "NGM_PlatinumTier",
    }
)

# OAuth scope that grants the gated SQL plane on its own. The FastAPI route this
# was ported from gated POST /query on this scope (``require_scope("ngm.query")``).
# We honor it alongside the Django-role check so scope-issued tokens (e.g. MCP /
# service-account clients granted only the scope) are not silently dropped.
NGM_QUERY_SCOPE = "ngm.query"


def token_scopes(request) -> set[str]:
    """Return the set of OAuth scopes from the token's ``scope`` claim.

    OIDCAuthentication puts the decoded claims dict on ``request.auth``. The
    ``scope`` claim is the standard space-delimited string; some IdPs emit it as
    ``scp`` (a list). Tolerate both shapes; return an empty set when absent.
    """
    claims = getattr(request, "auth", None)
    if not isinstance(claims, dict):
        return set()
    raw = claims.get("scope") or claims.get("scp")
    if isinstance(raw, str):
        return set(raw.split())
    if isinstance(raw, (list, tuple)):
        return {str(s) for s in raw}
    return set()


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


class HasNgmQueryAccess(HasNgmRole):
    """Gate the SQL plane on EITHER the ``ngm.query`` OAuth scope OR an NGM role.

    The FastAPI POST /query route gated on the OAuth scope ``ngm.query`` via
    ``require_scope``; the initial Django port gated purely on role/group, which
    silently dropped the scope control. This restores scope-awareness without
    breaking role-based access: a token bearing the ``ngm.query`` scope is
    accepted (so scope-only clients like MCP keep working), and so is a principal
    in an NGM-capable Django Group (the role model). Either is sufficient.

    Unauthenticated -> 401; authenticated but lacking both scope and role -> 403.
    """

    message = (
        "The 'ngm.query' OAuth scope or an NGM role "
        "(caseworker or an NGM tier) is required."
    )

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False  # 401 via the authenticator's WWW-Authenticate header
        if NGM_QUERY_SCOPE in token_scopes(request):
            return True
        return super().has_permission(request, view)
