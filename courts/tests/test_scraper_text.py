"""Unit tests for the shared court-portal text helpers, focused on the judge
run-on de-separator — the fix for the multi-judge concatenation DQ bug where a
bare ``get_text()`` glued the next judge's honorific onto the previous name
(``…प्याकुरेलमा. न्या. श्री…``) across ~1.56M high-court + ~212k Supreme hearings."""

from bs4 import BeautifulSoup

from courts.scraper.text import desep_judges, extract_judges


def _cell(html: str):
    return BeautifulSoup(html, "html.parser").find("td")


def test_desep_judges_splits_glued_high_honorific():
    # High court honorific "मा. न्या." (with a space). A ", " is inserted before the
    # second judge; the FIRST judge (honorific at string start) is left unprefixed.
    glued = "मा. न्या. श्री राममा. न्या. श्री श्याम"
    assert desep_judges(glued) == "मा. न्या. श्री राम, मा. न्या. श्री श्याम"


def test_desep_judges_splits_glued_supreme_honorific():
    # Supreme uses "मा.न्या." with NO space after मा. — the anchor matches both forms.
    glued = "मा.न्या. श्री एकमा.न्या. श्री दुई"
    assert desep_judges(glued) == "मा.न्या. श्री एक, मा.न्या. श्री दुई"


def test_desep_judges_splits_glued_chief_judge():
    # Chief-judge honorific "मा. मु. न्या." is anchored on the LEADING मा.; the mid
    # tokens (मु./न्या.) are never split inside a single honorific.
    glued = "मा. न्या. श्री राममा. मु. न्या. श्री श्याम"
    assert desep_judges(glued) == "मा. न्या. श्री राम, मा. मु. न्या. श्री श्याम"


def test_desep_judges_three_judge_bench():
    glued = "मा. न्या. एकमा. न्या. दुईमा. न्या. तीन"
    assert desep_judges(glued) == "मा. न्या. एक, मा. न्या. दुई, मा. न्या. तीन"


def test_desep_judges_is_idempotent_on_already_separated():
    comma = "मा. न्या. श्री राम, मा. न्या. श्री श्याम"
    assert desep_judges(comma) == comma
    # space-delimited (honorific preceded by whitespace) is NOT glued → left as-is.
    spaced = "मा. न्या. श्री राम मा. न्या. श्री श्याम"
    assert desep_judges(spaced) == spaced


def test_desep_judges_noop_on_single_judge_and_empty():
    assert desep_judges("मा. न्या. श्री एक जना") == "मा. न्या. श्री एक जना"
    # District's spelled-out "माननीय … न्यायाधीश" single judge is untouched (honorific
    # at start, no interior माननीय/मा.).
    assert desep_judges("माननीय जिल्ला न्यायाधीश सीता") == "माननीय जिल्ला न्यायाधीश सीता"
    assert desep_judges("") is None
    assert desep_judges(None) is None


def test_extract_judges_honours_br_then_backstops_glue():
    # <br> is turned into the separator structurally…
    assert extract_judges(_cell("<td>मा. न्या. राम<br>मा. न्या. श्याम</td>")) == (
        "मा. न्या. राम, मा. न्या. श्याम"
    )
    # …and a cell whose judges are run-on with no <br> is still de-separated.
    assert extract_judges(_cell("<td>मा. न्या. राममा. न्या. श्याम</td>")) == (
        "मा. न्या. राम, मा. न्या. श्याम"
    )
    assert extract_judges(_cell("<td>   </td>")) is None
    assert extract_judges(None) is None


def test_desep_judges_keeps_name_ending_in_ma_intact():
    # THE over-split trap: a judge NAME ending in the akshara मा (रमा / …शर्मा) glued
    # to the next honorific. The anchor is मा + a literal PERIOD (मा\.), and a name
    # never carries a period, so only the honorific's मा. is split — the name is whole.
    assert desep_judges("मा. न्या. श्री रमामा. न्या. श्री सीता") == (
        "मा. न्या. श्री रमा, मा. न्या. श्री सीता"
    )
    assert desep_judges("मा. न्या. श्री पूर्णिमामा. न्या. श्री गंगा") == (
        "मा. न्या. श्री पूर्णिमा, मा. न्या. श्री गंगा"
    )


def test_desep_judges_acting_chief_title_soup_not_split_internally():
    # Real acting-chief title "मा.का.मु.मु.न्या." has interior periods, but only its
    # LEADING मा. is an anchor (का./मु./न्या. are not), so a glued entry splits at the
    # boundary and the title stays intact.
    glued = "मा. न्या. श्री राममा.का.मु.मु.न्या. श्री श्याम"
    assert desep_judges(glued) == "मा. न्या. श्री राम, मा.का.मु.मु.न्या. श्री श्याम"


def test_desep_judges_quadruple_glue_and_double_application():
    glued = "मा. न्या. एकमा. न्या. दुईमा. न्या. तीनमा. न्या. चार"
    once = desep_judges(glued)
    assert once == "मा. न्या. एक, मा. न्या. दुई, मा. न्या. तीन, मा. न्या. चार"
    # Idempotent under re-application (the backfill must be safe to re-run).
    assert desep_judges(once) == once


def test_desep_judges_mixed_glued_and_already_spaced():
    # First boundary already space-delimited (clean), second boundary glued.
    mixed = "मा. न्या. श्री एक मा. न्या. श्री दुईमा. न्या. श्री तीन"
    assert desep_judges(mixed) == (
        "मा. न्या. श्री एक मा. न्या. श्री दुई, मा. न्या. श्री तीन"
    )


def test_extract_judges_self_closing_br_and_multiline():
    # Portals emit both <br> and <br/>; both become the separator.
    assert extract_judges(_cell("<td>मा. न्या. राम<br/>मा. न्या. श्याम</td>")) == (
        "मा. न्या. राम, मा. न्या. श्याम"
    )
