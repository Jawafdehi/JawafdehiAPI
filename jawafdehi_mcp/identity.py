"""Per-request identity and tool-catalog helpers for jawafdehi-mcp.

Identity is built from the caller's verified OIDC bearer token (see ``oidc.py``
and ``http_server.py``) and stored in a request-scoped ContextVar. One endpoint,
three catalogs: an anonymous caller gets the read-only set below, a signed-in
caller gets the reads, and a caller holding the content role gets the writes too.
"""

from contextvars import ContextVar

current_user_identity: ContextVar[dict | None] = ContextVar(
    "current_user_identity", default=None
)

#: Tools an unauthenticated caller may list and call.
#:
#: Every entry here wraps a REST route that is already ``AllowAny``, so this set
#: grants nothing that ``/api/`` does not serve anonymously — which is what makes
#: a single door with an anonymous catalog consistent rather than lax.
#: ``ngm_query_judicial`` belongs for the same reason: the judicial lake is public
#: court record and its SQL plane is SELECT-gated.
#:
#: ``convert_to_markdown`` is deliberately ABSENT. It is one of the few tools with
#: no ``/api/`` route behind it, so this catalog is its ONLY gate (enforced in
#: ``server.call_tool``, not just in ``tools/list``), and each call spends real
#: fetch/OCR budget. It requires authentication — but no particular role, like
#: every other authenticated tool.
ANONYMOUS_TOOL_NAMES: set[str] = {
    "get_current_user",
    "search_jawafdehi_cases",
    "get_jawafdehi_case",
    "search_nes_entities",
    "get_nes_entities",
    "get_nes_tags",
    "get_nes_entity_prefixes",
    "get_nes_entity_versions",
    "search_control_plane",
    "browse_materials",
    "browse_court_data",
    "ngm_query_judicial",
    "convert_date",
}


#: Tools that change state, and so need the content role rather than merely a
#: verified bearer.
#:
#: Each one wraps a REST route that already refuses a caller without the
#: ``Caseworker`` group, so gating here grants nothing and removes nothing — it
#: moves the refusal from "the tool runs and Django answers 403" to "the tool is
#: not offered". That is the whole point: a signed-in ReadOnly user used to be
#: shown ``create_jawafdehi_case``, call it, and receive a permission error that
#: reads like a broken tool.
#:
#: ``ngm_extract_case_data`` belongs here despite the read-ish name: it writes
#: extracted case data back through the API.
CASEWORKER_TOOL_NAMES: set[str] = {
    "create_jawafdehi_case",
    "patch_jawafdehi_case",
    "delete_jawafdehi_case",
    "submit_nes_change",
    "delete_nes_entity",
    "upload_material_file",
    "manage_material",
    "manage_court_data",
    "manage_case_update_proposals",
    "manage_casework_reviews",
    "manage_jobs",
    "ngm_extract_case_data",
}

#: Zitadel role keys that confer the content role.
#:
#: THREE keys, all lowercase. Zitadel emits role *keys*
#: (``infra/terraform/zitadel/roles.tf``), not the capitalised Django Group
#: names, and ``jawafdehi_shared.auth.oidc.DEFAULT_ROLE_TO_GROUP`` maps
#: ``caseworker``, ``moderator`` and ``contributor`` all onto ``Caseworker``.
#: Matching on ``"Caseworker"`` here would match nobody and silently strip the
#: write tools from every user — ``tests/mcp/test_server_profile.py`` pins this
#: set against that map so the two cannot drift apart.
#:
#: Deliberately NOT read from that map at runtime: it lives behind
#: ``django.conf.settings`` and this module is import-light on purpose.
CASEWORKER_ROLE_KEYS: frozenset[str] = frozenset(
    {"caseworker", "moderator", "contributor"}
)


def has_caseworker_role(identity: dict | None) -> bool:
    """Whether this identity holds the content role under any of its keys.

    Compared case-insensitively. Zitadel emits lowercase keys today, but the
    cost of being wrong is invisible — every write tool quietly disappearing —
    so this does not depend on that staying true.
    """
    if not identity:
        return False
    roles = identity.get("roles") or []
    if not isinstance(roles, (list, tuple, set, frozenset)):
        return False
    return any(
        isinstance(role, str) and role.strip().lower() in CASEWORKER_ROLE_KEYS
        for role in roles
    )


def get_allowed_tool_names(
    identity: dict | None,
    all_tool_names: set[str],
) -> set[str]:
    """Return the set of tool names allowed for the given identity.

    - No identity → :data:`ANONYMOUS_TOOL_NAMES`.
    - Signed in, no content role → everything except
      :data:`CASEWORKER_TOOL_NAMES`.
    - Signed in with the content role → every tool.

    Catalog visibility is still not the authorization boundary: the tools
    forward the caller's bearer to Django, whose API permissions remain final.
    A Django superuser who holds no Zitadel content role is the case where the
    two disagree — the write tools vanish from their catalog while the API would
    have allowed the call. That is the safe direction, and the honest fix is to
    grant them the role rather than to widen this.
    """
    if identity is None:
        return ANONYMOUS_TOOL_NAMES & all_tool_names

    # Always a NEW set, never the caller's own. ``server`` mutates the result
    # (``allowed.discard``), and handing back ``all_tool_names`` itself would
    # let one request permanently edit the module-level catalog.
    if has_caseworker_role(identity):
        return set(all_tool_names)

    return all_tool_names - CASEWORKER_TOOL_NAMES
