from casework.common.select import (
    court_number, is_ciaa_special_court_case, is_enrichable_state,
    matches_fiscal_year, select_cases,
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


def test_state_gate_allows_draft_and_in_review_only():
    assert is_enrichable_state({"state": "DRAFT"})
    assert is_enrichable_state({"state": "IN_REVIEW"})
    assert not is_enrichable_state({"state": "PUBLISHED"})


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
