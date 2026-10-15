"""Pairing a case's accused with the party rows of its appeal docket.

Every name pair below is a real one from the 2026-10-09 measurement over the 262
published multi-accused cases with a decided appeal.
"""

import pytest

from cases.services.appeal_parties import (
    EXACT,
    NEAR,
    _split_alias,
    name_key,
    pair_accused,
    parties_on_appeal,
)
from casework.court_record import split_alias


def _bind(name, slug=None):
    return {
        "nes_id": f"https://jawafdehi.org/entity/person/{slug or abs(hash(name))}",
        "display_name": name,
        "type": "accused",
    }


def _party(name, side="defendant", nes_id=None):
    return {"name": name, "side": side, "nes_id": nes_id}


GOVERNMENT = _party("अख्तियार दुरुपयोग अनुसन्धान आयोगको प्रतिवेदनले नेपाल सरकार", "plaintiff")


@pytest.mark.parametrize(
    ("court_spelling", "our_spelling"),
    [
        ("प्रकाशचन्द्र भट्ट", "प्रकाश चन्द्र भट्ट"),  # the Supreme Court joins compound names
        ("गङ्गाबहादुर घिमिरे", "गंगा बहादुर घिमिरे"),  # conjunct nasal vs anusvara
        ("मोहन कोइराला", "मोहन कोईराला"),  # vowel length
        ("सुमित चापागाईँ", "सुमित चापागाई"),  # chandrabindu
        ("मन्दिपकुमार श्रेष्‍ठ", "मन्दिप कुमार श्रेष्ठ"),  # zero-width joiner
        ("लुद्रबहादुर के.सी.", "लुद्र बहादुर के.सी."),
        ("कमलबहादुर बस्नेत", "कमल वहादुर वस्नेत"),  # ब/व, the commonest swap in these rows
        ("वेदनिधी बराल", "बेदनिधि बराल"),
        ("ममिता गुरुङ्ग", "ममिता गुरुङ"),  # word-final ङ्ग / ङ
        ("लखनलाल साह", "डा.लखनलाल साह"),  # honorific
        ("हिमलाल ज्ञवाली  १७ जना", "हिमलाल ज्ञवाली"),  # a docket row standing for a group
        ("अनन्तराज सत्याल", "अनन्तराज सत्याल समेत ६४"),
    ],
)
def test_spellings_that_differ_only_in_form_share_a_key(court_spelling, our_spelling):
    assert name_key(court_spelling) == name_key(our_spelling)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("कमल थापा", "कमला थापा"),  # masculine vs feminine
        ("अमरनाथ साह", "अमरनाथ शाह"),  # साह and शाह are different surnames
        ("छविलाल भुषाल", "छविलाल भुसाल"),
        ("वैधनाथ झा", "वैद्यनाथ झा"),
        ("शिवजी मण्डल", "शिवजी मण्डल खत्वे"),
    ],
)
def test_a_real_letter_difference_keeps_the_keys_apart(left, right):
    assert name_key(left) != name_key(right)


def test_an_exact_pair_is_marked_exact():
    ratan = _bind("रतन बहादुर शाही")
    (pair,) = pair_accused([ratan], [GOVERNMENT, _party("रतनबहादुर शाही")])

    assert pair.nes_id == ratan["nes_id"]
    assert pair.party["name"] == "रतनबहादुर शाही"
    assert pair.tier == EXACT


def test_the_legal_name_after_an_alias_marker_pairs_exactly():
    (pair,) = pair_accused(
        [_bind("धन बहादुर तामाङ्ग")], [_party("समिर भन्‍ने धनबहादुर तामाङ्ग")]
    )

    assert pair.tier == EXACT


def test_a_near_spelling_pairs_as_near():
    (pair,) = pair_accused([_bind("धनदेवी छन्त्याल")], [_party("धनदेवी छत्याल (शाही)")])

    assert pair.tier == NEAR
    assert pair.party["name"] == "धनदेवी छत्याल (शाही)"


def test_an_extra_surname_on_the_court_row_is_near_not_exact():
    (pair,) = pair_accused([_bind("शिवजी मण्डल")], [_party("शिवजी मण्डल खत्वे")])

    assert pair.tier == NEAR


