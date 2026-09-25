import pytest
from casework.location_gazetteer import Gazetteer, load_gazetteer, place_key

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
