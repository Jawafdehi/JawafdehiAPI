import json
from pathlib import Path

import pytest
from casework.location_gazetteer import Gazetteer, load_gazetteer, place_key, resolve_locations

E = "https://jawafdehi.org/entity/"


def district(stem, code, ne, en, alt=None):
    doc = {"@id": f"{E}location/district/{stem}-np{code}", "name": {"en": en}}
    if ne:
        doc["name"]["ne"] = ne
    if alt:
        doc["alternateName"] = alt
    return doc


def unit(slug, ne, en, parent):
    return {"@id": f"{E}location/localunit/{slug}", "name": {"ne": ne, "en": en},
            "containedInPlace": {"@id": parent}}


BANKE = f"{E}location/district/banke-np0557"
KTM = f"{E}location/district/kathmandu-np0327"
SALYAN = f"{E}location/district/salyan-np0653"
DANG = f"{E}location/district/dang-np0556"
LALIT = f"{E}location/district/lalitpur-np0325"
KAILALI = f"{E}location/district/kailali-np0771"
NP_E = f"{E}location/district/nawalparasi-east-np0447"
NP_W = f"{E}location/district/nawalparasi-west-np0547"
RK_E = f"{E}location/district/rukum-east-np0552"
RK_W = f"{E}location/district/rukum-west-np0652"

DISTRICTS = [
    district("banke", "0557", "बाँके", "Banke"),
    district("kathmandu", "0327", "काठमाडौँ", "Kathmandu", ["Kathmandu District"]),
    district("salyan", "0653", "सल्यान", "Salyan"),
    district("dang", "0556", "दाङ", "Dang"),
    district("lalitpur", "0325", "ललितपुर", "Lalitpur"),
    district("kailali", "0771", "कैलाली", "Kailali"),
    district("nawalparasi-east", "0447", "नवलपरासी (बर्दघाट सुस्तापूर्व)", "Nawalparasi East"),
    district("nawalparasi-west", "0547", "नवलपरासी", "Nawalparasi West"),
    district("rukum-east", "0552", None, "Rukum East"),
    district("rukum-west", "0652", "रुकुम (पश्चिम)", "Rukum West"),
]
UNITS = [
    unit("khajura-gaunpalika-50001", "खजुरा गाउँपालिका", "Khajura Rural Municipality", BANKE),
    unit("kathmandu-metropolitian-city-30608", "काठमाडौं महानगरपालिका", "Kathmandu Metropolitan City", KTM),
    unit("triveni-salyan-1", "त्रिवेणी गाउँपालिका", "Triveni Rural Municipality", SALYAN),
    unit("triveni-other-2", "त्रिवेणी गाउँपालिका", "Triveni Rural Municipality", DANG),
    unit("godawari-lalitpur-30803", "गोदावरी नगरपालिका", "Godawari Municipality", LALIT),
    unit("godawari-kailali-70001", "गोदावरी नगरपालिका", "Godawari Municipality", KAILALI),
    # Two DIFFERENT municipalities that happen to share a name, both inside the
    # SAME claimed district -- for the "several named municipalities" reason,
    # distinct from the "sits in another district" one above.
    unit("sunuwa-gaunpalika-1", "सुनुवा गाउँपालिका", "Sunuwa Rural Municipality", BANKE),
    unit("sunuwa-gaunpalika-2", "सुनुवा गाउँपालिका", "Sunuwa Rural Municipality", BANKE),
]


@pytest.fixture
def gaz():
    return Gazetteer(DISTRICTS, UNITS)


def test_the_seizure_place_resolves_to_banke_and_khajura(gaz):
    d = gaz.resolve("जिल्ला बाँके, खजुरा गाँउपालिका वडा नं.४", "बाँके")
    assert (d.district, d.localunit) == (BANKE, f"{E}location/localunit/khajura-gaunpalika-50001")


def test_kathmandu_spelling_variants_and_chandrabindu(gaz):
    for form in ("काठमाडौं", "काठमाण्डौ", "काठमाडौँ जिल्ला", "Kathmandu District"):
        assert gaz.district_for(form) == KTM, form


