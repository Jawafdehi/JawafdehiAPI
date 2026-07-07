"""Unit tests for ``courts.normalize`` helpers."""

from courts.normalize import parse_stated_defendant_count


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
