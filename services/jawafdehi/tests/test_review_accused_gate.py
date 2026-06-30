"""Unit tests for the review engine's accused-present gate.

The `accused_present` detector is a hard gate for CORRUPTION cases but must
pass automatically for other case types (e.g. TAX_EVASION), which do not
require a named accused party.
"""

from review.rules_engine import accused_present


def _case(case_type, entities):
    return {"case_type": case_type, "entities": entities}


def test_corruption_without_accused_fails():
    score, issues = accused_present(_case("CORRUPTION", []))
    assert score == 0
    assert issues


def test_corruption_with_accused_passes():
    score, issues = accused_present(_case("CORRUPTION", [{"type": "accused"}]))
    assert score == 100
    assert not issues


def test_tax_evasion_without_accused_passes():
    score, issues = accused_present(_case("TAX_EVASION", []))
    assert score == 100
    assert not issues


def test_missing_case_type_is_not_treated_as_corruption():
    # Defensive: an unknown/blank case_type should not trip the hard gate.
    score, issues = accused_present(_case("", []))
    assert score == 100
    assert not issues
