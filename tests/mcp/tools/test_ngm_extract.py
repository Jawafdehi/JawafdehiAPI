"""Tests for NGMExtractCaseDataTool."""

from unittest.mock import mock_open, patch

import pytest

from jawafdehi_mcp.request_context import current_transport
from jawafdehi_mcp.tools.ngm_extract import NGMExtractCaseDataTool


class TestNGMExtractCaseDataTool:
    """Test functionality of NGMExtractCaseDataTool."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = NGMExtractCaseDataTool()
        self.valid_args = {
            "court_identifier": "supreme",
            "case_number": "079-WO-0123",
            "file_path": "/tmp/test_output.md",
        }

    @pytest.fixture(autouse=True)
    def _local_stdio_transport(self):
        token = current_transport.set("stdio")
        try:
            yield
        finally:
            current_transport.reset(token)

    def test_missing_arguments(self):
        """Test missing required arguments."""
        import asyncio

        arguments = {"court_identifier": "supreme"}
        result = asyncio.run(self.tool.execute(arguments))
        assert not result[0].text.startswith('{"success": true')
        assert "required" in result[0].text

    def test_format_markdown_empty_data(self):
        """Test format_markdown when no data is found."""
        md = self.tool._format_markdown({}, {}, [], [])
        assert "Unknown Court" in md
        assert "Unknown Case" in md
        assert "Could not find metadata" in md

    def test_format_markdown_with_data(self):
        """Test formatting valid case data into markdown."""
        court_info = {
            "full_name_english": "Supreme Court",
            "full_name_nepali": "सर्वोच्च अदालत",
        }
        case_info = {
            "case_number": "079-WO-123",
            "case_type": "Writ",
            "case_status": "Pending",
        }
        entities = [
            {"side": "Plaintiff", "name": "Ram", "address": "Kathmandu"},
            {"side": "Defendant", "name": "Shyam"},
        ]
        hearings = [
            {
                "hearing_date_ad": "2023-01-01",
                "decision_type": "Order",
                "judge_names": "Judge A",
            }
        ]

        md = self.tool._format_markdown(court_info, case_info, hearings, entities)

        assert "Supreme Court" in md
        assert "सर्वोच्च अदालत" in md
        assert "079-WO-123" in md
        assert "Writ" in md
        assert "Pending" in md
        assert "Ram" in md
        assert "Kathmandu" in md
        assert "Shyam" in md
        assert "2023-01-01" in md
        assert "Judge A" in md

        # JSON should only appear in the Appendix, not inline among the main sections
        appendix_idx = md.index("## Appendix: Raw Data")
        first_json_block = md.index("```json")
        assert first_json_block > appendix_idx, (
            "JSON blocks must only appear after the Appendix heading"
        )

        # No 'Full Case Record Details' or 'Hearing Full Details' headings should exist
        assert "Full Case Record Details" not in md
        assert "Hearing Full Details" not in md

    def test_format_markdown_appendix_structure(self):
        """Appendix with JSON blobs is present when data exists, absent when not."""
        court_info = {"full_name_english": "Test Court", "full_name_nepali": ""}
        case_info = {"case_number": "001", "case_type": "Civil"}
        hearing = {
            "hearing_date_ad": "2024-03-15",
            "hearing_date_bs": "2080-12-02",
            "decision_type": "Order",
            "judge_names": "Judge B",
        }

        # With data: Appendix present with both case and hearing JSON
        md_with_data = self.tool._format_markdown(court_info, case_info, [hearing], [])
        assert "## Appendix: Raw Data" in md_with_data
        assert "### Full Case Record" in md_with_data
        assert "### Hearing Records" in md_with_data
        assert "#### Hearing 1:" in md_with_data

        # Without data (empty case_info triggers early return): no Appendix
        md_no_data = self.tool._format_markdown({}, {}, [], [])
        assert "## Appendix: Raw Data" not in md_no_data

    @patch("os.makedirs")
    @patch("builtins.open", new_callable=mock_open)
    @patch(
        "jawafdehi_mcp.tools.ngm_extract.NGMExtractCaseDataTool._validate_environment"
    )
    @patch(
        "jawafdehi_mcp.tools.ngm_extract.NGMExtractCaseDataTool._execute_proxy_query"
    )
    def test_successful_extraction(
        self,
        mock_execute_proxy_query,
        mock_env,
        mock_file_open,
        mock_makedirs,
    ):
        """Test a full successful extraction flow."""
        mock_env.return_value = ("https://portal.jawafdehi.org", "test-token")
        mock_execute_proxy_query.side_effect = [
            {
                "success": True,
                "data": {
                    "columns": ["full_name_english", "full_name_nepali"],
                    "rows": [["Test Court", "टेस्ट अदालत"]],
                },
            },
            {
                "success": True,
                "data": {
                    "columns": ["case_number", "case_type", "case_status"],
                    "rows": [["123", "Writ", "Pending"]],
                },
            },
            {
                "success": True,
                "data": {
                    "columns": ["side", "name", "address"],
                    "rows": [["Plaintiff", "Ram", "Kathmandu"]],
                },
            },
            {
                "success": True,
                "data": {
                    "columns": ["hearing_date_ad", "decision_type", "judge_names"],
                    "rows": [["2023-01-01", "Order", "Judge A"]],
                },
            },
        ]

        import asyncio

        result = asyncio.run(self.tool.execute(self.valid_args))

        # Validate successful execution
        assert '{"success": true' in result[0].text
        assert mock_makedirs.called
        mock_file_open.assert_called_with("/tmp/test_output.md", "w", encoding="utf-8")

        # Verify content written includes formatted markdown
        handle = mock_file_open()
        written_content = "".join(
            [call.args[0] for call in handle.write.call_args_list]
        )
        assert "Test Court" in written_content

    @pytest.mark.security
    @pytest.mark.asyncio
    async def test_rejects_file_output_on_http_transport(self):
        with (
            patch.object(
                self.tool,
                "_validate_environment",
                side_effect=AssertionError("must not call the API"),
            ),
            patch(
                "builtins.open",
                side_effect=AssertionError("must not open server paths"),
            ),
        ):
            token = current_transport.set("http")
            try:
                result = await self.tool.execute(self.valid_args)
            finally:
                current_transport.reset(token)

        assert '"success": false' in result[0].text
        assert "only supported by local stdio" in result[0].text