def test_a_town_alone_finds_its_district_through_the_municipality(gaz):
    d = gaz.resolve("खजुरा गा.पा. वडा नं. ४", "")
    assert d.district == BANKE


def test_a_shared_municipality_name_binds_only_inside_the_claimed_district(gaz):
    d = gaz.resolve("सल्यान जिल्ला, त्रिवेणी गाउँपालिका वडा नं. १", "सल्यान")
    assert d.localunit == f"{E}location/localunit/triveni-salyan-1"


def test_a_shared_municipality_with_no_district_named_is_not_guessed(gaz):
    d = gaz.resolve("गोदावरी नगरपालिका", "")
    assert d.district is None and "no single district" in d.reason


def test_old_nawalparasi_and_rukum_are_never_guessed(gaz):
    assert gaz.district_for("नवलपरासी") is None
    assert gaz.resolve("नवलपरासी जिल्ला", "नवलपरासी").district is None
    assert gaz.resolve("रुकुम जिल्ला", "रुकुम").district is None


def test_split_district_names_that_say_which_half_resolve(gaz):
    assert gaz.district_for("नवलपरासी (बर्दघाट सुस्तापूर्व)") == NP_E
    assert gaz.district_for("रुकुम पूर्व") == RK_E


def test_a_claim_the_text_does_not_support_is_refused(gaz):
    d = gaz.resolve("जिल्ला बाँके, खजुरा गाउँपालिका", "दाङ")
    assert d.district is None


def test_a_municipality_in_another_district_is_not_bound(gaz):
    d = gaz.resolve("दाङ जिल्ला, खजुरा गाउँपालिका", "दाङ")
    assert d.district == DANG and d.localunit is None and "another district" in d.reason


def test_a_district_name_inside_a_longer_word_is_not_a_match(gaz):
    assert gaz.districts_in("अबाँके") == set()


def test_twin_redirect(gaz):
    assert gaz.redirect(f"{E}location/kathamandau-9a0ddc", "काठमाण्डौ") == KTM
    assert gaz.redirect(f"{E}kalikot/kalikot-0162", "सल्यान") == SALYAN
    assert gaz.redirect(KTM, "whatever") == KTM
    assert gaz.redirect(f"{E}location/narayani-aspatala-f50002", "नारायणी अस्पताल") is None


def test_a_disambiguated_compound_does_not_leak_the_other_half(gaz):
    assert gaz.districts_in("रुकुम पश्चिम जिल्ला") == {RK_W}


def test_a_self_disambiguating_text_overrides_a_different_half_claim(gaz):
    d = gaz.resolve("रुकुम पश्चिम जिल्ला अदालतमा", "रुकुम पूर्व")
    assert d.district is None


def test_nawalparasi_compound_forms_resolve_to_their_own_half(gaz):
    assert gaz.resolve("नवलपरासी पश्चिम", "नवलपरासी पूर्व").district is None
    assert gaz.resolve("नवलपरासी पश्चिम", "नवलपरासी पश्चिम").district == NP_W


def test_two_same_named_municipalities_in_the_claimed_district_are_not_guessed(gaz):
    d = gaz.resolve("बाँके जिल्ला, सुनुवा गाउँपालिका वडा नं. २", "बाँके")
    assert d.district == BANKE and d.localunit is None
    assert "several named municipalities" in d.reason


def test_place_key_expands_abbreviations():
    assert place_key("बुटवल उ.म.न.पा.") == place_key("बुटवल उपमहानगरपालिका")


class _PagedApi:
    def __init__(self, districts, units):
        self.pages = {"location/district": districts, "location/localunit": units}

    def get(self, path, params=None, timeout=60):
        assert path == "/entities"
        rows = self.pages[params["entity_prefix"]]
        o, n = params["offset"], params["limit"]
        return {"entities": rows[o:o + n], "total": len(rows)}


