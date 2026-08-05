"""Tests for authentication-aware MCP tool catalogs."""

from jawafdehi_mcp.identity import (
    ANONYMOUS_TOOL_NAMES,
    get_allowed_tool_names,
)


class TestGetAllowedToolNames:
    ALL_TOOLS = {
        "get_current_user",
        "search_jawafdehi_cases",
        "get_jawafdehi_case",
        "create_jawafdehi_case",
        "patch_jawafdehi_case",
        "submit_nes_change",
    }

    def test_none_identity_returns_anonymous_tools(self):
        result = get_allowed_tool_names(None, self.ALL_TOOLS)
        assert result == ANONYMOUS_TOOL_NAMES & self.ALL_TOOLS

    def test_authenticated_identity_returns_all_tools(self):
        identity = {"sub": "1", "email": "cw@x.org", "roles": ["contributor"]}
        result = get_allowed_tool_names(identity, self.ALL_TOOLS)
        assert result == self.ALL_TOOLS

    def test_authenticated_identity_with_empty_roles_returns_all_tools(self):
        identity = {"sub": "2", "email": "pub@x.org", "roles": []}
        result = get_allowed_tool_names(identity, self.ALL_TOOLS)
        assert result == self.ALL_TOOLS

    def test_authenticated_identity_with_unknown_role_returns_all_tools(self):
        identity = {"sub": "2", "email": "pub@x.org", "roles": ["unknown"]}
        result = get_allowed_tool_names(identity, self.ALL_TOOLS)
        assert result == self.ALL_TOOLS

    def test_identity_with_no_roles_key(self):
        identity = {"sub": "3", "email": "x@x.org"}
        result = get_allowed_tool_names(identity, self.ALL_TOOLS)
        assert result == self.ALL_TOOLS

    def test_respects_all_tool_names_boundary(self):
        limited_tools = {"search_jawafdehi_cases", "get_jawafdehi_case"}
        identity = {"sub": "4", "email": "cw@x.org", "roles": ["admin"]}
        result = get_allowed_tool_names(identity, limited_tools)
        assert result == limited_tools

    def test_get_current_user_is_anonymous(self):
        assert "get_current_user" in ANONYMOUS_TOOL_NAMES

    def test_anonymous_set_does_not_include_write_tools(self):
        write_tools = {
            "create_jawafdehi_case",
            "patch_jawafdehi_case",
            "submit_nes_change",
            "upload_material_file",
            "ngm_extract_case_data",
        }
        assert ANONYMOUS_TOOL_NAMES.isdisjoint(write_tools)


class TestAnonymousToolSet:
    """One catalog, no request modes: the header-selected doors are gone."""

    def test_document_converter_requires_authentication(self):
        # No /api/ route backs this tool, so the catalog is its only gate.
        assert "convert_to_markdown" not in ANONYMOUS_TOOL_NAMES

    def test_anonymous_keeps_public_reads(self):
        assert "search_jawafdehi_cases" in ANONYMOUS_TOOL_NAMES
        assert "get_jawafdehi_case" in ANONYMOUS_TOOL_NAMES
        # Public court record over a SELECT-gated plane.
        assert "ngm_query_judicial" in ANONYMOUS_TOOL_NAMES

    def test_document_converter_needs_no_role_beyond_authentication(self):
        all_tools = ANONYMOUS_TOOL_NAMES | {"convert_to_markdown"}
        identity = {"sub": "1", "roles": []}
        assert "convert_to_markdown" in get_allowed_tool_names(identity, all_tools)
