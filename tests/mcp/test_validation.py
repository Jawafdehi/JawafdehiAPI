"""Tests for query validation logic."""

import json

import pytest

from jawafdehi_mcp.tools.ngm_judicial import NGMJudicialTool


class TestQueryValidation:
    """Test query validation rules."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = NGMJudicialTool()

    def test_valid_select_query(self):
        """Test that valid SELECT queries pass validation."""
        query = "SELECT * FROM courts"
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is True
        assert error is None

    def test_valid_join_query(self):
        """Test that valid JOIN queries pass validation."""
        query = """
            SELECT c.case_number, co.full_name_nepali
            FROM court_cases c
            JOIN courts co ON c.court_identifier = co.identifier
        """
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is True
        assert error is None

    def test_reject_insert_query(self):
        """Test that INSERT queries are rejected."""
        query = "INSERT INTO courts (identifier) VALUES ('test')"
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is False
        assert "only select" in error.lower()

    def test_reject_update_query(self):
        """Test that UPDATE queries are rejected."""
        query = "UPDATE courts SET full_name_nepali = 'test'"
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is False
        assert "only select" in error.lower()

    def test_reject_delete_query(self):
        """Test that DELETE queries are rejected."""
        query = "DELETE FROM courts WHERE identifier = 'test'"
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is False
        assert "only select" in error.lower()

    def test_reject_drop_query(self):
        """Test that DROP queries are rejected."""
        query = "DROP TABLE courts"
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is False
        assert "only select" in error.lower()

    def test_reject_scraped_dates_table(self):
        """Test that queries to scraped_dates table are rejected."""
        query = "SELECT * FROM scraped_dates"
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is False
        assert "scraped_dates" in error.lower()

    def test_reject_invalid_table(self):
        """Test that queries to non-allowed tables are rejected."""
        query = "SELECT * FROM users"
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is False
        assert "invalid table" in error.lower()

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT setval('seq', 1)",
            "SELECT nextval('seq')",
            "SELECT pg_advisory_lock(1)",
            "SELECT database_to_xml(true, false, '')",
            "SELECT pg_sleep_for(interval '1 second')",
            "SELECT table_to_xmlschema('courts'::regclass, true, false, '')",
        ],
    )
    def test_reuses_api_guard_for_side_effecting_functions(self, query):
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is False
        assert "forbidden function" in error.lower()

    def test_case_insensitive_validation(self):
        """Test that validation is case-insensitive."""
        query = "SELECT * FROM COURTS"
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is True
        assert error is None

    def test_complex_valid_query(self):
        """Test complex but valid query."""
        query = """
            SELECT
                cc.case_number,
                cc.case_type,
                co.full_name_nepali,
                COUNT(cch.id) as hearing_count
            FROM court_cases cc
            JOIN courts co ON cc.court_identifier = co.identifier
            LEFT JOIN court_case_hearings cch
                ON cc.case_number = cch.case_number
            WHERE cc.case_type LIKE '%भ्रष्टाचार%'
            GROUP BY cc.case_number, cc.case_type, co.full_name_nepali
            ORDER BY hearing_count DESC
            LIMIT 10
        """
        is_valid, error = self.tool._validate_query(query)
        assert is_valid is True
        assert error is None

    @pytest.mark.asyncio
    async def test_validation_error_is_json_encoded(self, monkeypatch):
        message = 'invalid table "quoted"\nwith a backslash: \\'
        monkeypatch.setattr(
            self.tool,
            "_validate_query",
            lambda query: (False, message),
        )

        result = await self.tool.execute({"query": "SELECT 1"})

        assert result.is_error is True
        assert json.loads(result[0].text)["error"] == message


class TestEnvironmentValidation:
    """Test environment variable validation."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = NGMJudicialTool()

    def test_missing_api_token_is_ok_in_public_mode(self, monkeypatch):
        """The public query tool can initialize without a configured credential."""
        monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", "https://portal.jawafdehi.org")
        monkeypatch.delenv("JAWAFDEHI_API_TOKEN", raising=False)
        monkeypatch.delenv("MCP_QUERY_API_TOKEN", raising=False)

        base_url, token = self.tool._validate_environment()
        assert base_url == "https://portal.jawafdehi.org"
        assert token is None

    def test_invalid_base_url(self, monkeypatch):
        """Test that non-HTTP URL raises error."""
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", "test-token")
        monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", "postgresql://example.local")

        with pytest.raises(ValueError, match=r"must be an HTTP\(S\) URL"):
            self.tool._validate_environment()

    def test_valid_base_url_and_token(self, monkeypatch):
        """Test that valid API base URL and token pass validation."""
        url = "https://portal.jawafdehi.org"
        token = "token-123"
        monkeypatch.setenv("JAWAFDEHI_API_BASE_URL", url)
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", token)

        result = self.tool._validate_environment()
        assert result == (url, token)

    def test_default_base_url_is_used(self, monkeypatch):
        """Test default base URL is used when env var is omitted."""
        monkeypatch.delenv("JAWAFDEHI_API_BASE_URL", raising=False)
        monkeypatch.setenv("MCP_QUERY_API_TOKEN", "token-123")

        result = self.tool._validate_environment()
        assert result[0] == "https://api.jawafdehi.org"
        assert result[1] == "token-123"