def test_load_refuses_anything_but_77_districts():
    with pytest.raises(RuntimeError, match="77"):
        load_gazetteer(_PagedApi(DISTRICTS, UNITS))


class _StubSearchApi:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.calls: list = []

    def search_entities(self, query):
        self.calls.append(query)
        return self.rows


SEIZURE_PLACE = "जिल्ला बाँके, खजुरा गाँउपालिका वडा नं.४"
SEIZURE_TEXT = f"जफत गरिएको सामान {SEIZURE_PLACE} बाट बरामद भएको हो ।"
KHAJURA = f"{E}location/localunit/khajura-gaunpalika-50001"


def test_a_grounded_answer_binds_district_and_municipality(gaz):
    answers = [{"place_as_written": SEIZURE_PLACE, "district": "बाँके",
                "evidence": f"{SEIZURE_PLACE} बाट बरामद भएको हो", "notes": "seizure site"}]
    binds, rejected = resolve_locations(_StubSearchApi(), gaz, answers, SEIZURE_TEXT, 0)
    assert rejected == []
    assert [(b.nes_id, b.via) for b in binds] == [(BANKE, "gazetteer"), (KHAJURA, "gazetteer")]
    assert binds[0].notes == "seizure site"


def test_an_answer_quoting_the_caption_is_rejected(gaz):
    text = ("**अध्यक्ष माननीय न्यायाधीश**\nजिल्ला कालिकोट स्थायी घर भई हाल बस्ने जयराज\n"
            "मुद्धा:- भ्रष्टाचार\n"
            f"5. **खानतलासी मुचुल्का:** {SEIZURE_PLACE} स्थित पूर्वमा बाटो भएको घरमा रकम बरामद भयो ।\n"
            "आयोगको कार्यालय, कोहलपुरबाट खटिएको टोली ।\n")
    cap = text.index("मुद्धा:") + len("मुद्धा:")
    answers = [{"place_as_written": "कालिकोट", "district": "",
                "evidence": "जिल्ला कालिकोट स्थायी घर भई हाल बस्ने", "notes": ""}]
    binds, rejected = resolve_locations(_StubSearchApi(), gaz, answers, text, cap)
    assert binds == []
    assert "caption" in rejected[0]["reason"]


def test_two_answers_naming_the_same_district_produce_one_bind(gaz):
    text = "यो घटना बाँके जिल्लाको हो । अर्को विवरण पनि बाँके जिल्लामा भएको हो ।"
    answers = [
        {"place_as_written": "बाँके जिल्ला", "district": "बाँके",
         "evidence": "यो घटना बाँके जिल्लाको हो", "notes": "first"},
        {"place_as_written": "बाँके जिल्ला", "district": "बाँके",
         "evidence": "अर्को विवरण पनि बाँके जिल्लामा भएको हो", "notes": "second"},
    ]
    binds, rejected = resolve_locations(_StubSearchApi(), gaz, answers, text, 0)
    assert rejected == []
    assert [b.nes_id for b in binds] == [BANKE]
    assert binds[0].notes == "first"


def test_a_twin_spelling_redirects_through_the_english_title(gaz):
    text = "बरामद सामान काठमांडू बाट ल्याइएको थियो ।"
    answers = [{"place_as_written": "काठमांडू", "district": "",
                "evidence": "बरामद सामान काठमांडू बाट ल्याइएको थियो", "notes": ""}]
    api = _StubSearchApi([{"id": f"{E}location/kathmandu-twin-1",
                            "title": {"ne": "काठमांडू", "en": "Kathmandu"}}])
    binds, rejected = resolve_locations(api, gaz, answers, text, 0)
    assert rejected == []
    assert [(b.nes_id, b.via) for b in binds] == [(KTM, "nes-redirect")]
    assert api.calls == ["काठमांडू"]


