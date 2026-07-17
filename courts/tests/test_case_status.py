"""Unit tests for ``courts.case_status`` — real Nepali corpus values (no DB)."""

import pytest

from courts.case_status import (
    ACQUITTED,
    CONVICTED,
    DECIDED,
    PENDING,
    UNKNOWN,
    is_status_artifact,
    parse_case_status,
    verdict_from_hearings,
)


def test_header_artifact_is_not_a_status():
    # DQ-01: the ~103k Supreme rows whose case_status is the column header.
    p = parse_case_status("आदेश /फैसलाको किसिम")
    assert p.lifecycle_status == UNKNOWN
    assert p.verdict_type is None
    assert is_status_artifact("आदेश /फैसलाको किसिम") is True
    assert is_status_artifact("चालु") is False


@pytest.mark.parametrize("raw", ["चालु", "चलिरहेको", "चली रहेको", "विचाराधीन"])
def test_pending_spellings(raw):
    p = parse_case_status(raw)
    assert p.lifecycle_status == PENDING
    assert p.verdict_type is None


@pytest.mark.parametrize(
    "raw,verdict",
    [
        ("फैसला / अन्तिम आदेश >> अभियोग दावी पुग्ने", CONVICTED),  # व/ब variant unified
        ("फैसला / अन्तिम आदेश >> अभियोग दाबी नपुग्ने", ACQUITTED),
        ("फैसला / अन्तिम आदेश >> दाबी पुग्ने", "CLAIM_UPHELD"),
        ("फैसला / अन्तिम आदेश >> मिलापत्र", "SETTLED"),
        ("फैसला / अन्तिम आदेश >> डिसमिस", "DISMISSED"),
    ],
)
def test_arrow_outcomes(raw, verdict):
    p = parse_case_status(raw)
    assert p.lifecycle_status == DECIDED
    assert p.verdict_type == verdict
    assert p.verdict_date_bs is None  # arrow form carries no date


def test_arrow_unmapped_outcome_falls_through_to_other():
    p = parse_case_status("फैसला / अन्तिम आदेश >> कुनै नौलो नतिजा")
    assert p.lifecycle_status == DECIDED
    assert p.verdict_type == "OTHER"
    assert p.unmapped is True


def test_paren_date_recovers_verdict_date():
    # Special-court shape: outcome not in the status, but the date is.
    p = parse_case_status("फैसला (मिती: २०८२/०९/२८)")
    assert p.lifecycle_status == DECIDED
    assert p.verdict_date_bs == "2082-09-28"
    assert p.verdict_date_ad is not None  # bs_to_ad resolved
    assert p.verdict_type is None  # left for the hearing resolver


def test_empty_is_unknown():
    assert parse_case_status(None).lifecycle_status == UNKNOWN
    assert parse_case_status("").lifecycle_status == UNKNOWN


def test_verdict_from_hearings_special_schema():
    hearings = [
        {"case_status": "पेशी", "decision_type": "स्थगित"},
        {"case_status": "फैसला", "decision_type": "सफाई"},
    ]
    assert verdict_from_hearings(hearings) == ACQUITTED


def test_verdict_from_hearings_supreme_schema():
    # Supreme uses status/order_type instead of case_status/decision_type.
    hearings = [{"status": "फैसला", "order_type": "ठहर गर्ने"}]
    assert verdict_from_hearings(hearings) == CONVICTED


def test_verdict_from_hearings_none_when_no_terminal_decision():
    assert verdict_from_hearings([{"case_status": "पेशी", "decision_type": "स्थगित"}]) is None
    assert verdict_from_hearings([]) is None
    assert verdict_from_hearings(None) is None
