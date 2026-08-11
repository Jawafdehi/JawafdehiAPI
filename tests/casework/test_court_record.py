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

from casework.court_record import (
    BINDABLE_CODES,
    UNPARSEABLE,
    case_number_code,
    court_record_for_case,
    court_ref,
    defendant_names,
    party_legal_name,
    split_alias,
)


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


class _FullApi:
    """Stub covering all three court reads. Values that are Exceptions raise."""

    def __init__(self, detail=None, hearings=None, parties=None):
        self.detail, self.hearings, self.parties = detail or {}, hearings or {}, parties or {}

    def _pick(self, store, court, number):
        value = store.get(f"{court}/{number}")
        if isinstance(value, Exception):
            raise value
        return value

    def get_courtcase(self, court, number, timeout=60):
        return self._pick(self.detail, court, number) or {}

    def list_hearings(self, court, number, timeout=60):
        return self._pick(self.hearings, court, number) or []

    def get_court_case_entities(self, court, number, timeout=60):
        return self._pick(self.parties, court, number) or []


CASE_0151 = {"court_cases": ["https://jawafdehi.org/courtcase/special/079-cr-0151"]}


def test_reads_detail_hearings_and_parties_for_every_reference():
    api = _FullApi(
        detail={"special/079-cr-0151": {"registration_date_ad": "2023-06-22"}},
        hearings={"special/079-cr-0151": [{"case_status": "फैसला",
                                           "hearing_date_ad": "2024-06-04"}]},
        parties={"special/079-cr-0151": [{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}]},
    )
    records, skips = court_record_for_case(api, CASE_0151)
    assert skips == []
    assert len(records) == 1
    assert records[0]["court"] == "special"
    assert records[0]["detail"]["registration_date_ad"] == "2023-06-22"
    assert records[0]["hearings"][0]["hearing_date_ad"] == "2024-06-04"
    assert records[0]["parties"][0]["name"] == "कृष्ण प्रसाद यादव"


def test_an_unreadable_reference_is_a_skip_not_a_raise():
    api = _FullApi(detail={"special/079-cr-0151": urllib.error.HTTPError(
        "u", 404, "Not Found", None, None)})
    records, skips = court_record_for_case(api, CASE_0151)
    assert records == []
    assert "404" in skips[0]


def test_one_bad_reference_does_not_cost_the_others():
    case = {"court_cases": [
        "https://jawafdehi.org/courtcase/special/079-cr-0151",
        "https://jawafdehi.org/courtcase/special/080-cr-0111",
    ]}
    api = _FullApi(
        detail={
            "special/079-cr-0151": urllib.error.HTTPError("u", 404, "gone", None, None),
            "special/080-cr-0111": {"registration_date_ad": "2024-01-01"},
        },
    )
    records, skips = court_record_for_case(api, case)
    assert [r["number"] for r in records] == ["080-cr-0111"]
    assert len(skips) == 1


def test_no_court_reference_reports_why():
    records, skips = court_record_for_case(_FullApi(), {"court_cases": []})
    assert records == []
    assert "no court reference" in skips[0]


@pytest.mark.parametrize("number, expected", [
    ("079-CR-0151", "CR"),
    # Lower-case in, upper-case out: a case-sensitive `"-CR-" in number` check
    # would miss this and wrongly skip the bind.
    ("078-cb-1372", "CB"),
    # The pre-FY073 format carries no type segment at all. A rule spelled
    # "must contain a `-XX-` code" would misclassify this as unrecognised and
    # skip 139 real prosecutions in the corpus.
    ("93-068-0194", ""),
    ("081-RE-1730", "RE"),
    # No hyphens at all: a `.split("-")[1]` implementation would raise
    # IndexError here instead of falling back to a safe value. Not the
    # legacy shape either (no hyphens at all), so this is UNPARSEABLE, not
    # the pre-FY073 "" bucket -- an allow-list miss, not a silent prosecution.
    ("0791234", UNPARSEABLE),
])
def test_case_number_code_classifies_the_court_case_type(number, expected):
    assert case_number_code(number) == expected


