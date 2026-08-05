"""Per-request identity and tool-catalog helpers for jawafdehi-mcp.

Identity is built from the caller's verified OIDC bearer token (see
``oidc.py`` and ``http_server.py``) and stored in a request-scoped ContextVar.
Authenticated callers receive the full catalog; request mode controls only the
anonymous catalog.
"""

from contextvars import ContextVar

current_user_identity: ContextVar[dict | None] = ContextVar(
    "current_user_identity", default=None
)

# Which "door" the request came through, tagged by the ingress (X-MCP-Mode):
#   "public"   -> anonymous, restricted tool set (mcp.jawafdehi.org)
#   "internal" -> OAuth-gated (mcp-internal.jawafdehi.org); anonymous requests
#                 are challenged with 401 upstream in http_server, so an
#                 anonymous request should not normally reach tool gating here.
#   None       -> legacy/unset (OWUI-facing in-cluster deploy, stdio) — keep the
#                 historical anonymous behavior (full read-only set).
current_request_mode: ContextVar[str | None] = ContextVar(
    "current_request_mode", default=None
)

PUBLIC_READ_ONLY_TOOL_NAMES: set[str] = {
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
    "convert_to_markdown",
}

# Anonymous tool set for the public internet door. Drops convert_to_markdown,
# which burns real resources (OCR / LLM credits) for unauthenticated callers.
# ngm_query_judicial is intentionally exposed here: the judicial lake is public
# court data and the SQL plane is already SELECT-gated.
PUBLIC_HOST_TOOL_NAMES: set[str] = PUBLIC_READ_ONLY_TOOL_NAMES - {
    "convert_to_markdown",
}


def anonymous_tool_names(mode: str | None) -> set[str]:
    """Tool set for an unauthenticated caller, by request mode.

    Only the public internet door restricts the set; the legacy/unset and
    internal doors keep the full read-only set (the internal door 401s
    anonymous callers before this anyway).
    """
    if mode == "public":
        return PUBLIC_HOST_TOOL_NAMES
    return PUBLIC_READ_ONLY_TOOL_NAMES


def get_allowed_tool_names(
    identity: dict | None,
    all_tool_names: set[str],
    mode: str | None = None,
) -> set[str]:
    """Return the set of tool names allowed for the given identity + mode.

    - No identity → anonymous tools for the request mode (restricted on the
      public internet door, full read-only otherwise).
    - Any verified identity → all tools. The downstream Django API remains the
      authorization boundary for operations performed by those tools.
    """
    if identity is None:
        return anonymous_tool_names(mode) & all_tool_names

    return all_tool_names
