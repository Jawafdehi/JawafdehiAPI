"""Tests for authentication-aware MCP tool catalogs."""

from jawafdehi_mcp.identity import (
    PUBLIC_HOST_TOOL_NAMES,
    PUBLIC_READ_ONLY_TOOL_NAMES,
    anonymous_tool_names,
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

    def test_none_identity_returns_public_tools(self):
        result = get_allowed_tool_names(None, self.ALL_TOOLS)
        assert result == PUBLIC_READ_ONLY_TOOL_NAMES & self.ALL_TOOLS

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

    def test_get_current_user_is_public(self):
        assert "get_current_user" in PUBLIC_READ_ONLY_TOOL_NAMES

    def test_public_tools_set_does_not_include_write_tools(self):
        write_tools = {
            "create_jawafdehi_case",
            "patch_jawafdehi_case",
            "submit_nes_change",
            "upload_material_file",
            "ngm_extract_case_data",
        }
        assert PUBLIC_READ_ONLY_TOOL_NAMES.isdisjoint(write_tools)


class TestPublicHostToolSet:
    def test_public_host_drops_ocr(self):
        assert "convert_to_markdown" not in PUBLIC_HOST_TOOL_NAMES

    def test_public_host_keeps_reads(self):
        assert "search_jawafdehi_cases" in PUBLIC_HOST_TOOL_NAMES
        assert "get_jawafdehi_case" in PUBLIC_HOST_TOOL_NAMES
        assert "ngm_query_judicial" in PUBLIC_HOST_TOOL_NAMES

    def test_public_host_is_subset_of_read_only(self):
        assert PUBLIC_HOST_TOOL_NAMES < PUBLIC_READ_ONLY_TOOL_NAMES

    def test_anonymous_tool_names_public_mode(self):
        assert anonymous_tool_names("public") == PUBLIC_HOST_TOOL_NAMES

    def test_anonymous_tool_names_internal_mode_full_readonly(self):
        assert anonymous_tool_names("internal") == PUBLIC_READ_ONLY_TOOL_NAMES

    def test_anonymous_tool_names_legacy_mode_full_readonly(self):
        assert anonymous_tool_names(None) == PUBLIC_READ_ONLY_TOOL_NAMES


class TestModeAwareGating:
    ALL_TOOLS = {
        "search_jawafdehi_cases",
        "convert_to_markdown",
        "ngm_query_judicial",
        "create_jawafdehi_case",
    }

    def test_anonymous_public_mode_excludes_ocr_keeps_sql(self):
        result = get_allowed_tool_names(None, self.ALL_TOOLS, "public")
        assert "convert_to_markdown" not in result
        assert "ngm_query_judicial" in result
        assert "search_jawafdehi_cases" in result

    def test_anonymous_internal_mode_keeps_ocr_sql(self):
        result = get_allowed_tool_names(None, self.ALL_TOOLS, "internal")
        assert "convert_to_markdown" in result
        assert "ngm_query_judicial" in result

    def test_authenticated_identity_unaffected_by_public_mode(self):
        identity = {"sub": "1", "roles": []}
        result = get_allowed_tool_names(identity, self.ALL_TOOLS, "public")
        assert result == self.ALL_TOOLS
