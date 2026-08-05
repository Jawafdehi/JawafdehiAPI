"""Tests for authentication-aware tool catalogs in jawafdehi-mcp."""

from contextlib import contextmanager

import pytest

from jawafdehi_mcp.identity import (
    PUBLIC_HOST_TOOL_NAMES,
    PUBLIC_READ_ONLY_TOOL_NAMES,
    current_user_identity,
    get_allowed_tool_names,
)
from jawafdehi_mcp.request_context import current_transport
from jawafdehi_mcp.server import (
    ALL_TOOL_NAMES,
    TOOL_MAP,
    _get_allowed_tools,
    _has_api_token,
    _is_tool_allowed,
)


@contextmanager
def _using_transport(name: str | None):
    token = current_transport.set(name)
    try:
        yield
    finally:
        current_transport.reset(token)


class TestHasApiToken:
    def test_has_token_when_set(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token-123")
        with _using_transport("stdio"):
            assert _has_api_token() is True

    def test_has_no_token_when_unset(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        with _using_transport("stdio"):
            assert _has_api_token() is False

    def test_has_no_token_when_empty(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "")
        with _using_transport("stdio"):
            assert _has_api_token() is False

    def test_has_no_token_when_whitespace(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "   ")
        with _using_transport("stdio"):
            assert _has_api_token() is False

    @pytest.mark.security
    def test_http_transport_cannot_use_service_token(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token-123")
        with _using_transport("http"):
            assert _has_api_token() is False

    @pytest.mark.security
    def test_unknown_transport_cannot_use_service_token(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token-123")
        with _using_transport(None):
            assert _has_api_token() is False


class TestGetAllowedToolNames:
    def test_no_identity_returns_only_public(self):
        all_names = set(TOOL_MAP.keys())
        result = get_allowed_tool_names(None, all_names)
        assert result == PUBLIC_READ_ONLY_TOOL_NAMES

    def test_authenticated_identity_returns_all(self):
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 1, "username": "test", "roles": ["Contributor"]}
        result = get_allowed_tool_names(identity, all_names)
        assert result == all_names

    def test_authenticated_identity_with_empty_roles_returns_all(self):
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 2, "username": "public", "roles": []}
        result = get_allowed_tool_names(identity, all_names)
        assert result == all_names

    def test_authenticated_identity_without_roles_key_returns_all(self):
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 3, "username": "noroles"}
        result = get_allowed_tool_names(identity, all_names)
        assert result == all_names


class TestAllowedTools:
    def test_no_identity_no_token_hides_unconfigured_query_tool(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)
        current_user_identity.set(None)
        tools = _get_allowed_tools()
        tool_names = {t.name for t in tools}
        assert tool_names == PUBLIC_READ_ONLY_TOOL_NAMES - {"ngm_query_judicial"}

    def test_no_identity_with_query_token_returns_full_public_set(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", "query-only-token")
        current_user_identity.set(None)

        tool_names = {tool.name for tool in _get_allowed_tools()}

        assert tool_names == PUBLIC_READ_ONLY_TOOL_NAMES

    def test_token_mode_no_user_returns_all(self, monkeypatch):
        """A stdio service token with no identity receives all tools."""
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        current_user_identity.set(None)
        with _using_transport("stdio"):
            tools = _get_allowed_tools()
        assert len(tools) == len(TOOL_MAP)

    def test_token_only_no_identity_returns_all(self, monkeypatch):
        """A stdio service token remains usable without a forwarded identity."""
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        current_user_identity.set(None)
        with _using_transport("stdio"):
            tools = _get_allowed_tools()
        assert len(tools) == len(TOOL_MAP)

    @pytest.mark.security
    def test_http_service_token_does_not_expand_tool_list(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)
        current_user_identity.set(None)
        with _using_transport("http"):
            tool_names = {tool.name for tool in _get_allowed_tools()}
        assert tool_names == PUBLIC_READ_ONLY_TOOL_NAMES - {"ngm_query_judicial"}

    def test_authenticated_identity_returns_all(self, monkeypatch):
        """An authenticated identity gets all tools."""
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        identity = {"user_id": 1, "username": "worker", "roles": ["Contributor"]}
        current_user_identity.set(identity)
        try:
            tools = _get_allowed_tools()
            assert len(tools) == len(TOOL_MAP)
        finally:
            current_user_identity.set(None)

    def test_roleless_authenticated_identity_returns_all(self, monkeypatch):
        """A verified identity does not need a role to receive all tools."""
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        identity = {"user_id": 2, "username": "public", "roles": []}
        current_user_identity.set(identity)
        try:
            tools = _get_allowed_tools()
            assert len(tools) == len(TOOL_MAP)
        finally:
            current_user_identity.set(None)


class TestIsToolAllowed:
    def test_public_tool_allowed_without_token(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        current_user_identity.set(None)
        assert _is_tool_allowed("search_jawafdehi_cases") is True

    def test_write_tool_blocked_without_token(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        current_user_identity.set(None)
        assert _is_tool_allowed("create_jawafdehi_case") is False

    def test_query_tool_blocked_without_anonymous_query_token(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)
        current_user_identity.set(None)

        assert _is_tool_allowed("ngm_query_judicial") is False

    def test_write_tool_allowed_for_authenticated_identity(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        identity = {"user_id": 1, "username": "cw", "roles": ["Contributor"]}
        current_user_identity.set(identity)
        try:
            assert _is_tool_allowed("create_jawafdehi_case") is True
        finally:
            current_user_identity.set(None)

    def test_write_tool_allowed_for_roleless_authenticated_identity(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        identity = {"user_id": 2, "username": "pub", "roles": []}
        current_user_identity.set(identity)
        try:
            assert _is_tool_allowed("create_jawafdehi_case") is True
        finally:
            current_user_identity.set(None)

    def test_write_tool_allowed_with_token_only(self, monkeypatch):
        """A stdio service token can call write tools without a user identity."""
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        current_user_identity.set(None)
        with _using_transport("stdio"):
            assert _is_tool_allowed("create_jawafdehi_case") is True

    @pytest.mark.security
    def test_write_tool_blocked_for_anonymous_http_with_service_token(
        self, monkeypatch
    ):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        current_user_identity.set(None)
        with _using_transport("http"):
            assert _is_tool_allowed("create_jawafdehi_case") is False


class TestPublicToolSetIntegrity:
    EXPECTED_ALL_TOOL_NAMES = {
        "get_current_user",
        "search_control_plane",
        "ngm_query_judicial",
        "ngm_extract_case_data",
        "search_jawafdehi_cases",
        "get_jawafdehi_case",
        "create_jawafdehi_case",
        "patch_jawafdehi_case",
        "delete_jawafdehi_case",
        "submit_nes_change",
        "upload_material_file",
        "search_nes_entities",
        "get_nes_entities",
        "get_nes_entity_prefixes",
        "get_nes_tags",
        "get_nes_entity_versions",
        "delete_nes_entity",
        "browse_materials",
        "manage_material",
        "browse_court_data",
        "manage_court_data",
        "manage_case_update_proposals",
        "manage_casework_reviews",
        "manage_jobs",
        "convert_date",
        "convert_to_markdown",
    }

    def test_all_public_tools_exist_in_tool_map(self):
        for name in PUBLIC_READ_ONLY_TOOL_NAMES:
            assert name in TOOL_MAP, f"Public tool '{name}' not found in TOOL_MAP"

    def test_public_tools_are_read_only(self):
        write_tool_names = {
            "create_jawafdehi_case",
            "delete_nes_entity",
            "manage_material",
            "manage_court_data",
            "manage_case_update_proposals",
            "manage_casework_reviews",
            "manage_jobs",
            "patch_jawafdehi_case",
            "submit_nes_change",
            "upload_material_file",
            "ngm_extract_case_data",
        }
        assert PUBLIC_READ_ONLY_TOOL_NAMES.isdisjoint(write_tool_names)

    def test_private_tools_not_in_public_set(self):
        private_tools = set(TOOL_MAP.keys()) - PUBLIC_READ_ONLY_TOOL_NAMES
        assert len(private_tools) > 0
        assert "create_jawafdehi_case" in private_tools
        assert "upload_material_file" in private_tools

    def test_all_tool_names_count(self):
        assert ALL_TOOL_NAMES == self.EXPECTED_ALL_TOOL_NAMES
        assert len(ALL_TOOL_NAMES) == 26

    def test_private_tool_set(self):
        assert ALL_TOOL_NAMES - PUBLIC_READ_ONLY_TOOL_NAMES == {
            "ngm_extract_case_data",
            "create_jawafdehi_case",
            "patch_jawafdehi_case",
            "delete_jawafdehi_case",
            "submit_nes_change",
            "upload_material_file",
            "delete_nes_entity",
            "manage_material",
            "manage_court_data",
            "manage_case_update_proposals",
            "manage_casework_reviews",
            "manage_jobs",
        }

    def test_public_tools_count(self):
        assert {
            "search_control_plane",
            "get_nes_entity_versions",
            "browse_materials",
            "browse_court_data",
        }.issubset(PUBLIC_READ_ONLY_TOOL_NAMES)
        assert len(PUBLIC_READ_ONLY_TOOL_NAMES) == 14
        assert PUBLIC_HOST_TOOL_NAMES == PUBLIC_READ_ONLY_TOOL_NAMES - {
            "convert_to_markdown"
        }
