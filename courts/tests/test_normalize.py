"""Unit tests for ``courts.normalize`` helpers."""

import pytest

from courts.normalize import normalize_case_type, parse_stated_defendant_count


def test_parse_stated_count_ascii_and_devanagari():
    # "<lead> समेत N" states the court's true defendant total; Devanagari digits
    # are normalized to ASCII before parsing.
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत ३७") == (37, False)
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत 98") == (98, False)


def test_parse_stated_count_bare_samet_is_unknown():
    # A trailing bare "समेत" (no number) = truncated, magnitude unknown.
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत") == (None, True)


def test_parse_stated_count_bare_samet_with_trailing_punctuation():
    # Scraped cells may carry a trailing danda / full stop after "समेत".
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत।") == (None, True)
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत ॥") == (None, True)


def test_parse_stated_count_double_samet_takes_the_number():
    # Some cells repeat "समेत"; the stated number is still recovered.
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत ६४ समेत") == (64, False)


def test_parse_stated_count_no_signal():
    # A complete, non-truncated cell carries no signal.
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी") == (None, False)
    assert parse_stated_defendant_count("") == (None, False)
    assert parse_stated_defendant_count(None) == (None, False)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # भ्रष्टाचार ( X ) wrapper -> inner offense.
        ("भ्रष्टाचार ( रकम हिनामिना )", "रकम हिनामिना"),
        # Leading/trailing structured case-number token stripped, label kept.
        ("080-cp-1852 लेनदेन", "लेनदेन"),
        ("074-CP-1181, भुल वा त्रुटि शंशोधन", "भुल वा त्रुटि शंशोधन"),
        ("(079-C1-0427) अपराधिक उपद्रव", "अपराधिक उपद्रव"),
        ("०७९-CP-२३४२ लेनदेन", "लेनदेन"),  # Devanagari-digit case number
        ("अपराधिक उपद्रव (079-C1-0427)", "अपराधिक उपद्रव"),
        # A separator (comma) before a trailing token is consumed whole — no
        # dangling "लेनदेन," left behind.
        ("लेनदेन, 080-cp-1852", "लेनदेन"),
        ("भुल सुधार; 074-CP-1181", "भुल सुधार"),
    ],
)
def test_normalize_case_type_strips_case_numbers(raw, expected):
    assert normalize_case_type(raw) == expected


@pytest.mark.parametrize(
    "value",
    [
        # Statute citations are the MOST useful case_type values — never touched,
        # even though they contain digits and parentheses.
        "चोरी गरेको (दफा 241)",
        "जबरजस्ती करणी गरेको (दफा 219)",
        "अंशबण्डा गरिपाऊँ (दफा २०५), (२१५)",
        "मु.फौ.सं. को दफा 155 बमोजिमको निवेदन",
        # Section references and clean labels pass through.
        "१५५ को सुविधा पाउँ",
        "155 बमोजिमको निवेदन",
        "चेक अनादर",
        # A value that is ONLY a case number has no label to keep -> unchanged
        # (never emptied to something misleading).
        "080-c1-0199",
        "3942",
    ],
)
def test_normalize_case_type_preserves_meaningful_values(value):
    assert normalize_case_type(value) == value


def test_normalize_case_type_is_idempotent():
    for raw in ("भ्रष्टाचार ( रकम हिनामिना )", "080-cp-1852 लेनदेन", "चोरी गरेको (दफा 241)"):
        once = normalize_case_type(raw)
        assert normalize_case_type(once) == once


@pytest.mark.parametrize(
    "value",
    [
        "चेक  अनादर",  # internal double space
        " चेक अनादर ",  # leading/trailing whitespace
        '"लेनदेन"',  # surrounding quotes
        "  080-c1-0199  ",  # pure case number with padding
    ],
)
def test_normalize_case_type_cosmetic_only_returns_input_verbatim(value):
    # Whitespace / surrounding quotes are NOT structural noise: with no
    # case-number token or wrapper to strip, the raw input is returned unchanged,
    # so the importer does not rewrite / re-archive / re-count it for cosmetics.
    assert normalize_case_type(value) == value


@pytest.mark.parametrize("value", ["", None])
def test_normalize_case_type_empty(value):
    assert normalize_case_type(value) == value