def test_a_twin_spelling_with_no_place_candidate_is_rejected(gaz):
    text = "बरामद सामान काठमांडू बाट ल्याइएको थियो ।"
    answers = [{"place_as_written": "काठमांडू", "district": "",
                "evidence": "बरामद सामान काठमांडू बाट ल्याइएको थियो", "notes": ""}]
    api = _StubSearchApi([{"id": f"{E}organization/kathmandu-office", "title": {"ne": "काठमांडू"}}])
    binds, rejected = resolve_locations(api, gaz, answers, text, 0)
    assert binds == []
    assert rejected[0]["reason"] == "NES match is not a district or municipality"


def test_a_gazetteer_resolved_answer_makes_no_search_call(gaz):
    answers = [{"place_as_written": SEIZURE_PLACE, "district": "बाँके",
                "evidence": f"{SEIZURE_PLACE} बाट बरामद भएको हो", "notes": ""}]
    api = _StubSearchApi()
    resolve_locations(api, gaz, answers, SEIZURE_TEXT, 0)
    assert api.calls == []


def test_a_municipality_redirect_binds_its_parent_district_first(gaz):
    text = "बरामद सामान खजुरागाउँपालिका बाट ल्याइएको थियो ।"
    answers = [{"place_as_written": "खजुरागाउँपालिका", "district": "",
                "evidence": "बरामद सामान खजुरागाउँपालिका बाट ल्याइएको थियो", "notes": ""}]
    api = _StubSearchApi([{"id": f"{E}location/khajura-twin",
                            "title": {"ne": "खजुरागाउँपालिका", "en": "Khajura Rural Municipality"}}])
    binds, rejected = resolve_locations(api, gaz, answers, text, 0)
    assert rejected == []
    assert [(b.nes_id, b.via) for b in binds] == [(BANKE, "nes-redirect"), (KHAJURA, "nes-redirect")]


def test_a_municipality_redirect_with_no_known_parent_is_rejected(gaz):
    text = "बरामद सामान अज्ञातपालिका बाट ल्याइएको थियो ।"
    answers = [{"place_as_written": "अज्ञातपालिका", "district": "",
                "evidence": "बरामद सामान अज्ञातपालिका बाट ल्याइएको थियो", "notes": ""}]
    orphan = {"@id": f"{E}location/localunit/unknown-unit-99999",
              "name": {"ne": "अज्ञात नगरपालिका", "en": "Unknown Municipality"}}
    gaz = Gazetteer(DISTRICTS, UNITS + [orphan])
    api = _StubSearchApi([{"id": f"{E}location/unknown-twin",
                            "title": {"ne": "अज्ञातपालिका", "en": "Unknown Municipality"}}])
    binds, rejected = resolve_locations(api, gaz, answers, text, 0)
    assert binds == []
    assert rejected[0]["reason"] == "municipality has no known district in the gazetteer"


def test_notes_are_capped_at_200_chars(gaz):
    answers = [{"place_as_written": SEIZURE_PLACE, "district": "बाँके",
                "evidence": f"{SEIZURE_PLACE} बाट बरामद भएको हो", "notes": "क" * 250}]
    binds, rejected = resolve_locations(_StubSearchApi(), gaz, answers, SEIZURE_TEXT, 0)
    assert rejected == []
    assert binds and all(len(b.notes) == 200 for b in binds)


def test_a_grounding_failure_is_tagged_grounding(gaz):
    answers = [{"place_as_written": "बाँके", "district": "बाँके",
                "evidence": "यो वाक्य पाठमा कतै छैन, बाँके।", "notes": ""}]
    _binds, rejected = resolve_locations(_StubSearchApi(), gaz, answers, SEIZURE_TEXT, 0)
    assert [(r["stage"], r["reason"]) for r in rejected] == [
        ("grounding", "evidence not found in the source")]


def test_an_unresolved_grounded_place_is_tagged_resolution_with_the_gazetteer_reason(gaz):
    text = "रकम जिल्ला सुर्खेतमा बरामद भएको थियो ।"
    answers = [{"place_as_written": "जिल्ला सुर्खेत", "district": "सुर्खेत",
                "evidence": "रकम जिल्ला सुर्खेतमा बरामद भएको थियो", "notes": ""}]
    _binds, rejected = resolve_locations(_StubSearchApi(), gaz, answers, text, 0)
    assert [(r["stage"], r["reason"]) for r in rejected] == [
        ("resolution", "NES match is not a district or municipality")]
    assert rejected[0]["gazetteer_reason"] == "no single district in the place as written"


