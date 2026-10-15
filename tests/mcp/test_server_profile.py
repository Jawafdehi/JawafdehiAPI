"""Tests for authentication-aware tool catalogs in jawafdehi-mcp."""

from contextlib import contextmanager

import pytest

from jawafdehi_mcp.identity import (
    ANONYMOUS_TOOL_NAMES,
    CASEWORKER_ROLE_KEYS,
    CASEWORKER_TOOL_NAMES,
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
    call_tool,
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
    def test_no_identity_returns_only_anonymous(self):
        all_names = set(TOOL_MAP.keys())
        result = get_allowed_tool_names(None, all_names)
        assert result == ANONYMOUS_TOOL_NAMES

    @pytest.mark.parametrize("role", ["caseworker", "moderator", "contributor"])
    def test_each_content_role_key_returns_all(self, role):
        """All three keys map to Caseworker in Django; all three must here too."""
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 1, "username": "test", "roles": [role]}
        assert get_allowed_tool_names(identity, all_names) == all_names

    def test_content_role_is_matched_case_insensitively(self):
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 1, "username": "test", "roles": ["Contributor"]}
        assert get_allowed_tool_names(identity, all_names) == all_names

    def test_authenticated_identity_with_empty_roles_loses_write_tools(self):
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 2, "username": "public", "roles": []}
        result = get_allowed_tool_names(identity, all_names)
        assert result == all_names - CASEWORKER_TOOL_NAMES
        assert "create_jawafdehi_case" not in result
        # Still strictly more than an anonymous caller gets — reads are not
        # role-gated, only writes are.
        assert ANONYMOUS_TOOL_NAMES <= result

    def test_authenticated_identity_without_roles_key_loses_write_tools(self):
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 3, "username": "noroles"}
        assert get_allowed_tool_names(identity, all_names) == (
            all_names - CASEWORKER_TOOL_NAMES
        )

    def test_readonly_role_does_not_confer_writes(self):
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 4, "username": "ro", "roles": ["readonly"]}
        assert get_allowed_tool_names(identity, all_names) == (
            all_names - CASEWORKER_TOOL_NAMES
        )

    @pytest.mark.security
    @pytest.mark.parametrize(
        "roles",
        [
            "caseworker",  # a bare string, not a list
            None,
            {"caseworker": True},  # the un-flattened Zitadel claim shape
            ["case worker"],
            [None, 7],
        ],
    )
    def test_malformed_roles_claim_denies_rather_than_grants(self, roles):
        """A roles value we do not understand must not read as the role.

        The Zitadel claim arrives as a dict and is flattened to a list upstream
        (``oidc.py``); if that ever regresses, the mistake has to cost write
        access rather than hand it out.
        """
        all_names = set(TOOL_MAP.keys())
        identity = {"user_id": 5, "username": "odd", "roles": roles}
        assert get_allowed_tool_names(identity, all_names) == (
            all_names - CASEWORKER_TOOL_NAMES
        )

    def test_result_never_aliases_the_catalog(self):
        """Callers mutate the returned set; it must not be the module's own."""
        all_names = set(TOOL_MAP.keys())
        before = set(all_names)
        for identity in ({"roles": ["caseworker"]}, {"roles": []}, None):
            returned = get_allowed_tool_names(identity, all_names)
            returned.discard("convert_date")
        assert all_names == before


class TestCaseworkerToolSet:
    def test_every_name_is_a_real_tool(self):
        assert CASEWORKER_TOOL_NAMES <= ALL_TOOL_NAMES

    def test_disjoint_from_the_anonymous_catalog(self):
        """A write tool must never have been reachable without a bearer."""
        assert CASEWORKER_TOOL_NAMES.isdisjoint(ANONYMOUS_TOOL_NAMES)

    def test_role_keys_match_the_django_group_map(self):
        """Pin the keys against the map the REST API actually enforces.

        These are role KEYS from Zitadel (lowercase), not Django Group names.
        Matching on "Caseworker" would match nobody and silently strip the write
        tools from every user, which is why this is asserted rather than trusted.
        """
        from jawafdehi_shared.auth.oidc import DEFAULT_ROLE_TO_GROUP

        keys_for_caseworker = {
            key
            for key, group in DEFAULT_ROLE_TO_GROUP.items()
            if group == "Caseworker"
        }
        assert CASEWORKER_ROLE_KEYS == keys_for_caseworker


