import pytest
from casework.common.grounding import (
    evidence_found, has_word, is_readable_devanagari, is_teaser, location_quote_problem,
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


@pytest.mark.parametrize("quote", [
    "बाँके जिल्लाको स्वतन्त्र उपभोक्ता समितिले खजुरामा ठेक्का लियो",
    "प्रतिवादी हरि बस्नेतले बाँके जिल्ला खजुरामा रकम बुझे",
    "बाँके जिल्ला खजुरामा अस्थायी शिविर राखी रकम बुझियो",
    "संसदको स्थायी समितिले बाँके जिल्ला खजुराको ठेक्का छानबिन गर्‍यो",
])
def test_a_marker_inside_another_word_does_not_reject_the_quote(quote):
    assert location_quote_problem(quote, "बाँके", quote) == ""


@pytest.mark.parametrize("quote,marker", [
    ("जिल्ला कालिकोट स्थायी घर भई बाँके जिल्ला खजुरामा", "स्थायी"),
    ("हाल बाँके जिल्ला खजुरामा बस्ने प्रतिवादी", "बस्ने"),
    ("बाँके जिल्ला खजुरा वतन भएका प्रतिवादी", "वतन"),
    ("आयोगको कार्यालय, बाँकेमा उजुरी पर्‍यो", "आयोगको कार्यालय"),
])
def test_a_marker_as_its_own_word_still_rejects(quote, marker):
    assert repr(marker) in location_quote_problem(quote, "बाँके", quote)


def test_has_word_allows_a_joined_case_ending_but_not_a_longer_word():
    assert has_word("बाँके", "बाँकेमा") and has_word("बाँके", "जिल्ला बाँके।")
    assert not has_word("बाँके", "बाँकेपुर") and not has_word("बाँके", "अबाँके")


#: 077-CR-0004's shape: the event place first, other people's addresses 200 chars on.
FAR_ADDRESS = ("दाङ जिल्ला, घोराही उप-महानगरपालिका वडा नं. १४ मा रहेको वडा प्रहरी कार्यालय घोराहीको "
               "कार्यालय प्रमुख प्रहरी निरीक्षक दिनेश रिमाललाई निजको कार्यकक्षभित्र आई मिति "
               "२०७७।०३।२७ गते १७:०० बजेको समयमा जिल्ला दाङ घोराही उप-महानगरपालिका वडा नं. "
               "१८ बस्ने गोविन्द शाहले घुस दिएको अवस्थामा नियन्त्रणमा लिइयो")


def test_an_address_far_from_the_place_does_not_reject_it():
    place = "दाङ जिल्ला, घोराही उप-महानगरपालिका वडा नं. १४"
    assert location_quote_problem(FAR_ADDRESS, place, FAR_ADDRESS) == ""


def test_an_address_right_after_the_place_still_rejects_it():
    quote = "जिल्ला कालिकोट, पचालझरना गाउँपालिका वडा नं. ५ बस्ने वर्ष ४५ को प्रतिवादी"
    assert "'बस्ने'" in location_quote_problem(quote, "जिल्ला कालिकोट", quote)


def test_a_home_address_lead_in_rejects_the_place():
    quote = "जिल्ला गोरखा, आरुघाट गाउँपालिका वडा नं.९ आरुघाट बजार घर भई खोटाङमा कार्यरत"
    assert "'घर भई'" in location_quote_problem(quote, "जिल्ला गोरखा", quote)


def test_a_place_differing_from_its_quote_only_by_punctuation_is_in_it():
    quote = "खानतलासी मुचुल्का:-जिल्ला खोटाङ, दिक्तेल रुपाकोट मझुवागढी नगरपालिका वडा नं.१ स्थितमा रहेको निवास"
    assert location_quote_problem(quote, "जिल्ला खोटाङ दिक्तेल रुपाकोट मझुवागढी नगरपालिका", quote) == ""


def test_a_district_joined_to_jilla_is_its_word():
    assert has_word("दाङ", "दाङजिल्ला, घोराही")