def test_a_municipality_refused_beside_its_bound_district_is_a_resolution_row(gaz):
    place = "जिल्ला बाँके, त्रिवेणी गाउँपालिका"
    text = f"घटना {place} मा भएको हो ।"
    answers = [{"place_as_written": place, "district": "बाँके",
                "evidence": f"घटना {place} मा भएको हो", "notes": ""}]
    binds, rejected = resolve_locations(_StubSearchApi(), gaz, answers, text, 0)
    assert [b.nes_id for b in binds] == [BANKE]
    assert [(r["stage"], r["reason"]) for r in rejected] == [
        ("resolution", "a named municipality sits in another district; not bound")]


# --- The real NES snapshot (2026-09-25): all 77 districts, the municipalities these tests name. ---

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "nes_locations_2026-09-25.json")
                      .read_text(encoding="utf-8"))


@pytest.fixture
def real_gaz():
    return Gazetteer(SNAPSHOT["districts"], SNAPSHOT["localunits"])


def _search_rows(*stems):
    """Snapshot rows as `search_entities` returns them: `{id, title: {ne, en}}`, snapshot order."""
    rows = []
    for row in SNAPSHOT["districts"] + SNAPSHOT["localunits"]:
        if row["@id"].rsplit("/", 1)[-1] in stems:
            rows.append({"id": row["@id"], "title": row["name"]})
    return rows


def _one_answer(place, claim):
    text = f"रकम {place} मा बरामद भएको थियो ।"
    return text, [{"place_as_written": place, "district": claim,
                   "evidence": f"रकम {place} मा बरामद भएको थियो", "notes": ""}]


GAZETTEER_REFUSALS = [
    ("नवलपरासी", "", ("nawalparasi-east-np0447", "nawalparasi-west-np0547"),
     "no single district in the place as written"),
    ("मुसिकोट नगरपालिका", "रुकुम", ("musikot-municipality-50404", "musikot-municipality-60804"),
     "no single district in the place as written"),
    ("मादी गाउँपालिका", "", ("madi-gaunpalika-40501", "madi-gaunpalika-50205"),
     "no single district in the place as written"),
    ("सुनकोशी गाउँपालिका", "",
     ("sunkoshi-gaunpalika-10407", "sunkoshi-gaunpalika-30212", "sunkoshi-gaunpalika-31106"),
     "no single district in the place as written"),
    ("खजुरा गाउँपालिका", "बर्दिया", ("khajura-gaunpalika-51104",),
     "the place as written does not name the claimed district"),
    ("बर्दिया", "बाँके", ("bardiya-np0558",),
     "the place as written does not name the claimed district"),
]


@pytest.mark.parametrize("place,claim,stems,reason", GAZETTEER_REFUSALS,
                         ids=[row[0] for row in GAZETTEER_REFUSALS])
def test_a_gazetteer_refusal_is_review_never_an_nes_bind(real_gaz, place, claim, stems, reason):
    text, answers = _one_answer(place, claim)
    api = _StubSearchApi(_search_rows(*stems))
    binds, rejected = resolve_locations(api, real_gaz, answers, text, 0)
    assert binds == []
    assert [(r["stage"], r["reason"]) for r in rejected] == [("resolution", reason)]
    assert api.calls == []


def test_a_coded_candidate_the_gazetteer_did_not_match_is_not_bound(gaz):
    text = "बरामद सामान खजुरागाउँपालिका बाट ल्याइएको थियो ।"
    answers = [{"place_as_written": "खजुरागाउँपालिका", "district": "",
                "evidence": "बरामद सामान खजुरागाउँपालिका बाट ल्याइएको थियो", "notes": ""}]
    api = _StubSearchApi([{"id": KHAJURA, "title": {"ne": "खजुरागाउँपालिका"}},
                          {"id": BANKE, "title": {"ne": "खजुरागाउँपालिका"}}])
    binds, rejected = resolve_locations(api, gaz, answers, text, 0)
    assert binds == []
    assert rejected[0]["reason"] == "NES match is not a district or municipality"