class TestAllowedTools:
    def test_no_identity_no_token_hides_unconfigured_query_tool(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)
        current_user_identity.set(None)
        tools = _get_allowed_tools()
        tool_names = {t.name for t in tools}
        assert tool_names == ANONYMOUS_TOOL_NAMES - {"ngm_query_judicial"}

    def test_no_identity_with_query_token_returns_full_anonymous_set(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", "query-only-token")
        current_user_identity.set(None)

        tool_names = {tool.name for tool in _get_allowed_tools()}

        assert tool_names == ANONYMOUS_TOOL_NAMES

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
        assert tool_names == ANONYMOUS_TOOL_NAMES - {"ngm_query_judicial"}

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

    def test_roleless_authenticated_identity_gets_the_read_catalog(self, monkeypatch):
        """A verified identity receives every tool EXCEPT the writes."""
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        identity = {"user_id": 2, "username": "public", "roles": []}
        current_user_identity.set(identity)
        try:
            tool_names = {tool.name for tool in _get_allowed_tools()}
            assert tool_names == ALL_TOOL_NAMES - CASEWORKER_TOOL_NAMES
        finally:
            current_user_identity.set(None)

    def test_caseworker_identity_returns_all(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        identity = {"user_id": 3, "username": "cw", "roles": ["caseworker"]}
        current_user_identity.set(identity)
        try:
            assert len(_get_allowed_tools()) == len(TOOL_MAP)
        finally:
            current_user_identity.set(None)


class TestCallToolEnforcesTheCatalog:
    """The catalog must gate INVOCATION, not just what `tools/list` advertises.

    These go through ``call_tool`` rather than asserting ``_is_tool_allowed``,
    because the predicate being correct is worth nothing if the dispatcher stops
    consulting it. Deleting the check in ``call_tool`` previously left the whole
    suite green — for ``convert_to_markdown``, which no ``/api/`` permission backs,
    that check is the only thing standing between an anonymous caller and the tool.
    """

    pytestmark = pytest.mark.asyncio(loop_scope="function")

    @pytest.mark.security
    @pytest.mark.parametrize(
        "tool_name",
        ["convert_to_markdown", "create_jawafdehi_case", "manage_jobs"],
    )
    async def test_anonymous_invocation_is_refused(self, monkeypatch, tool_name):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        token = current_user_identity.set(None)
        try:
            with pytest.raises(ValueError, match=tool_name):
                await call_tool(tool_name, {})
        finally:
            current_user_identity.reset(token)

    @pytest.mark.security
    @pytest.mark.parametrize(
        "tool_name", ["create_jawafdehi_case", "upload_material_file", "manage_jobs"]
    )
    async def test_roleless_user_invocation_is_refused(self, monkeypatch, tool_name):
        """Hiding a tool from the list is not a gate; the dispatcher must refuse.

        A client holding a cached catalog from before a role change — or one
        simply naming a tool it was never offered — reaches ``call_tool``
        directly.
        """
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        token = current_user_identity.set(
            {"sub": "u1", "email": "ro@x.org", "roles": ["readonly"]}
        )
        try:
            with pytest.raises(ValueError, match="Caseworker role"):
                await call_tool(tool_name, {})
        finally:
            current_user_identity.reset(token)

    async def test_caseworker_passes_the_gate(self, monkeypatch):
        """The content role is not refused.

        Asserted on the predicate rather than through ``call_tool``: the
        dispatcher would go on to execute the tool for real. The direction that
        must be proven end to end is the refusal, above — a gate that fails open
        is the dangerous one.
        """
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        token = current_user_identity.set(
            {"sub": "u2", "email": "cw@x.org", "roles": ["caseworker"]}
        )
        try:
            assert _is_tool_allowed("create_jawafdehi_case") is True
        finally:
            current_user_identity.reset(token)

    @pytest.mark.security
    async def test_anonymous_invocation_of_unconfigured_query_tool_is_refused(
        self, monkeypatch
    ):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)
        token = current_user_identity.set(None)
        try:
            with pytest.raises(ValueError, match="ngm_query_judicial"):
                await call_tool("ngm_query_judicial", {"query": "SELECT 1"})
        finally:
            current_user_identity.reset(token)

    async def test_anonymous_invocation_of_an_allowed_tool_is_not_refused(
        self, monkeypatch
    ):
        """The guard must refuse the right things and only those."""
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        token = current_user_identity.set(None)
        try:
            result = await call_tool(
                "convert_date", {"dates": ["2023-01-15"], "mode": "ad_to_bs"}
            )
        finally:
            current_user_identity.reset(token)
        assert "Converted AD 2023-01-15 to BS" in result[0].text


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

    def test_write_tool_refused_for_roleless_authenticated_identity(self, monkeypatch):
        """Note the env token present: a USER identity does not fall back to it.

        ``_get_runtime_allowed_tool_names`` only consults the service token when
        there is no identity at all, so signing in with no role is narrower than
        signing in with none — which is the intended shape, not an oversight.
        """
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        identity = {"user_id": 2, "username": "pub", "roles": []}
        current_user_identity.set(identity)
        try:
            assert _is_tool_allowed("create_jawafdehi_case") is False
            assert _is_tool_allowed("search_jawafdehi_cases") is True
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
        "list_document_sources",
        "get_document_overview",
        "list_document_contents",
        "read_document_part",
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

    def test_all_anonymous_tools_exist_in_tool_map(self):
        for name in ANONYMOUS_TOOL_NAMES:
            assert name in TOOL_MAP, f"Anonymous tool '{name}' not found in TOOL_MAP"

    def test_anonymous_tools_are_read_only(self):
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
        assert ANONYMOUS_TOOL_NAMES.isdisjoint(write_tool_names)

    def test_private_tools_not_in_anonymous_set(self):
        private_tools = set(TOOL_MAP.keys()) - ANONYMOUS_TOOL_NAMES
        assert len(private_tools) > 0
        assert "create_jawafdehi_case" in private_tools
        assert "upload_material_file" in private_tools

    def test_all_tool_names_count(self):
        assert ALL_TOOL_NAMES == self.EXPECTED_ALL_TOOL_NAMES
        assert len(ALL_TOOL_NAMES) == 30

    def test_authentication_required_tool_set(self):
        assert ALL_TOOL_NAMES - ANONYMOUS_TOOL_NAMES == {
            # Document reading is authenticated-only: a signed-out caller
            # could otherwise walk every page of every document, and there
            # is no per-caller quota yet.
            "list_document_sources",
            "get_document_overview",
            "list_document_contents",
            "read_document_part",
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
            # Authentication required, but no role beyond it — no /api/ route
            # backs this tool, so the catalog is its only gate.
            "convert_to_markdown",
        }

    def test_anonymous_tools_count(self):
        assert {
            "search_control_plane",
            "get_nes_entity_versions",
            "browse_materials",
            "browse_court_data",
        }.issubset(ANONYMOUS_TOOL_NAMES)
        assert len(ANONYMOUS_TOOL_NAMES) == 13
        assert "convert_to_markdown" not in ANONYMOUS_TOOL_NAMES