@pytest.mark.parametrize("number", [
    "W-081-0037",     # a writ code, but not between two hyphens -- must not
                      # fall into the pre-FY073 "" bucket and bind as a
                      # prosecution.
    "RE-081-1730",    # code-first ordering: no `-<letters>-` segment exists.
    "079_CR_0151",    # underscores, not hyphens: not the legacy shape either.
    "०८१-आरई-१७३०",   # Devanagari digits and letters: `_CODE_SEGMENT` only
                      # matches ASCII letters, and the middle segment is not
                      # `[0-9]+`, so this cannot be the legacy shape.
])
def test_case_number_code_refuses_to_guess_an_unparseable_number(number):
    # Reviewer repro: all four used to return "" (BINDABLE), inverting the
    # allow-list rule for a number the parser genuinely cannot read.
    code = case_number_code(number)
    assert code == UNPARSEABLE
    assert code not in BINDABLE_CODES


def test_the_legacy_all_digit_shape_still_classifies_as_a_prosecution():
    assert case_number_code("93-068-0194") == ""


# ------------------------------------------------------- the `भन्ने` alias form

def test_the_legal_name_follows_the_alias_marker():
    # The bug this fixes: binding the raw string created an NES person named
    # `आवास भन्ने आभाश अर्याल`, slugged `avasa-bhanne-abhasha-aryala`.
    assert split_alias("आवास भन्ने आभाश अर्याल") == ("आभाश अर्याल", ["आवास"])


def test_a_name_with_two_markers_splits_on_the_last_one():
    # Real: special/078-CR-0062. Splitting on the FIRST marker would keep
    # `विनि बहादुर भन्ने विनि बहादुर बानिया` as the name.
    legal, aliases = split_alias("बलराम भन्ने विनि बहादुर भन्ने विनि बहादुर बानिया")
    assert legal == "विनि बहादुर बानिया"
    assert aliases == ["बलराम", "विनि बहादुर"]


@pytest.mark.parametrize(("raw", "legal"), [
    # Every remaining occurrence in the FY078/079 corpus. The post-marker half
    # is the one carrying a real surname in all of them.
    ("हश्मुल्लाह खाँ भन्ने हश्मुल्लाह मुसलमान", "हश्मुल्लाह मुसलमान"),
    ("सागर माली भन्ने सागर सािताराम लोखण्डे", "सागर सािताराम लोखण्डे"),
    ("गज बहादुर रावत भन्ने गजरा रावत", "गजरा रावत"),
    ("करिष्मा नेपाली भन्ने करिष्मा परियार", "करिष्मा परियार"),
    ("बाबुराम शर्मा भन्ने उद्धव बहादुर खात्री", "उद्धव बहादुर खात्री"),
    ("श्याम प्रदेशी भन्ने श्यामकृष्ण साह", "श्यामकृष्ण साह"),
])
def test_every_corpus_alias_resolves_to_the_post_marker_name(raw, legal):
    assert split_alias(raw)[0] == legal


def test_a_name_without_the_marker_is_returned_whole():
    assert split_alias("कृष्ण प्रसाद यादव") == ("कृष्ण प्रसाद यादव", [])


@pytest.mark.parametrize("raw", [
    "कृष्ण प्रसाद यादव भन्ने",      # truncated record -- nothing after
    "भन्ने कृष्ण प्रसाद यादव",      # nothing before
    "भन्ने",
])
def test_a_marker_with_an_empty_side_leaves_the_name_alone(raw):
    # Refusing to bind the empty string is the whole point: a truncated record
    # is a record to look at, not an instruction to name someone "".
    assert split_alias(raw) == (raw, [])


def test_party_legal_name_strips_the_alias_from_a_party_row():
    party = {"side": "defendant", "name": "  आवास भन्ने आभाश अर्याल  "}
    assert party_legal_name(party) == "आभाश अर्याल"
    assert party_legal_name({"side": "defendant"}) == ""
