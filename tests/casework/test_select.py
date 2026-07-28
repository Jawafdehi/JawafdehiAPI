import pytest

from casework.common.select import (
    DEFAULT_ENRICHABLE_STATE, ENRICHABLE_STATES, court_number,
    is_ciaa_special_court_case, is_enrichable_state, matches_fiscal_year,
    select_cases,
)

SPECIAL = "https://jawafdehi.org/courtcase/special/081-cr-0098"
SUPREME = "https://jawafdehi.org/courtcase/supreme/075-wf-0005"


def test_recognises_special_court_iri():
    assert is_ciaa_special_court_case({"court_cases": [SPECIAL]})


def test_rejects_retired_colon_prefix_shape():
    # Must assert on the ACTUAL retired shape. An earlier version of this
    # test asserted on an empty list, which passes even under a full
    # revert to `startswith("special:")` -- a paper tiger that reads like
    # a guard but discriminates nothing.
    assert not is_ciaa_special_court_case({"court_cases": ["special:081-CR-0098"]})
    assert not is_ciaa_special_court_case({"court_cases": []})


def test_court_number_extracted_from_iri():
    assert court_number({"court_cases": [SPECIAL]}) == "081-cr-0098"


def test_fiscal_year_matching_is_case_insensitive():
    # The IRI lowercases the number. A case-sensitive match returned 0 of
    # 2,912 live cases on 2026-07-18 -- a silent, total selection failure.
    assert matches_fiscal_year({"court_cases": [SPECIAL]}, "081")
    assert matches_fiscal_year({"court_cases": [SPECIAL.upper()]}, "081")
    assert not matches_fiscal_year({"court_cases": [SUPREME]}, "081")


def test_fiscal_year_leading_zero_forms_all_select_the_same_case():
    # The canonical IRI carries a zero-padded case number (081-cr-0098). A
    # naive (un-normalised) prefix comparison built needle "81-" against a
    # "081-..." case number and matched NOTHING: --fiscal-year 81 silently
    # selected 0 cases while --fiscal-year 081 selected the same case
    # correctly -- a landmine that looks like a clean run, not an error.
    # Donor: casework/common.py:420, `fy = fiscal_year.lstrip("0") or "0"`.
    case = {"court_cases": [SPECIAL]}  # .../special/081-cr-0098
    assert matches_fiscal_year(case, "81")
    assert matches_fiscal_year(case, "081")
    assert matches_fiscal_year(case, "0081")


def test_fiscal_year_all_zero_forms_fall_back_to_literal_zero():
    # A naive `lstrip("0")` on "0" or "00" collapses to "", which would
    # then match against ANY case number whose prefix strips to "" too --
    # or worse, never match at all depending on how the empty string is
    # compared. The donor's `or "0"` fallback exists precisely for this;
    # pin it against a case whose prefix is genuinely "000" (i.e. also
    # normalises to "0"), not against the "" trap.
    zero_case = {"court_cases": [
        "https://jawafdehi.org/courtcase/special/000-cr-0001"]}
    assert matches_fiscal_year(zero_case, "0")
    assert matches_fiscal_year(zero_case, "00")
    assert not matches_fiscal_year({"court_cases": [SPECIAL]}, "0")


def test_fiscal_year_no_cr_marker_never_matches():
    # A courtcase number with no "-cr-" segment (e.g. a writ number like
    # 075-wf-0005) must never satisfy any fiscal_year -- the donor only
    # ever compares `-CR-` prefixes.
    assert not matches_fiscal_year({"court_cases": [SUPREME]}, "75")
    assert not matches_fiscal_year({"court_cases": [SUPREME]}, "075")


def test_state_gate_allows_draft_only():
    # IN_REVIEW was in ENRICHABLE_STATES and is deliberately gone: a bulk run
    # gating on it rewrites cases a moderator already has open for review.
    assert is_enrichable_state({"state": "DRAFT"})
    assert not is_enrichable_state({"state": "IN_REVIEW"})
    assert not is_enrichable_state({"state": "PUBLISHED"})


def test_default_state_is_draft_and_nothing_else():
    # Pinned as an exact tuple, not `"DRAFT" in ENRICHABLE_STATES`: the latter
    # still passes if IN_REVIEW is added back alongside it, which is the exact
    # regression this constant exists to prevent.
    assert ENRICHABLE_STATES == ("DRAFT",)
    assert DEFAULT_ENRICHABLE_STATE == "DRAFT"


def test_bulk_selection_skips_in_review_cases():
    cases = [
        {"slug": "d", "state": "DRAFT", "court_cases": [SPECIAL]},
        {"slug": "r", "state": "IN_REVIEW", "court_cases": [SPECIAL]},
    ]
    assert [c["slug"] for c in select_cases(cases)] == ["d"]


def test_bulk_selection_refuses_an_explicit_in_review_state():
    # The DRAFT default is not the guarantee -- this is. Without the refusal,
    # `--state IN_REVIEW` (or any caller passing it) quietly re-opens exactly
    # the hole the narrowed default closed.
    cases = [{"slug": "r", "state": "IN_REVIEW", "court_cases": [SPECIAL]}]
    with pytest.raises(ValueError, match="IN_REVIEW"):
        select_cases(cases, states=("IN_REVIEW",))
    with pytest.raises(ValueError, match="IN_REVIEW"):
        select_cases(cases, states=("DRAFT", "IN_REVIEW"))


def test_bulk_selection_honours_an_explicitly_requested_state():
    # `states` is a real parameter (wired to --state), not decoration: a state
    # that is neither the default nor forbidden must actually select.
    cases = [
        {"slug": "d", "state": "DRAFT", "court_cases": [SPECIAL]},
        {"slug": "p", "state": "PUBLISHED", "court_cases": [SPECIAL]},
    ]
    assert [c["slug"] for c in select_cases(cases, states=("PUBLISHED",))] == ["p"]


def test_explicit_slug_still_reaches_an_in_review_case():
    # The bypass is retained on purpose: naming one case is an operator acting
    # on a case they have already looked at, and the run is dry by default.
    # Only the *bulk* sweep is barred from IN_REVIEW.
    cases = [{"slug": "r", "state": "IN_REVIEW", "court_cases": [SPECIAL]}]
    assert select_cases(cases) == []
    assert len(select_cases(cases, slugs=("r",))) == 1
    # ...and the forbidden-state refusal must not fire on the bypass path,
    # where it would break single-case runs for no safety gain.
    assert len(select_cases(cases, slugs=("r",), states=("IN_REVIEW",))) == 1


def test_explicit_slug_bypasses_state_gate():
    cases = [{"slug": "pub", "state": "PUBLISHED", "court_cases": [SPECIAL]}]
    assert select_cases(cases) == []
    assert len(select_cases(cases, slugs=("pub",))) == 1


def test_court_case_bypass_also_skips_state_gate():
    # The `slugs=` bypass is tested elsewhere; `court_cases=` is a separate
    # code path and was previously uncovered.
    cases = [{"slug": "pub", "state": "PUBLISHED", "court_cases": [SPECIAL]}]
    assert select_cases(cases) == []
    assert len(select_cases(cases, court_cases=("081-CR-0098",))) == 1  # uppercase input


def test_selection_is_non_empty_for_realistic_sample():
    # Guards the landmine: a regression here yields 0 and looks like success.
    cases = [{"slug": f"c{i}", "state": "DRAFT", "court_cases": [SPECIAL]} for i in range(5)]
    assert len(select_cases(cases, fiscal_year="081")) == 5
