"""Tests for the unified DocumentConverterTool."""

import asyncio
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jsonschema import Draft202012Validator
from markitdown import StreamInfo

from jawafdehi_mcp.request_context import current_transport
from jawafdehi_mcp.tools.document_converter import (
    DocumentConverterTool,
    _PinnedNetworkBackend,
    _ResolvedRemoteTarget,
    _run_bounded_conversion,
    _run_isolated_stream_conversion,
)


def _remote_document():
    return (
        b"%PDF-1.4 bounded",
        StreamInfo(
            mimetype="application/pdf",
            extension=".pdf",
            url="https://example.com/document.pdf",
        ),
    )


class TestDocumentConverterTool:
    """Test DocumentConverterTool properties and execution."""

    def setup_method(self):
        self.tool = DocumentConverterTool()

    @pytest.fixture(autouse=True)
    def _local_stdio_transport(self):
        token = current_transport.set("stdio")
        try:
            yield
        finally:
            current_transport.reset(token)

    def test_tool_name(self):
        assert self.tool.name == "convert_to_markdown"

    def test_tool_has_description(self):
        assert "MarkItDown" in self.tool.description
        assert "plugins disabled" in self.tool.description
        assert "opt-in" in self.tool.description

    def test_input_schema_has_all_fields(self):
        schema = self.tool.input_schema
        expected_fields = [
            "file_path",
            "uri",
            "output_path",
            "pages",
            "enable_plugins",
        ]
        for field in expected_fields:
            assert field in schema["properties"]

        assert "doc_type" not in schema["properties"]
        assert "title" not in schema["properties"]

    def test_input_schema_requires_exactly_one_source(self):
        schema = self.tool.input_schema
        validator = Draft202012Validator(schema)

        assert schema["required"] == []
        assert validator.is_valid({"file_path": "/tmp/input.pdf"})
        assert validator.is_valid({"uri": "https://example.com/input.pdf"})
        assert not validator.is_valid({})
        assert not validator.is_valid(
            {
                "file_path": "/tmp/input.pdf",
                "uri": "https://example.com/input.pdf",
            }
        )

    @pytest.mark.asyncio
    async def test_missing_both_file_path_and_uri(self):
        result = await self.tool.execute({})
        assert len(result) == 1
        assert "Error" in result[0].text
        assert "file_path" in result[0].text or "uri" in result[0].text

    @pytest.mark.asyncio
    async def test_both_file_path_and_uri_provided(self):
        result = await self.tool.execute(
            {"file_path": "/tmp/test.pdf", "uri": "https://example.com/doc.pdf"}
        )
        assert len(result) == 1
        assert "Error" in result[0].text
        assert "both" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_nonexistent_file(self, tmp_path):
        result = await self.tool.execute(
            {"file_path": str(tmp_path / "nonexistent_unified_test.pdf")}
        )
        assert len(result) == 1
        assert "File not found" in result[0].text

    @pytest.mark.asyncio
    async def test_path_is_directory(self, tmp_path):
        result = await self.tool.execute({"file_path": str(tmp_path)})
        assert len(result) == 1
        assert "not a file" in result[0].text

    @pytest.mark.asyncio
    async def test_local_pdf_uses_markitdown_without_plugins_by_default(self, tmp_path):
        """Local PDFs should route through MarkItDown and return markdown by default."""
        pdf_file = tmp_path / "ciaa.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake content")

        fake_markdown = "# Converted with plugin\n"
        mock_result = MagicMock()
        mock_result.markdown = fake_markdown

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter
            result = await self.tool.execute({"file_path": str(pdf_file)})

        assert len(result) == 1
        assert "Converted with MarkItDown\n\n" in result[0].text
        assert fake_markdown in result[0].text
        mock_markitdown.assert_called_once_with(enable_plugins=False)
        mock_converter.convert_uri.assert_called_once_with(pdf_file.resolve().as_uri())

    @pytest.mark.asyncio
    async def test_markitdown_direct_with_file_path(self, tmp_path):
        """Non-PDF local files should still use MarkItDown and return markdown."""
        docx_file = tmp_path / "document.docx"
        docx_file.write_bytes(b"fake docx content")

        fake_markdown = "# Document Title\n\nContent\n"
        mock_result = MagicMock()
        mock_result.markdown = fake_markdown

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_markitdown.return_value = mock_converter
            mock_converter.convert_uri.return_value = mock_result

            result = await self.tool.execute({"file_path": str(docx_file)})

        assert len(result) == 1
        assert "Converted with MarkItDown\n\n" in result[0].text
        assert fake_markdown in result[0].text
        mock_converter.convert_uri.assert_called_once()
        call_args = mock_converter.convert_uri.call_args[0][0]
        assert call_args == docx_file.resolve().as_uri()

    @pytest.mark.asyncio
    async def test_legacy_doc_uses_unified_plugin_enabled_path(self, tmp_path):
        """Legacy DOC files should be accepted by the unified MarkItDown path."""
        doc_file = tmp_path / "legacy.doc"
        doc_file.write_bytes(b"fake doc content")

        fake_markdown = "# Legacy Doc\n"
        mock_result = MagicMock()
        mock_result.markdown = fake_markdown

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter

            result = await self.tool.execute(
                {"file_path": str(doc_file), "enable_plugins": True}
            )

        assert len(result) == 1
        assert fake_markdown in result[0].text
        mock_markitdown.assert_called_once_with(enable_plugins=True)
        mock_converter.convert_uri.assert_called_once_with(doc_file.resolve().as_uri())

    @pytest.mark.asyncio
    async def test_markitdown_with_uri(self):
        """Remote bytes are passed to the isolated conversion worker."""
        fake_markdown = "# Web Document\n"

        with (
            patch(
                "jawafdehi_mcp.tools.document_converter."
                "_run_isolated_stream_conversion",
                new=AsyncMock(return_value=fake_markdown),
            ) as mock_convert,
            patch(
                "jawafdehi_mcp.tools.document_converter._fetch_remote_document",
                new=AsyncMock(return_value=_remote_document()),
            ) as mock_fetch,
        ):
            result = await self.tool.execute(
                {"uri": "https://example.com/document.pdf"}
            )

        assert len(result) == 1
        assert "Converted with MarkItDown\n\n" in result[0].text
        assert fake_markdown in result[0].text
        mock_fetch.assert_awaited_once_with("https://example.com/document.pdf")
        mock_convert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_output_path_writing(self, tmp_path):
        """Test that output_path writes markdown to disk without including content in response."""
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake")
        output_file = tmp_path / "output" / "result.md"

        fake_markdown = "# Test\n"

        mock_result = MagicMock()
        mock_result.markdown = fake_markdown

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter
            token = current_transport.set("stdio")
            try:
                result = await self.tool.execute(
                    {"file_path": str(pdf_file), "output_path": str(output_file)}
                )
            finally:
                current_transport.reset(token)

        assert len(result) == 1
        assert "written to" in result[0].text.lower()
        assert fake_markdown not in result[0].text
        assert output_file.exists()
        assert output_file.read_text(encoding="utf-8") == fake_markdown

    @pytest.mark.asyncio
    async def test_enable_plugins_defaults_to_false_for_remote_input(self):
        fake_markdown = "# Test\n"

        with (
            patch(
                "jawafdehi_mcp.tools.document_converter."
                "_run_isolated_stream_conversion",
                new=AsyncMock(return_value=fake_markdown),
            ) as mock_convert,
            patch(
                "jawafdehi_mcp.tools.document_converter._fetch_remote_document",
                new=AsyncMock(return_value=_remote_document()),
            ),
        ):
            await self.tool.execute({"uri": "https://example.com/doc.pdf"})

        mock_convert.assert_awaited_once()

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_enable_plugins_true_is_rejected_for_remote_input(self):
        with (
            patch(
                "jawafdehi_mcp.tools.document_converter."
                "_run_isolated_stream_conversion",
                new=AsyncMock(),
            ) as mock_convert,
            patch(
                "jawafdehi_mcp.tools.document_converter._fetch_remote_document",
                new=AsyncMock(),
            ) as mock_fetch,
        ):
            result = await self.tool.execute(
                {"uri": "https://example.com/doc.pdf", "enable_plugins": True}
            )

        assert "supported only for trusted local stdio" in result[0].text
        mock_fetch.assert_not_awaited()
        mock_convert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_uri_conversion(self, tmp_path):
        """Test that file:// URIs are converted and returned directly."""
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake")

        fake_markdown = "# Test\n"

        mock_result = MagicMock()
        mock_result.markdown = fake_markdown

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter
            result = await self.tool.execute({"uri": pdf_file.resolve().as_uri()})

        assert len(result) == 1
        assert "Converted with MarkItDown\n\n" in result[0].text
        assert "Error" not in result[0].text
        assert fake_markdown in result[0].text
        mock_converter.convert_uri.assert_called_once_with(pdf_file.resolve().as_uri())

    @pytest.mark.asyncio
    async def test_rejects_output_path_on_http_transport(self, tmp_path):
        """HTTP-hosted MCP servers must not write markdown files."""
        output_file = tmp_path / "result.md"

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            token = current_transport.set("http")
            try:
                result = await self.tool.execute(
                    {
                        "uri": "https://example.com/document.pdf",
                        "output_path": str(output_file),
                    }
                )
            finally:
                current_transport.reset(token)

        assert len(result) == 1
        assert "only supported by local stdio" in result[0].text
        assert "must not write files" in result[0].text
        assert not output_file.exists()
        mock_markitdown.assert_not_called()

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_rejects_file_path_on_http_transport(self):
        with (
            patch(
                "jawafdehi_mcp.tools.document_converter.Path.exists",
                side_effect=AssertionError("must not inspect server paths"),
            ),
            patch(
                "jawafdehi_mcp.tools.document_converter.MarkItDown"
            ) as mock_markitdown,
        ):
            token = current_transport.set("http")
            try:
                result = await self.tool.execute({"file_path": "/etc/passwd"})
            finally:
                current_transport.reset(token)

        assert "only supported by local stdio" in result[0].text
        assert "cannot read server filesystem paths" in result[0].text
        mock_markitdown.assert_not_called()

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_rejects_file_uri_on_http_transport(self):
        with (
            patch(
                "jawafdehi_mcp.tools.document_converter.Path.exists",
                side_effect=AssertionError("must not inspect server paths"),
            ),
            patch(
                "jawafdehi_mcp.tools.document_converter.MarkItDown"
            ) as mock_markitdown,
        ):
            token = current_transport.set("http")
            try:
                result = await self.tool.execute({"uri": "file:///etc/passwd"})
            finally:
                current_transport.reset(token)

        assert "only supported by local stdio" in result[0].text
        mock_markitdown.assert_not_called()

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_rejects_bare_path_supplied_as_uri_on_http_transport(self):
        with (
            patch(
                "jawafdehi_mcp.tools.document_converter.Path.exists",
                side_effect=AssertionError("must not inspect server paths"),
            ),
            patch(
                "jawafdehi_mcp.tools.document_converter.MarkItDown"
            ) as mock_markitdown,
        ):
            token = current_transport.set("http")
            try:
                result = await self.tool.execute({"uri": "/etc/passwd"})
            finally:
                current_transport.reset(token)

        assert "Bare paths are not valid URIs" in result[0].text
        mock_markitdown.assert_not_called()

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_rejects_loopback_remote_uri(self):
        token = current_transport.set("http")
        try:
            result = await self.tool.execute({"uri": "http://127.0.0.1/secret"})
        finally:
            current_transport.reset(token)

        assert "public IP address" in result[0].text

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_rejects_hostname_resolving_to_private_address(self):
        private_answer = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.8", 443),
            )
        ]
        with patch(
            "jawafdehi_mcp.tools.document_converter.socket.getaddrinfo",
            return_value=private_answer,
        ):
            result = await self.tool.execute(
                {"uri": "https://metadata.example/document"}
            )

        assert "public IP addresses" in result[0].text

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_pinned_backend_connects_to_validated_ip_without_new_dns_lookup(self):
        stream = object()
        backend = AsyncMock()
        backend.connect_tcp.return_value = stream
        pinned = _PinnedNetworkBackend(
            backend,
            _ResolvedRemoteTarget(
                hostname="documents.example",
                port=443,
                addresses=("93.184.216.34",),
            ),
        )

        result = await pinned.connect_tcp(
            "documents.example",
            443,
            timeout=5,
            local_address=None,
            socket_options=[],
        )

        assert result is stream
        backend.connect_tcp.assert_awaited_once_with(
            "93.184.216.34",
            443,
            timeout=5,
            local_address=None,
            socket_options=[],
        )

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_pinned_backend_rejects_a_changed_connection_host(self):
        backend = AsyncMock()
        pinned = _PinnedNetworkBackend(
            backend,
            _ResolvedRemoteTarget(
                hostname="documents.example",
                port=443,
                addresses=("93.184.216.34",),
            ),
        )

        with pytest.raises(Exception, match="connection target changed"):
            await pinned.connect_tcp("metadata.internal", 443)

        backend.connect_tcp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remote_fetch_holds_document_capacity_slot(self):
        limiter = asyncio.Semaphore(1)

        async def fetch_while_slot_is_held(_uri):
            assert limiter.locked()
            return _remote_document()

        with (
            patch(
                "jawafdehi_mcp.tools.document_converter._conversion_limiter",
                return_value=limiter,
            ),
            patch(
                "jawafdehi_mcp.tools.document_converter._fetch_remote_document",
                side_effect=fetch_while_slot_is_held,
            ),
            patch(
                "jawafdehi_mcp.tools.document_converter."
                "_run_isolated_stream_conversion",
                new=AsyncMock(return_value="# Remote\n"),
            ),
        ):
            result = await self.tool.execute(
                {"uri": "https://example.com/document.pdf"}
            )

        assert "# Remote" in result[0].text
        assert not limiter.locked()

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_rejects_oversized_data_uri(self, monkeypatch):
        monkeypatch.setenv("MCP_DOCUMENT_MAX_INPUT_BYTES", "4")
        result = await self.tool.execute({"uri": "data:text/plain,12345"})
        assert "exceeds the 4-byte limit" in result[0].text

    @pytest.mark.asyncio
    async def test_http_transport_allows_remote_uri(self):
        with (
            patch(
                "jawafdehi_mcp.tools.document_converter."
                "_run_isolated_stream_conversion",
                new=AsyncMock(return_value="# Remote\n"),
            ) as mock_convert,
            patch(
                "jawafdehi_mcp.tools.document_converter._fetch_remote_document",
                new=AsyncMock(return_value=_remote_document()),
            ) as mock_fetch,
        ):
            token = current_transport.set("http")
            try:
                result = await self.tool.execute(
                    {"uri": "https://example.com/document.pdf"}
                )
            finally:
                current_transport.reset(token)

        assert "# Remote" in result[0].text
        mock_fetch.assert_awaited_once_with("https://example.com/document.pdf")
        mock_convert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_output_path_matching_source_file(self, tmp_path):
        """Explicit output_path must not overwrite the source file."""
        markdown_file = tmp_path / "already.md"
        markdown_file.write_text("# Existing\n", encoding="utf-8")

        fake_markdown = "# Converted\n"
        mock_result = MagicMock()
        mock_result.markdown = fake_markdown

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter

            token = current_transport.set("stdio")
            try:
                result = await self.tool.execute(
                    {
                        "file_path": str(markdown_file),
                        "output_path": str(markdown_file),
                    }
                )
            finally:
                current_transport.reset(token)

        assert len(result) == 1
        assert "output_path must differ from the source file" in result[0].text
        assert markdown_file.read_text(encoding="utf-8") == "# Existing\n"

    @pytest.mark.asyncio
    async def test_localhost_file_uri_conversion(self, tmp_path):
        """file://localhost URIs should resolve to local filesystem paths."""
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake")
        localhost_uri = f"file://localhost{pdf_file.resolve().as_uri()[7:]}"

        mock_result = MagicMock()
        mock_result.markdown = "# Test\n"

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter
            result = await self.tool.execute({"uri": localhost_uri})

        assert len(result) == 1
        assert "Error" not in result[0].text
        mock_converter.convert_uri.assert_called_once_with(pdf_file.resolve().as_uri())

    @pytest.mark.asyncio
    async def test_uppercase_file_uri_conversion(self, tmp_path):
        """FILE:// URIs should be treated as file URIs."""
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake")
        uppercase_uri = pdf_file.resolve().as_uri().replace("file://", "FILE://", 1)

        mock_result = MagicMock()
        mock_result.markdown = "# Test\n"

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter
            result = await self.tool.execute({"uri": uppercase_uri})

        assert len(result) == 1
        assert "Error" not in result[0].text
        mock_converter.convert_uri.assert_called_once_with(pdf_file.resolve().as_uri())

    @pytest.mark.asyncio
    async def test_uppercase_https_uri_uses_markitdown(self):
        """HTTPS:// URIs should still be treated as web URIs."""
        fake_markdown = "# Web Document\n"

        with (
            patch(
                "jawafdehi_mcp.tools.document_converter."
                "_run_isolated_stream_conversion",
                new=AsyncMock(return_value=fake_markdown),
            ) as mock_convert,
            patch(
                "jawafdehi_mcp.tools.document_converter._fetch_remote_document",
                new=AsyncMock(return_value=_remote_document()),
            ) as mock_fetch,
        ):
            result = await self.tool.execute(
                {"uri": "HTTPS://example.com/document.pdf"}
            )

        assert len(result) == 1
        assert "MarkItDown" in result[0].text
        mock_fetch.assert_awaited_once_with("HTTPS://example.com/document.pdf")
        mock_convert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_remote_file_uri_netloc(self):
        """Remote file URIs should be rejected before path validation."""
        result = await self.tool.execute({"uri": "file://example.com/test.pdf"})

        assert len(result) == 1
        assert "Unsupported file URI" in result[0].text

    @pytest.mark.asyncio
    async def test_pages_kwarg_passed_to_convert_uri(self, tmp_path):
        """Pages parameter should be passed through to MarkItDown as a kwarg."""
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake content")

        mock_result = MagicMock()
        mock_result.markdown = "# First page\n"

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter
            result = await self.tool.execute(
                {"file_path": str(pdf_file), "pages": "1-3"}
            )

        assert len(result) == 1
        assert "Converted with MarkItDown\n\n" in result[0].text
        mock_converter.convert_uri.assert_called_once_with(
            pdf_file.resolve().as_uri(), pages="1-3"
        )

    @pytest.mark.asyncio
    async def test_pages_not_passed_when_omitted(self, tmp_path):
        """When pages is not provided, convert_uri should be called without it."""
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake content")

        mock_result = MagicMock()
        mock_result.markdown = "# All pages\n"

        with patch(
            "jawafdehi_mcp.tools.document_converter.MarkItDown"
        ) as mock_markitdown:
            mock_converter = MagicMock()
            mock_converter.convert_uri.return_value = mock_result
            mock_markitdown.return_value = mock_converter
            result = await self.tool.execute({"file_path": str(pdf_file)})

        assert len(result) == 1
        mock_converter.convert_uri.assert_called_once_with(pdf_file.resolve().as_uri())


@pytest.mark.security
@pytest.mark.asyncio
async def test_isolated_conversion_drains_results_larger_than_pipe_buffer():
    payload = b"x" * 200_000

    markdown = await _run_isolated_stream_conversion(
        payload,
        StreamInfo(mimetype="text/plain", charset="utf-8", extension=".txt"),
        {},
    )

    assert markdown.rstrip() == payload.decode()


@pytest.mark.asyncio
async def test_timed_out_thread_failure_is_consumed_and_releases_capacity(
    monkeypatch,
):
    monkeypatch.setenv("MCP_DOCUMENT_CONVERT_TIMEOUT", "0.01")
    limiter = asyncio.Semaphore(1)
    await limiter.acquire()
    loop = asyncio.get_running_loop()
    unhandled = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    def fail_late():
        time.sleep(0.03)
        raise RuntimeError("late parser failure")

    try:
        with pytest.raises(ValueError, match="timed out"):
            await _run_bounded_conversion(fail_late, limiter)
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(previous_handler)

    assert limiter._value == 1
    assert unhandled == []
