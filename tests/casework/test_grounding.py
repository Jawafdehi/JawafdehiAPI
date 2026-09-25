import pytest
from casework.common.grounding import (
    evidence_found, is_readable_devanagari, is_teaser, location_quote_problem,
    normalise_for_match,
)

ORDER = ("**अध्यक्ष माननीय न्यायाधीश**\nजिल्ला कालिकोट स्थायी घर भई हाल बस्ने जयराज\n"
         "मुद्धा:- भ्रष्टाचार\n"
         "5. **खानतलासी मुचुल्का:** जिल्ला बाँके, खजुरा गाँउपालिका वडा नं.४ स्थित "
         "पूर्वमा बाटो भएको घरमा रकम बरामद भयो ।\n"
         "आयोगको कार्यालय, कोहलपुरबाट खटिएको टोली ।\n")
CAP = ORDER.index("मुद्धा:") + len("मुद्धा:")


def test_normalisation_folds_markdown_zero_width_chandrabindu_and_space():
    assert normalise_for_match("**गाउँ**‍  पालिका") == normalise_for_match("गाउं पालिका")


def test_evidence_found_despite_bold_markers_in_the_source():
    assert evidence_found("खानतलासी मुचुल्का: जिल्ला बाँके", ORDER)


def test_a_paraphrase_is_not_evidence():
    assert not evidence_found("बाँके जिल्लामा रकम बरामद गरियो", ORDER)


def test_a_too_short_quote_is_not_evidence():
    assert not evidence_found("बाँके", ORDER)


def test_the_seizure_record_grounds_banke():
    ev = "जिल्ला बाँके, खजुरा गाँउपालिका वडा नं.४ स्थित पूर्वमा बाटो"
    assert location_quote_problem(ev, "बाँके", ORDER, CAP) == ""


def test_a_caption_home_address_is_refused():
    ev = "जिल्ला कालिकोट स्थायी घर भई हाल बस्ने"
    assert location_quote_problem(ev, "कालिकोट", ORDER, CAP) != ""


def test_the_ciaa_regional_office_is_refused():
    ev = "आयोगको कार्यालय, कोहलपुरबाट खटिएको टोली"
    assert "आयोगको कार्यालय" in location_quote_problem(ev, "कोहलपुर", ORDER, CAP)


def test_the_place_must_be_inside_its_own_quote():
    ev = "जिल्ला बाँके, खजुरा गाँउपालिका वडा नं.४ स्थित पूर्वमा बाटो"
    assert location_quote_problem(ev, "सुर्खेत", ORDER, CAP) != ""


def test_preeti_ascii_is_not_readable():
    assert not is_readable_devanagari("sf/f]af/ ug{ ;DalGw ljz]if cbfnt " * 50)
    assert is_readable_devanagari(ORDER * 5)


def test_teaser_detection():
    assert is_teaser("अनुसन्धान हुँदा। क्रमश...\n(नोट: थप जानकारीको लागि डाउनलोडमा थिच्नुहोला)")
    assert not is_teaser("क" * 1_000)


REGIONAL_OFFICE_QUOTES = [
    ("अख्तियार दुरुपयोग अनुसन्धान आयोग मध्यमाञ्चल क्षेत्रीय कार्यालय महोत्तरीको च.नं.946", "महोत्तरी"),
    ("अख्तियार दुरुपयोग अनुसन्धान आयोगको क्षेत्रीय कार्यालय सुर्खेतमा उजुरी परेको", "सुर्खेत"),
    ("गरिदिएको कागज अ.दु.आयोग म.प.क्षेत्रीय कार्यालय सुर्खेत सम्पर्क कार्यालय", "सुर्खेत"),
]


@pytest.mark.parametrize("ev,place", REGIONAL_OFFICE_QUOTES)
def test_every_ciaa_regional_office_form_is_refused(ev, place):
    text = f"मुद्दा: भ्रष्टाचार\n{ev} ।\n"
    assert "क्षेत्रीय कार्यालय" in location_quote_problem(ev, place, text, 0)


def test_a_permanent_home_vatan_is_refused():
    ev = "निजहरुको वतन बाँके जिल्लाको खजुरा गाउँपालिका वडा नं. २ मा"
    text = f"मुद्दा: भ्रष्टाचार\n{ev} देखिन्छ ।\n"
    assert "वतन" in location_quote_problem(ev, "बाँके", text, 0)
