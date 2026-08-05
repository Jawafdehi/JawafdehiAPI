"""NGM permission classes.

The shared :class:`jawafdehi_shared.auth.oidc.OIDCAuthentication` validates the
Zitadel JWT and SYNCS the token's project roles into Django Groups on every
request (``moderator``/``contributor`` -> ``Caseworker``, etc.). So role checks
here are just Django Group-membership checks on ``request.user.groups`` — no
token re-parsing.

``HasNgmRole`` gates the internal write/query planes (the gated SQL endpoint and
ingestion). Unauthenticated requests fail with 401 (the authenticator returns a
``WWW-Authenticate`` header so DRF answers 401, not 403); an authenticated user
without an NGM-capable role gets 403.
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

# OAuth scope that grants the gated SQL plane on its own. The FastAPI route this
# was ported from gated POST /query on this scope (``require_scope("ngm.query")``).
# We honor it alongside the Django-role check so scope-issued tokens (e.g. MCP /
# service-account clients granted only the scope) are not silently dropped.
NGM_QUERY_SCOPE = "ngm.query"

# Django Groups that may run the gated SELECT plane (``HasNgmQueryAccess``).
# WIDER than NGM_ROLE_GROUPS (the write gate): the org-wide ReadOnly read role is
# admitted here because querying is a read — the guard is SELECT-only and the
# rows are already public via the REST read plane. ReadOnly stays OUT of
# NGM_ROLE_GROUPS, so it remains excluded from ingestion / mutations.
NGM_QUERY_GROUPS = frozenset({"ReadOnly"}) | NGM_ROLE_GROUPS


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


def has_ngm_query_role(user) -> bool:
    """Return whether a user may inspect the internal, unprojected query plane."""
    if not (user and user.is_authenticated):
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=NGM_QUERY_GROUPS).exists()


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


class HasNgmQueryAccess(permissions.BasePermission):
    """Gate the SELECT plane on the ``ngm.query`` OAuth scope OR a read role.

    The FastAPI POST /query route gated on the OAuth scope ``ngm.query`` via
    ``require_scope``; the initial Django port gated purely on role/group, which
    silently dropped the scope control. This restores scope-awareness without
    breaking role-based access: a token bearing the ``ngm.query`` scope is
    accepted (so scope-only clients like MCP keep working), and so is a principal
    in a query-capable Django Group. Either is sufficient.

    Query-capable is WIDER than ``HasNgmRole`` (the write gate): it is superuser,
    the NGM content role (Caseworker), OR the org-wide ReadOnly read role.
    Querying is a read — the guard is SELECT-only and the rows are already public
    via the REST read plane — so ReadOnly is admitted here even though it is
    excluded from every write gate. (This deliberately shares no logic with
    ``HasNgmRole``, so it subclasses ``BasePermission`` directly rather than
    that gate.)

    Unauthenticated -> 401; authenticated but lacking scope and a read role -> 403.
    """

    message = (
        "The 'ngm.query' OAuth scope or a read-capable role "
        "(ReadOnly or Caseworker) is required."
    )

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False  # 401 via the authenticator's WWW-Authenticate header
        if NGM_QUERY_SCOPE in token_scopes(request):
            return True
        # Note this is NGM_QUERY_GROUPS (ReadOnly + NGM roles), NOT
        # HasNgmRole's NGM_ROLE_GROUPS, which excludes ReadOnly and gates writes.
        return has_ngm_query_role(user)
