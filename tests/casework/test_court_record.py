"""The accused name source: a case's own NGM court record.

Why names and not ids: NGM *can* store a resolved `nes_id` per party, but
measured across the 59 published cases it does so on exactly one
(`special/080-cr-0111`) -- 185 of 659 accused binds, 28.1%. Everywhere else the
field is null. So the court record is authoritative for WHO the defendants are,
and resolution still has to happen. What this buys is a clean, authoritative name
list in place of the LLM's extraction.
"""

import urllib.error

import pytest

from casework.court_record import court_ref, defendant_names


class _Api:
    """Stub. `parties` is keyed by "<court>/<number>"; a value that is an
    HTTPError is raised instead of returned."""

    def __init__(self, parties):
        self.parties = parties
        self.calls = []

    def get_court_case_entities(self, court, number, timeout=60):
        key = f"{court}/{number}"
        self.calls.append(key)
        value = self.parties.get(key, [])
        if isinstance(value, Exception):
            raise value
        return value


CASE = {"court_cases": ["https://jawafdehi.org/courtcase/special/080-cr-0111"]}


def test_court_ref_parses_a_courtcase_iri():
    assert court_ref("https://jawafdehi.org/courtcase/special/080-cr-0111") == (
        "special", "080-cr-0111")


@pytest.mark.parametrize("bad", [
    "", None, "not-an-iri",
    "https://jawafdehi.org/courtcase/special",           # no case number
    "https://jawafdehi.org/courtcase/special/a/b",       # too many segments
    "https://jawafdehi.org/entity/person/kamala-thapa",  # not a courtcase IRI
])
def test_court_ref_refuses_anything_else(bad):
    assert court_ref(bad) is None


def test_returns_defendant_names_only():
    api = _Api({"special/080-cr-0111": [
        {"side": "plaintiff", "name": "नेपाल सरकार"},
        {"side": "defendant", "name": "अनार देवी झा"},
        {"side": "defendant", "name": "सिताराम यादव"},
    ]})
    names, skips = defendant_names(api, CASE)
    assert names == ["अनार देवी झा", "सिताराम यादव"]
    assert not skips


def test_never_returns_a_plaintiff():
    api = _Api({"special/080-cr-0111": [
        {"side": "plaintiff", "name": "नेपाल सरकार"},
    ]})
    names, _ = defendant_names(api, CASE)
    assert names == []


def test_side_matching_is_case_and_space_tolerant():
    api = _Api({"special/080-cr-0111": [
        {"side": " Defendant ", "name": "अनार देवी झा"},
    ]})
    names, _ = defendant_names(api, CASE)
    assert names == ["अनार देवी झा"]


def test_deduplicates_a_name_repeated_across_two_court_references():
    api = _Api({
        "special/080-cr-0111": [{"side": "defendant", "name": "अनार देवी झा"}],
        "supreme/075-wf-0005": [{"side": "defendant", "name": "अनार देवी झा"}],
    })
    case = {"court_cases": [
        "https://jawafdehi.org/courtcase/special/080-cr-0111",
        "https://jawafdehi.org/courtcase/supreme/075-wf-0005",
    ]}
    names, _ = defendant_names(api, case)
    assert names == ["अनार देवी झा"]
    assert api.calls == ["special/080-cr-0111", "supreme/075-wf-0005"]


def test_reports_a_case_with_no_court_reference():
    names, skips = defendant_names(_Api({}), {"court_cases": []})
    assert names == []
    assert "no court reference" in skips[0]


def test_a_404_court_reference_is_reported_not_raised():
    """9 of the 49 published court refs 404 -- a stale or mistyped number must
    not abort the case."""
    err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    api = _Api({"special/080-cr-0111": err})
    names, skips = defendant_names(api, CASE)
    assert names == []
    assert "404" in skips[0]


def test_one_bad_court_reference_does_not_lose_a_good_one():
    err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    api = _Api({
        "special/080-cr-0111": err,
        "supreme/075-wf-0005": [{"side": "defendant", "name": "सिताराम यादव"}],
    })
    case = {"court_cases": [
        "https://jawafdehi.org/courtcase/special/080-cr-0111",
        "https://jawafdehi.org/courtcase/supreme/075-wf-0005",
    ]}
    names, skips = defendant_names(api, case)
    assert names == ["सिताराम यादव"]
    assert len(skips) == 1


def test_blank_and_missing_names_are_dropped():
    api = _Api({"special/080-cr-0111": [
        {"side": "defendant", "name": "   "},
        {"side": "defendant"},
        {"side": "defendant", "name": "सिताराम यादव"},
    ]})
    names, _ = defendant_names(api, CASE)
    assert names == ["सिताराम यादव"]
