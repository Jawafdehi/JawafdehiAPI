"""Unit tests for the review engine's bigo-present gate.

`bigo_amount_present` is a hard gate for CIAA cases: the bigo (बिगो) must be a
positive integer, OR the case must certify a legitimate no-bigo via a `NO_BIGO:`
marker line in internal_notes. A bare empty bigo (the common data gap) still fails.
"""

from review.rules_engine import bigo_amount_present


def test_positive_bigo_passes():
    score, issues = bigo_amount_present({"bigo": 237546324})
    assert score == 100
    assert not issues


def test_null_bigo_without_marker_fails():
    score, issues = bigo_amount_present({"bigo": None, "internal_notes": ""})
    assert score == 0
    assert issues


def test_empty_string_bigo_fails():
    score, issues = bigo_amount_present({"bigo": ""})
    assert score == 0
    assert issues


def test_negative_bigo_fails():
    score, issues = bigo_amount_present({"bigo": -5})
    assert score == 0
    assert issues


def test_non_numeric_bigo_fails():
    score, issues = bigo_amount_present({"bigo": "abc"})
    assert score == 0
    assert issues


def test_no_bigo_marker_passes_with_null():
    score, issues = bigo_amount_present(
        {
            "bigo": None,
            "internal_notes": "NO_BIGO: record_offence — बिगो रकम उल्लेख छैन",
        }
    )
    assert score == 100
    assert not issues


def test_no_bigo_marker_case_and_spacing_insensitive():
    for note in ("no bigo: pre-charge", "  No_Bigo - district court", "NO-BIGO"):
        score, issues = bigo_amount_present({"bigo": None, "internal_notes": note})
        assert score == 100, note
        assert not issues


def test_marker_must_be_at_line_start():
    # An incidental mention of "no bigo" mid-sentence should NOT certify.
    score, issues = bigo_amount_present(
        {"bigo": None, "internal_notes": "we found no bigo figure in the source yet"}
    )
    assert score == 0
    assert issues


def test_zero_bigo_with_marker_passes():
    score, issues = bigo_amount_present(
        {"bigo": 0, "internal_notes": "NO_BIGO: non_ciaa"}
    )
    assert score == 100
    assert not issues


def test_zero_bigo_without_marker_fails():
    score, issues = bigo_amount_present({"bigo": 0, "internal_notes": ""})
    assert score == 0
    assert issues