def test_an_accused_missing_from_the_appeal_pairs_with_nothing():
    """The CIAA often appeals only some of the accused."""
    pairs = pair_accused(
        [_bind("नेपाल साह"), _bind("संजिव यादव")],
        [GOVERNMENT, _party("नेपाल साह"), _party("प्रमोद कुमार राय")],
    )

    assert [(p.display_name, p.tier) for p in pairs] == [
        ("नेपाल साह", EXACT),
        ("संजिव यादव", None),
    ]


def test_the_government_is_never_paired():
    (pair,) = pair_accused([_bind("नेपाल सरकार")], [GOVERNMENT])

    assert pair.tier is None


def test_an_exact_pair_claims_its_party_before_a_near_one_can():
    sita, sarita = _bind("सिता भट्टराई"), _bind("सरिता भट्टराई")
    pairs = pair_accused([sarita, sita], [_party("सिता भट्टराई")])

    assert [(p.display_name, p.tier) for p in pairs] == [
        ("सरिता भट्टराई", None),
        ("सिता भट्टराई", EXACT),
    ]


def test_two_accused_sharing_one_key_pair_with_nobody():
    """Namesakes on one roster: the docket row cannot say which of them it is."""
    pairs = pair_accused(
        [_bind("राम बहादुर थापा", "a"), _bind("रामबहादुर थापा", "b")],
        [_party("राम बहादुर थापा")],
    )

    assert [p.tier for p in pairs] == [None, None]


def test_a_near_pair_with_a_close_rival_is_dropped():
    pairs = pair_accused(
        [_bind("हरि प्रसाद शर्मा")],
        [_party("हरिप्रसाद शर्मा पौडेल"), _party("हरिप्रसाद शर्मा ढकाल")],
    )

    assert [p.tier for p in pairs] == [None]


@pytest.mark.parametrize("name", [
    "आवास भन्ने आभाश अर्याल",
    "बलराम भन्ने विनि बहादुर भन्ने विनि बहादुर बानिया",
    "कृष्ण प्रसाद यादव",
    "राम भन्ने",
    "भन्ने राम",
])
def test_the_alias_split_agrees_with_the_court_record_reader(name):
    assert _split_alias(name) == split_alias(name)


# --- one docket --------------------------------------------------------------------


def test_an_accused_named_on_the_docket_is_on_the_appeal():
    ratan, nepal = _bind("रतन बहादुर शाही"), _bind("नेपाल साह")

    assert parties_on_appeal([ratan, nepal], [GOVERNMENT, _party("रतनबहादुर शाही")]) == [
        ratan["nes_id"]
    ]


def test_an_accused_shown_in_english_pairs_by_the_nepali_name():
    shashikant = {**_bind("Shashikant Jha"), "name": {"en": "Shashikant Jha", "ne": "शशीकान्त झा"}}

    assert parties_on_appeal([shashikant], [_party("शशीकान्त झा")]) == [shashikant["nes_id"]]


def test_a_near_spelling_is_on_the_appeal():
    buddhi = _bind("बुद्दिराज भट्ट")

    assert parties_on_appeal([buddhi], [_party("बुद्धिराज भट्ट")]) == [buddhi["nes_id"]]


def test_a_row_linked_by_hand_is_trusted_over_the_name():
    """The hand link names someone else; the namesake accused is not paired to it."""
    ratan = _bind("रतन बहादुर शाही")
    linked = "https://jawafdehi.org/entity/person/other-ratan"

    assert parties_on_appeal([ratan], [_party("रतन बहादुर शाही", nes_id=linked)]) == [linked]


def test_an_accused_linked_by_hand_is_not_paired_again():
    ratan = _bind("रतन बहादुर शाही")
    rows = [_party("रतन बहादुर शाही", nes_id=ratan["nes_id"]), _party("रतनबहादुर शाही")]

    assert parties_on_appeal([ratan], rows) == [ratan["nes_id"]]


def test_a_docket_with_no_rows_has_nobody_on_it():
    assert parties_on_appeal([_bind("रतन बहादुर शाही")], []) == []


def test_an_accused_linked_by_hand_does_not_take_another_accuseds_row():
    sita, sita_b = _bind("सिता भट्टराई", "sita"), _bind("सिता भट्टराइ", "sita-b")
    rows = [_party("सीता भट्टराई", nes_id=sita["nes_id"]), _party("सिता भट्टराई")]

    assert parties_on_appeal([sita, sita_b], rows) == sorted([sita["nes_id"], sita_b["nes_id"]])
