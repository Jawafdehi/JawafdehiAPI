import pytest
from django.db.utils import DatabaseError

from ngm import services
from ngm.services import apply_row_cap, execute_select_query, validate_query


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM court_cases",
        "SELECT * FROM public.court_cases",
        "select case_number from court_case_entities",
        "SELECT c.identifier FROM courts c JOIN court_cases cc ON cc.court_identifier = c.identifier",
    ],
)
def test_validate_query_accepts_allowed_select_queries(query):
    is_valid, error = validate_query(query)
    assert is_valid is True
    assert error is None


@pytest.mark.parametrize(
    "query,error_contains",
    [
        ("UPDATE court_cases SET case_type = 'X'", "Only SELECT queries are allowed"),
        ("SELECT * FROM scraped_dates", "scraped_dates"),
        ("SELECT * FROM public.scraped_dates", "scraped_dates"),
        ("SELECT * FROM users", "Invalid table"),
        ("SELECT * FROM public.users", "Invalid table"),
        ("SELECT * FROM court_cases; DELETE FROM court_cases", "Forbidden keyword"),
    ],
)
def test_validate_query_rejects_unsafe_queries(query, error_contains):
    is_valid, error = validate_query(query)
    assert is_valid is False
    assert error_contains in error


def test_apply_row_cap_wraps_query_and_limits_rows():
    query = "SELECT * FROM court_cases ORDER BY registration_date_ad DESC"
    capped = apply_row_cap(query, max_rows=500)
    assert capped.startswith("SELECT * FROM (")
    assert "LIMIT 500" in capped
    assert "ORDER BY registration_date_ad DESC" in capped


def test_execute_select_query_hides_database_error_details(monkeypatch, caplog):
    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            raise DatabaseError("sensitive database details")

    class FakeConnection:
        vendor = "postgresql"

        def cursor(self):
            return FakeCursor()

    monkeypatch.setitem(
        services.settings.DATABASES, "ngm", {"ENGINE": "django.db.backends.postgresql"}
    )
    monkeypatch.setattr(services, "connections", {"ngm": FakeConnection()})

    with pytest.raises(ValueError, match="Database query failed"):
        execute_select_query("SELECT * FROM court_cases", 5)

    assert "NGM database query failed" in caplog.text
    assert "sensitive database details" in caplog.text


class TestNormalizeCaseNumber:
    """Test suite for case number normalization."""

    def test_already_normalized(self):
        """Already normalized case numbers pass through unchanged."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("081-CR-0081") == "081-CR-0081"
        assert normalize_case_number("123-WR-4567") == "123-WR-4567"

    def test_lowercase_conversion(self):
        """Lowercase letters are converted to uppercase."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("081-cr-0081") == "081-CR-0081"
        assert normalize_case_number("081-Cr-0081") == "081-CR-0081"
        assert normalize_case_number("081-cR-0081") == "081-CR-0081"

    def test_missing_leading_zeros_first_part(self):
        """Missing leading zeros in first part are added."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("81-CR-0081") == "081-CR-0081"
        assert normalize_case_number("1-CR-0081") == "001-CR-0081"
        assert normalize_case_number("12-CR-0081") == "012-CR-0081"

    def test_missing_leading_zeros_last_part(self):
        """Missing leading zeros in last part are added."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("081-CR-81") == "081-CR-0081"
        assert normalize_case_number("081-CR-1") == "081-CR-0001"
        assert normalize_case_number("081-CR-123") == "081-CR-0123"

    def test_missing_leading_zeros_both_parts(self):
        """Missing leading zeros in both parts are added."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("81-CR-81") == "081-CR-0081"
        assert normalize_case_number("1-CR-1") == "001-CR-0001"

    def test_devanagari_numerals(self):
        """Devanagari numerals are converted to ASCII."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("०८१-CR-००८१") == "081-CR-0081"
        assert normalize_case_number("०१२-WR-३४५६") == "012-WR-3456"

    def test_devanagari_with_lowercase(self):
        """Devanagari numerals with lowercase letters."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("०८१-cr-००८१") == "081-CR-0081"

    def test_devanagari_missing_zeros(self):
        """Devanagari numerals without leading zeros."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("८१-CR-८१") == "081-CR-0081"

    def test_all_devanagari_digits(self):
        """Test all Devanagari digits 0-9."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("०१२-CR-३४५६") == "012-CR-3456"
        assert normalize_case_number("७८९-WR-०१२३") == "789-WR-0123"

    def test_different_middle_parts(self):
        """Different letter combinations in middle part."""
        from ngm.services import normalize_case_number

        assert normalize_case_number("081-WR-0081") == "081-WR-0081"
        assert normalize_case_number("081-ABC-0081") == "081-ABC-0081"
        assert normalize_case_number("081-X-0081") == "081-X-0081"

    def test_empty_case_number(self):
        """Empty case number raises ValueError."""
        from ngm.services import normalize_case_number

        with pytest.raises(ValueError, match="Case number cannot be empty"):
            normalize_case_number("")

    def test_invalid_format_no_dashes(self):
        """Case number without dashes raises ValueError."""
        from ngm.services import normalize_case_number

        with pytest.raises(ValueError, match="Invalid case number format"):
            normalize_case_number("081CR0081")

    def test_invalid_format_one_dash(self):
        """Case number with only one dash raises ValueError."""
        from ngm.services import normalize_case_number

        with pytest.raises(ValueError, match="Invalid case number format"):
            normalize_case_number("081-CR0081")

    def test_invalid_format_letters_in_numbers(self):
        """Letters in numeric parts raise ValueError."""
        from ngm.services import normalize_case_number

        with pytest.raises(ValueError, match="Invalid case number format"):
            normalize_case_number("08A-CR-0081")
        with pytest.raises(ValueError, match="Invalid case number format"):
            normalize_case_number("081-CR-008B")

    def test_invalid_format_numbers_in_middle(self):
        """Numbers in middle part raise ValueError."""
        from ngm.services import normalize_case_number

        with pytest.raises(ValueError, match="Invalid case number format"):
            normalize_case_number("081-C1-0081")

    def test_invalid_format_special_characters(self):
        """Special characters raise ValueError."""
        from ngm.services import normalize_case_number

        with pytest.raises(ValueError, match="Invalid case number format"):
            normalize_case_number("081-CR-0081!")
        with pytest.raises(ValueError, match="Invalid case number format"):
            normalize_case_number("081@CR-0081")

    def test_none_case_number(self):
        """None case number raises ValueError."""
        from ngm.services import normalize_case_number

        with pytest.raises(ValueError, match="Case number cannot be empty"):
            normalize_case_number(None)

    def test_extra_long_numbers(self):
        """Numbers longer than expected are preserved."""
        from ngm.services import normalize_case_number

        # First part longer than 3 digits
        assert normalize_case_number("1234-CR-0081") == "1234-CR-0081"
        # Last part longer than 4 digits
        assert normalize_case_number("081-CR-12345") == "081-CR-12345"