def test_twins_that_redirect_to_different_districts_go_to_review(gaz):
    text = "बरामद सामान काठमांडू बाट ल्याइएको थियो ।"
    answers = [{"place_as_written": "काठमांडू", "district": "",
                "evidence": "बरामद सामान काठमांडू बाट ल्याइएको थियो", "notes": ""}]
    api = _StubSearchApi([
        {"id": f"{E}location/kathmandu-twin-1", "title": {"ne": "काठमांडू", "en": "Kathmandu"}},
        {"id": f"{E}location/kathmandu-twin-2", "title": {"ne": "काठमांडू", "en": "Lalitpur"}},
    ])
    binds, rejected = resolve_locations(api, gaz, answers, text, 0)
    assert binds == []
    assert [(r["stage"], r["reason"]) for r in rejected] == [("resolution", "NES twins disagree")]


def test_twins_that_agree_on_one_district_still_bind(gaz):
    text = "बरामद सामान काठमांडू बाट ल्याइएको थियो ।"
    answers = [{"place_as_written": "काठमांडू", "district": "",
                "evidence": "बरामद सामान काठमांडू बाट ल्याइएको थियो", "notes": ""}]
    api = _StubSearchApi([
        {"id": f"{E}location/kathmandu-twin-1", "title": {"ne": "काठमांडू", "en": "Kathmandu"}},
        {"id": f"{E}location/kathmandu-twin-2", "title": {"ne": "काठमांडू", "en": "Kathmandu District"}},
    ])
    binds, rejected = resolve_locations(api, gaz, answers, text, 0)
    assert rejected == []
    assert [(b.nes_id, b.via) for b in binds] == [(KTM, "nes-redirect")]


KV = f"{E}location/district/"


@pytest.mark.parametrize("place,district,localunit", [
    ("का.जि. वडा नं. ४", f"{KV}kathmandu-np0327", None),
    ("का.म.न.पा. वडा नं. ४", f"{KV}kathmandu-np0327", None),
    ("ल.पु.उ.म.न.पा. वडा नं. ४", f"{KV}lalitpur-np0325", None),
])
def test_kathmandu_valley_abbreviations_resolve_on_the_real_snapshot(real_gaz, place, district, localunit):
    d = real_gaz.resolve(place, "")
    assert (d.district, d.localunit) == (district, localunit)


def test_valley_abbreviations_expand_before_their_shorter_suffixes():
    assert place_key("का.म.न.पा.") == "काठमाडौं महानगरपालिका"
    assert place_key("ल.पु.उ.म.न.पा.") == "ललितपुर उपमहानगरपालिका"
    assert place_key("का.जि. वडा") == "काठमाडौं जिल्ला वडा"


def test_bhanaupa_abbreviation_not_expanded_to_bhaktapur(real_gaz):
    # भ.न.पा. is ambiguous: it can mean भक्तपुर (Kathmandu Valley) or भरतपुर (Chitwan).
    # The abbreviation has been removed to avoid binding wrong locations.
    # When resolving "चितवन जिल्ला भ.न.पा.११", it should resolve to Chitwan district,
    # never Bhaktapur, since the abbreviation is not expanded.
    chitwan = f"{KV}chitawan-np0335"
    d = real_gaz.resolve("चितवन जिल्ला भ.न.पा.११", "चितवन")
    assert d.district == chitwan, "भ.न.पा. should not expand to Bhaktapur"
    # Also verify that place_key doesn't contain the Bhaktapur expansion
    assert "भक्तपुर" not in place_key("भ.न.पा."), "place_key should not expand भ.न.पा. to भक्तपुर नगरपालिका"
