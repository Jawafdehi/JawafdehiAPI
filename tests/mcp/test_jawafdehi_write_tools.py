"""Tests for Jawafdehi MCP create/patch write tools."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jsonschema import Draft202012Validator

from jawafdehi_mcp.request_context import (
    current_transport,
    jawafdehi_bearer_token,
)
from jawafdehi_mcp.server import TOOL_MAP
from jawafdehi_mcp.tools.jawafdehi_cases import (
    CASE_CREATE_FIELDS,
    CASE_TYPE_VALUES,
    CreateJawafdehiCaseTool,
    DeleteJawafdehiCaseTool,
    GetJawafdehiCaseTool,
    PatchJawafdehiCaseTool,
    UploadMaterialFileTool,
    MAX_MATERIAL_UPLOAD_BYTES,
    _get_auth_headers,
    _has_upstream_auth,
)
from cases.caseworker_serializers import CaseCreateSerializer
from cases.models import CaseState, CaseType

TEST_SLUG = "ciaa-081-cr-0123-sample-case-abc123"


@pytest.fixture(autouse=True)
def _local_stdio_transport():
    token = current_transport.set("stdio")
    try:
        yield
    finally:
        current_transport.reset(token)


def _mock_async_client(response):
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    client.patch = AsyncMock(return_value=response)
    client.delete = AsyncMock(return_value=response)

    context_manager = AsyncMock()
    context_manager.__aenter__.return_value = client
    context_manager.__aexit__.return_value = False
    return context_manager, client


class TestTransportAuthBoundary:
    @pytest.mark.security
    def test_http_does_not_fall_back_to_service_token(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "service-token")
        transport_token = current_transport.set("http")
        bearer_token = jawafdehi_bearer_token.set(None)
        try:
            assert _get_auth_headers() == {}
            assert _has_upstream_auth() is False
        finally:
            jawafdehi_bearer_token.reset(bearer_token)
            current_transport.reset(transport_token)

    def test_http_forwards_verified_caller_bearer(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "service-token")
        transport_token = current_transport.set("http")
        bearer_token = jawafdehi_bearer_token.set("caller-token")
        try:
            assert _get_auth_headers() == {"Authorization": "Bearer caller-token"}
            assert _has_upstream_auth() is True
        finally:
            jawafdehi_bearer_token.reset(bearer_token)
            current_transport.reset(transport_token)


class TestCreateJawafdehiCaseTool:
    def setup_method(self):
        self.tool = CreateJawafdehiCaseTool()

    def test_tool_metadata(self):
        assert self.tool.name == "create_jawafdehi_case"
        assert "draft Jawafdehi case" in self.tool.description
        assert self.tool.input_schema["required"] == ["title", "case_type"]

    def test_case_type_schema_matches_the_control_plane(self):
        assert CASE_TYPE_VALUES == [value for value, _label in CaseType.choices]
        assert self.tool.input_schema["properties"]["case_type"]["enum"] == (
            CASE_TYPE_VALUES
        )

    def test_schema_field_set_matches_create_serializer(self):
        serializer_fields = {
            name
            for name, field in CaseCreateSerializer().fields.items()
            if not field.read_only
        }

        assert set(CASE_CREATE_FIELDS) == serializer_fields
        assert set(self.tool.input_schema["properties"]) == serializer_fields
        assert self.tool.input_schema["properties"]["state"]["enum"] == [
            CaseState.DRAFT
        ]

    @pytest.mark.asyncio
    async def test_requires_token(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)

        result = await self.tool.execute({"title": "Case", "case_type": "CORRUPTION"})

        assert "JAWAFDEHI_API_TOKEN" in result[0].text

    @pytest.mark.asyncio
    async def test_create_success(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.is_success = True
        response.json.return_value = {"id": 7, "title": "Road contract case"}

        context_manager, client = _mock_async_client(response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {
                    "title": "Road contract case",
                    "case_type": "CORRUPTION",
                    "short_description": "Tender irregularities",
                    "tags": ["procurement"],
                    "key_allegations": ["Bid steering"],
                    "notes": "Internal lead",
                    "public_notes": "Source attribution",
                    "alleged_entities": ["https://jawafdehi.org/entity/person/example"],
                    "court_cases": ["https://jawafdehi.org/courtcase/special/082-cr-1"],
                    "bigo": 17,
                }
            )

        payload = json.loads(result[0].text)
        assert payload["id"] == 7
        client.post.assert_awaited_once()
        _, kwargs = client.post.await_args
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        assert kwargs["json"]["title"] == "Road contract case"
        assert kwargs["json"]["case_type"] == "CORRUPTION"
        assert kwargs["json"]["short_description"] == "Tender irregularities"
        assert kwargs["json"]["tags"] == ["procurement"]
        assert kwargs["json"]["key_allegations"] == ["Bid steering"]
        assert kwargs["json"]["notes"] == "Internal lead"
        assert kwargs["json"]["public_notes"] == "Source attribution"
        assert kwargs["json"]["bigo"] == 17

    @pytest.mark.asyncio
    async def test_create_422_passthrough(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.is_success = False
        response.status_code = 422
        response.json.return_value = {
            "title": ["Ensure this field has no more than 200 characters."]
        }

        context_manager, _ = _mock_async_client(response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {"title": "x" * 201, "case_type": "CORRUPTION"}
            )

        payload = json.loads(result[0].text)
        assert payload["status_code"] == 422
        assert payload["details"]["title"] == [
            "Ensure this field has no more than 200 characters."
        ]

    @pytest.mark.asyncio
    async def test_create_403_passthrough(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.is_success = False
        response.status_code = 403
        response.json.return_value = {"detail": "Permission denied."}

        context_manager, _ = _mock_async_client(response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {"title": "Case", "case_type": "CORRUPTION"}
            )

        payload = json.loads(result[0].text)
        assert payload["status_code"] == 403
        assert payload["details"]["detail"] == "Permission denied."


class TestPatchJawafdehiCaseTool:
    def setup_method(self):
        self.tool = PatchJawafdehiCaseTool()

    def test_tool_metadata(self):
        assert self.tool.name == "patch_jawafdehi_case"
        assert "RFC 6902" in self.tool.description
        assert self.tool.input_schema["required"] == ["slug", "operations"]

    def test_schema_enforces_json_patch_operation_contract(self):
        validator = Draft202012Validator(self.tool.input_schema)
        base = {"slug": TEST_SLUG}

        assert validator.is_valid(
            {
                **base,
                "operations": [
                    {"op": "move", "from": "/old", "path": "/new"},
                    {"op": "replace", "path": "/title", "value": "Updated"},
                ],
            }
        )
        assert not validator.is_valid({**base, "operations": []})
        assert not validator.is_valid(
            {**base, "operations": [{"op": "move", "path": "/new"}]}
        )
        assert not validator.is_valid(
            {**base, "operations": [{"op": "replace", "path": "/title"}]}
        )

    @pytest.mark.asyncio
    async def test_requires_token(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)

        result = await self.tool.execute(
            {
                "slug": TEST_SLUG,
                "operations": [{"op": "replace", "path": "/title", "value": "Updated"}],
            }
        )

        assert "JAWAFDEHI_API_TOKEN" in result[0].text

    @pytest.mark.asyncio
    async def test_requires_operations_list(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        result = await self.tool.execute({"slug": TEST_SLUG, "operations": {}})

        assert "operations must be a non-empty JSON Patch array" in result[0].text

    @pytest.mark.asyncio
    async def test_patch_success(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.is_success = True
        response.json.return_value = {"id": 3, "title": "Updated title"}
        response.headers = {"etag": '"v2"'}

        context_manager, client = _mock_async_client(response)

        ops = [{"op": "replace", "path": "/title", "value": "Updated title"}]
        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {
                    "slug": TEST_SLUG,
                    "operations": ops,
                    "if_match": '"v1"',
                    "transition_reason": "Evidence verified",
                }
            )

        payload = json.loads(result[0].text)
        assert payload["title"] == "Updated title"
        assert payload["etag"] == '"v2"'
        client.patch.assert_awaited_once()
        args, kwargs = client.patch.await_args
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        assert kwargs["headers"]["If-Match"] == '"v1"'
        assert kwargs["headers"]["X-Transition-Reason"] == "Evidence verified"
        assert kwargs["json"] == ops
        assert TEST_SLUG in args[0]

    @pytest.mark.asyncio
    async def test_patch_404_passthrough(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.is_success = False
        response.status_code = 404
        response.json.return_value = {"detail": "Not found."}

        context_manager, _ = _mock_async_client(response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {
                    "slug": TEST_SLUG,
                    "operations": [{"op": "replace", "path": "/title", "value": "x"}],
                }
            )

        payload = json.loads(result[0].text)
        assert payload["status_code"] == 404
        assert payload["details"]["detail"] == "Not found."

    @pytest.mark.asyncio
    async def test_patch_422_passthrough(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.is_success = False
        response.status_code = 422
        response.json.return_value = {
            "detail": "Patching path '/state' is not allowed."
        }

        context_manager, _ = _mock_async_client(response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {
                    "slug": TEST_SLUG,
                    "operations": [
                        {
                            "op": "replace",
                            "path": "/state",
                            "value": "PUBLISHED",
                        }
                    ],
                }
            )

        payload = json.loads(result[0].text)
        assert payload["status_code"] == 422
        assert "/state" in payload["details"]["detail"]


class TestGetJawafdehiCaseTool:
    @pytest.mark.asyncio
    async def test_returns_etag_and_optional_transition_history(self):
        detail = MagicMock(
            is_success=True,
            status_code=200,
            headers={"etag": '"v1"'},
        )
        detail.json.return_value = {"slug": TEST_SLUG, "state": "IN_REVIEW"}
        history = MagicMock(is_success=True, status_code=200, headers={})
        history.json.return_value = {
            "results": [{"from_state": "DRAFT", "to_state": "IN_REVIEW"}]
        }
        context_manager, client = _mock_async_client(detail)
        client.get.side_effect = [detail, history]

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await GetJawafdehiCaseTool().execute(
                {"slug": TEST_SLUG, "include_history": True}
            )

        payload = json.loads(result[0].text)
        assert payload["etag"] == '"v1"'
        assert payload["history"] == history.json.return_value
        assert (
            client.get.await_args_list[1]
            .args[0]
            .endswith(f"/api/cases/{TEST_SLUG}/history/")
        )


class TestDeleteJawafdehiCaseTool:
    @pytest.mark.asyncio
    async def test_soft_delete_uses_authoritative_case_endpoint(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock(is_success=True, status_code=204, headers={})
        context_manager, client = _mock_async_client(response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await DeleteJawafdehiCaseTool().execute({"slug": TEST_SLUG})

        assert json.loads(result[0].text) == {
            "success": True,
            "status_code": 204,
        }
        client.delete.assert_awaited_once()
        args, kwargs = client.delete.await_args
        assert args[0].endswith(f"/api/cases/{TEST_SLUG}/")
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"


class TestSubmitNESChangeTool:
    def setup_method(self):
        from jawafdehi_mcp.tools.jawafdehi_cases import SubmitNESChangeTool

        self.tool = SubmitNESChangeTool()

    def test_tool_metadata(self):
        assert self.tool.name == "submit_nes_change"
        assert self.tool.input_schema["required"] == ["action", "change_description"]

    def test_schema_requires_action_specific_write_payloads(self):
        validator = Draft202012Validator(self.tool.input_schema)

        assert validator.is_valid(
            {
                "action": "CREATE",
                "change_description": "create",
                "document": {"@type": "Person"},
            }
        )
        assert validator.is_valid(
            {
                "action": "UPDATE",
                "change_description": "update",
                "ref": "person/ram",
                "patch_ops": [{"op": "remove", "path": "/description"}],
            }
        )
        assert not validator.is_valid(
            {"action": "CREATE", "change_description": "missing document"}
        )
        assert not validator.is_valid(
            {
                "action": "UPDATE",
                "change_description": "missing patch",
                "ref": "person/ram",
            }
        )
        assert not validator.is_valid(
            {
                "action": "UPDATE",
                "change_description": "empty patch",
                "ref": "person/ram",
                "patch_ops": [],
            }
        )

    @pytest.mark.asyncio
    async def test_requires_auth(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)

        result = await self.tool.execute(
            {"action": "CREATE", "change_description": "x", "document": {}}
        )

        assert "JAWAFDEHI_API_TOKEN" in result[0].text

    @pytest.mark.asyncio
    async def test_create_posts_document(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {"@id": "https://jawafdehi.org/entity/person/ram"}

        context_manager, client = _mock_async_client(response)
        client.request = AsyncMock(return_value=response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {
                    "action": "CREATE",
                    "change_description": "seed",
                    "document": {"prefix": "person", "slug": "ram", "type": "Person"},
                }
            )

        payload = json.loads(result[0].text)
        assert payload["@id"].endswith("/entity/person/ram")
        client.request.assert_awaited_once()
        args, kwargs = client.request.await_args
        assert args[0] == "POST"
        assert args[1].endswith("/api/entities")
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        assert kwargs["json"]["change_description"] == "seed"

    @pytest.mark.asyncio
    async def test_update_patches_ref_with_ops(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"@id": "https://jawafdehi.org/entity/person/ram"}

        context_manager, client = _mock_async_client(response)
        client.request = AsyncMock(return_value=response)

        ops = [{"op": "add", "path": "/name/en", "value": "Ram"}]
        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {
                    "action": "UPDATE",
                    "ref": "person/ram",
                    "patch_ops": ops,
                    "change_description": "add name",
                }
            )

        assert "@id" in json.loads(result[0].text)
        args, kwargs = client.request.await_args
        assert args[0] == "PATCH"
        assert args[1].endswith(
            "/api/entities/https%3A%2F%2Fjawafdehi.org%2Fentity%2Fperson%2Fram"
        )
        assert kwargs["json"]["patch_ops"] == ops

    @pytest.mark.asyncio
    async def test_update_full_iri_ref_is_encoded_as_one_segment(self, monkeypatch):
        # A full @id IRI ref must be url-encoded as a single opaque path segment
        # (safe=''), NOT left with its scheme '//' and path slashes as route
        # separators — otherwise the detail route can't match it.
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"@id": "x"}

        context_manager, client = _mock_async_client(response)
        client.request = AsyncMock(return_value=response)

        iri = "https://portal.jawafdehi.org/entity/person/ram-chandra-poudel"
        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            await self.tool.execute(
                {
                    "action": "UPDATE",
                    "ref": iri,
                    "patch_ops": [{"op": "add", "path": "/name/en", "value": "R"}],
                    "change_description": "x",
                }
            )

        args, _ = client.request.await_args
        # The IRI is one encoded segment: no bare '//' from the scheme survives.
        assert args[1].endswith(
            "/api/entities/https%3A%2F%2Fjawafdehi.org%2Fentity%2Fperson%2Fram-chandra-poudel"
        )

    @pytest.mark.asyncio
    async def test_create_without_document_errors(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        result = await self.tool.execute(
            {"action": "CREATE", "change_description": "x"}
        )

        assert "document" in result[0].text

    @pytest.mark.asyncio
    async def test_update_without_ops_errors(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        result = await self.tool.execute(
            {"action": "UPDATE", "ref": "person/ram", "change_description": "x"}
        )

        assert "patch_ops" in result[0].text


class TestUploadMaterialFileTool:
    def setup_method(self):
        self.tool = UploadMaterialFileTool()

    def test_tool_metadata(self):
        assert self.tool.name == "upload_material_file"
        assert "Material" in self.tool.description
        assert self.tool.input_schema["required"] == ["source", "ident", "file_path"]

    @pytest.mark.asyncio
    async def test_requires_token(self, monkeypatch):
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)

        result = await self.tool.execute(
            {"source": "nkp", "ident": "2080-order-1", "file_path": "/tmp/o.pdf"}
        )

        assert "JAWAFDEHI_API_TOKEN" in result[0].text

    @pytest.mark.asyncio
    async def test_requires_fields(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        result = await self.tool.execute({"source": "nkp", "ident": "x"})

        assert "Missing required arguments" in result[0].text
        assert "file_path" in result[0].text

    @pytest.mark.asyncio
    async def test_invalid_file_path(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        result = await self.tool.execute(
            {"source": "nkp", "ident": "x", "file_path": "/nonexistent/broken.pdf"}
        )

        assert "Could not read file" in result[0].text

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_rejects_file_input_on_http_transport(self, monkeypatch):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        with (
            patch(
                "jawafdehi_mcp.tools.jawafdehi_cases.Path",
                side_effect=AssertionError("must not inspect server paths"),
            ),
            patch(
                "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient"
            ) as mock_client,
        ):
            token = current_transport.set("http")
            try:
                result = await self.tool.execute(
                    {
                        "source": "nkp",
                        "ident": "x",
                        "file_path": "/etc/passwd",
                    }
                )
            finally:
                current_transport.reset(token)

        assert "only supported by local stdio" in result[0].text
        assert "cannot read server filesystem paths" in result[0].text
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_success(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        pdf_file = tmp_path / "order.pdf"
        pdf_file.write_bytes(b"pdf-content")

        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {
            "@id": "https://jawafdehi.org/material/nkp/2080-order-1"
        }

        context_manager, client = _mock_async_client(response)
        uploaded = {}

        async def capture_upload(*args, **kwargs):
            file_handle = kwargs["files"]["file"][1]
            uploaded["content"] = file_handle.read()
            uploaded["handle"] = file_handle
            return response

        client.post.side_effect = capture_upload

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {
                    "source": "nkp",
                    "ident": "2080-order-1",
                    "role": "RAW",
                    "material_type": "court_order",
                    "source_url": "https://court.example/order/1",
                    "skip_convert": True,
                    "file_path": str(pdf_file),
                }
            )

        payload = json.loads(result[0].text)
        assert payload["@id"].endswith("/material/nkp/2080-order-1")
        client.post.assert_awaited_once()
        args, kwargs = client.post.await_args
        assert args[0].endswith("/api/materials/nkp/2080-order-1/file")
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        assert kwargs["data"]["role"] == "RAW"
        assert kwargs["data"]["material_type"] == "court_order"
        assert kwargs["data"]["source_url"] == "https://court.example/order/1"
        assert kwargs["data"]["skip_convert"] == "true"
        assert kwargs["files"]["file"][0] == "order.pdf"
        assert uploaded["content"] == b"pdf-content"
        assert uploaded["handle"].closed is True

    @pytest.mark.asyncio
    async def test_rejects_oversized_file_before_opening_or_request(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")
        oversized = tmp_path / "oversized.pdf"
        with oversized.open("wb") as handle:
            handle.truncate(MAX_MATERIAL_UPLOAD_BYTES + 1)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient"
        ) as mock_client:
            result = await self.tool.execute(
                {
                    "source": "nkp",
                    "ident": "oversized",
                    "file_path": str(oversized),
                }
            )

        assert result.is_error is True
        assert "100 MB" in result[0].text
        mock_client.assert_not_called()

    def test_upload_schema_matches_material_api_options(self):
        properties = self.tool.input_schema["properties"]
        assert {"MARKDOWN", "SOURCE_PAGE"}.issubset(properties["role"]["enum"])
        assert "source_url" in properties
        assert "skip_convert" in properties

    @pytest.mark.asyncio
    async def test_upload_defaults_role_to_raw_when_omitted(
        self, monkeypatch, tmp_path
    ):
        # The schema advertises role default RAW, but that's metadata only — the
        # tool must send RAW when the caller omits role.
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        pdf_file = tmp_path / "order.pdf"
        pdf_file.write_bytes(b"pdf-content")

        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {"@id": "x"}

        context_manager, client = _mock_async_client(response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            await self.tool.execute(
                {
                    "source": "nkp",
                    "ident": "2080-order-1",
                    "material_type": "court_order",
                    "file_path": str(pdf_file),
                }
            )

        _, kwargs = client.post.await_args
        assert kwargs["data"]["role"] == "RAW"

    @pytest.mark.asyncio
    async def test_upload_error_passthrough(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        pdf_file = tmp_path / "big.pdf"
        pdf_file.write_bytes(b"small-content")

        response = MagicMock()
        response.status_code = 413
        response.json.return_value = {
            "detail": "Uploaded file exceeds the 100 MB limit."
        }

        context_manager, _ = _mock_async_client(response)

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            return_value=context_manager,
        ):
            result = await self.tool.execute(
                {"source": "nkp", "ident": "oversize", "file_path": str(pdf_file)}
            )

        payload = json.loads(result[0].text)
        assert payload["status_code"] == 413
        assert "100 MB" in payload["details"]["detail"]

    @pytest.mark.asyncio
    async def test_upload_http_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JAWAFDEHI_API_TOKEN", "test-token")

        pdf_file = tmp_path / "evidence.pdf"
        pdf_file.write_bytes(b"file-content")

        with patch(
            "jawafdehi_mcp.tools.jawafdehi_cases.httpx.AsyncClient",
            side_effect=httpx.HTTPError("network down"),
        ):
            result = await self.tool.execute(
                {"source": "nkp", "ident": "x", "file_path": str(pdf_file)}
            )

        assert "Unexpected error uploading material" in result[0].text


def test_new_tools_registered_in_server_tool_map():
    assert "upload_material_file" in TOOL_MAP
    assert "submit_nes_change" in TOOL_MAP
    assert "create_jawaf_entity" not in TOOL_MAP
