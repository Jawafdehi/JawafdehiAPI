"""Per-request identity and tool-catalog helpers for jawafdehi-mcp.

Identity is built from the caller's verified OIDC bearer token (see ``oidc.py``
and ``http_server.py``) and stored in a request-scoped ContextVar. There is one
endpoint and one catalog rule: an anonymous caller gets the read-only set below,
and any verified bearer gets everything.
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


def get_allowed_tool_names(
    identity: dict | None,
    all_tool_names: set[str],
) -> set[str]:
    """Return the set of tool names allowed for the given identity.

    - No identity → :data:`ANONYMOUS_TOOL_NAMES`.
    - Any verified identity → every tool, independent of its roles. Catalog
      visibility is not an authorization grant: the tools forward that bearer to
      Django, whose API permissions remain the final boundary for reads and
      writes.
    """
    if identity is None:
        return ANONYMOUS_TOOL_NAMES & all_tool_names

    return all_tool_names
