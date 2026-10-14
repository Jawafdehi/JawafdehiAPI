"""Pairing a case's accused with the party rows of its appeal docket.

Every name pair below is a real one from the 2026-10-09 measurement over the 262
published multi-accused cases with a decided appeal.
"""

import csv

import pytest

from casework.appeal_parties import (
    EXACT,
    NEAR,
    apply_rows,
    build_review,
    name_key,
    pair_accused,
    read_rows,
    review_rows,
    write_rows,
)


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


def test_a_near_spelling_pairs_for_review_only():
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


# --- the review file and the write ---------------------------------------------

APPEAL = "https://jawafdehi.org/courtcase/supreme/081-cr-1949"


def _case(*accused):
    return {"slug": "khaulachaur-field-fake-survey-book-079-cr-0088", "state": "PUBLISHED",
            "entities": list(accused)}


def test_review_rows_prefill_bind_only_for_exact_pairs():
    ratan, dhan = _bind("रतन बहादुर शाही"), _bind("धनदेवी छन्त्याल")
    parties = [GOVERNMENT, _party("रतन बहादुर शाही"), _party("धनदेवी छत्याल (शाही)")]
    rows = review_rows(_case(ratan, dhan), APPEAL, "AFFIRMED", parties)

    by_name = {r["accused_name"]: r for r in rows}
    assert by_name["रतन बहादुर शाही"]["decision"] == "bind"
    assert by_name["धनदेवी छन्त्याल"]["decision"] == ""
    assert by_name["धनदेवी छन्त्याल"]["tier"] == NEAR
    assert by_name["रतन बहादुर शाही"]["court"] == "supreme"
    assert by_name["रतन बहादुर शाही"]["case_number"] == "081-CR-1949"


def test_review_rows_list_a_party_no_accused_matched():
    rows = review_rows(_case(_bind("नेपाल साह")), APPEAL, "AFFIRMED",
                       [GOVERNMENT, _party("नेपाल साह"), _party("जयमङ्गल प्रसाद")])

    orphan = [r for r in rows if not r["accused_nes_id"]]
    assert [r["party_name"] for r in orphan] == ["जयमङ्गल प्रसाद"]
    assert orphan[0]["decision"] == ""


def test_review_rows_skip_a_party_already_resolved_to_the_same_entity():
    ratan = _bind("रतन बहादुर शाही")
    (row,) = review_rows(_case(ratan), APPEAL, None,
                         [_party("रतन बहादुर शाही", nes_id=ratan["nes_id"])])

    assert row["decision"] == "already"


def test_review_rows_refuse_a_party_resolved_to_someone_else():
    (row,) = review_rows(_case(_bind("रतन बहादुर शाही")), APPEAL, None,
                         [_party("रतन बहादुर शाही",
                                 nes_id="https://jawafdehi.org/entity/person/other")])

    assert row["decision"] == "conflict"


def test_rows_survive_a_round_trip_through_the_review_file(tmp_path):
    rows = review_rows(_case(_bind("रतन बहादुर शाही")), APPEAL, "AFFIRMED",
                       [_party("रतन बहादुर शाही")])
    path = tmp_path / "review.csv"
    write_rows(path, rows)

    assert read_rows(path) == rows
    with open(path, encoding="utf-8") as fh:
        assert "decision" in next(csv.reader(fh))


class _FakeApi:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def resolve_court_parties(self, items):
        self.calls.append(items)
        return self.response or {
            "resolved": len(items), "unmatched": 0, "failed": 0,
            "results": [{"index": i, "status": "resolved", "matched": 1}
                        for i in range(len(items))],
        }


def _row(decision, name="रतन बहादुर शाही"):
    return {"slug": "s", "court": "supreme", "case_number": "081-CR-1949",
            "party_name": name, "party_side": "defendant",
            "accused_nes_id": "https://jawafdehi.org/entity/person/ratan",
            "decision": decision}


def test_apply_sends_only_the_rows_a_human_marked_bind():
    api = _FakeApi()
    stats = apply_rows(api, [_row("bind"), _row(""), _row("skip"), _row("conflict"),
                             _row("already")])

    assert api.calls == [[{
        "court": "supreme", "case_number": "081-CR-1949", "side": "defendant",
        "name": "रतन बहादुर शाही",
        "nes_id": "https://jawafdehi.org/entity/person/ratan",
    }]]
    assert stats["resolved"] == 1


def test_apply_reports_a_row_the_server_could_not_find():
    api = _FakeApi({"resolved": 0, "unmatched": 1, "failed": 0,
                    "results": [{"index": 0, "status": "unmatched", "matched": 0}]})
    stats = apply_rows(api, [_row("bind")])

    assert stats["unmatched"] == 1
    assert stats["unmatched_rows"] == [("s", "रतन बहादुर शाही")]


def test_apply_with_nothing_marked_makes_no_request():
    api = _FakeApi()
    apply_rows(api, [_row(""), _row("skip")])

    assert api.calls == []


def test_apply_sends_one_item_when_two_rows_bind_the_same_party_to_the_same_entity():
    """Two cases citing one appeal docket produce the same write twice."""
    api = _FakeApi()
    apply_rows(api, [_row("bind"), {**_row("bind"), "slug": "other"}])

    assert len(api.calls[0]) == 1


# --- walking the cases -------------------------------------------------------------

class _ListingApi:
    def __init__(self, cases, parties):
        self.cases, self.parties, self.party_reads = cases, parties, []

    def iter_cases(self):
        yield from self.cases

    def get_court_case_entities(self, court, number):
        self.party_reads.append((court, number))
        return self.parties[(court, number)]


def _appealed(slug, *accused):
    return {"slug": slug, "state": "PUBLISHED", "entities": list(accused),
            "dates": {"stages": [{"stage": "appeal", "courtcase_iri": APPEAL}]},
            "appeal_verdicts": {APPEAL: "AFFIRMED"}}


def test_a_case_the_list_returns_twice_is_reviewed_once():
    case = _appealed("s", _bind("रतन बहादुर शाही"))
    api = _ListingApi([case, case], {("supreme", "081-CR-1949"): [_party("रतन बहादुर शाही")]})

    rows = build_review(api, [])

    assert len(rows) == 1


def test_two_cases_binding_one_party_row_to_different_entities_is_a_conflict():
    """A party row holds one nes_id: duplicate entities on two cases citing one
    appeal would overwrite each other."""
    one = _appealed("a", _bind("उमेश प्रसाद ढकाल", "umesha-prasada-dhakala"))
    two = _appealed("b", _bind("उमेश प्रसाद ढकाल", "umesh-prasad-dhakal"))
    api = _ListingApi([one, two], {("supreme", "081-CR-1949"): [_party("उमेश प्रसाद ढकाल")]})

    rows = build_review(api, [])

    assert [r["decision"] for r in rows] == ["conflict", "conflict"]


def test_two_cases_binding_one_party_row_to_the_same_entity_both_stay_bind():
    ratan = _bind("रतन बहादुर शाही")
    api = _ListingApi([_appealed("a", ratan), _appealed("b", ratan)],
                      {("supreme", "081-CR-1949"): [_party("रतन बहादुर शाही")]})

    assert [r["decision"] for r in build_review(api, [])] == ["bind", "bind"]
