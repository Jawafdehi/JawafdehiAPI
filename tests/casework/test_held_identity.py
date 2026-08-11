"""`casework.held_identity` -- telling same-named court defendants apart.

The two cases used throughout are the real collision the FY078/079 dry run
surfaced: `ज्ञानेन्द्र चौधरी` is an elected ward chair in Rautahat on
`078-CR-0118` and a contracted environment officer in Jhapa on `079-CR-0071`.
"""

import pytest

from casework.held_identity import (
    EVIDENCE_FLOOR,
    MAX_MENTIONS,
    CaseIdentity,
    HeldVerdict,
    build_content,
    case_identity,
    compare_held,
    compare_identities,
    discriminator,
    splittable,
)

HELD_NAME = "ज्ञानेन्द्र चौधरी"

RAUTAHAT = {
    "slug": "case-078-cr-0118",
    "title": "CIAA Special Court Case 078-CR-0118: गोपाल राय यादव समेत २१",
    "description": (
        "नगरप्रमुख, नगर उप-प्रमुख, वडा अध्यक्ष र नगर कार्यपालिका सदस्यहरूले लाल बकैया "
        f"नदीको ठेक्काको निर्णय कार्यान्वयन गराउन वेवास्ता गरेको। {HELD_NAME} "
        "समेत १८ जनाले सहीछाप गरेको।"),
    "evidence": [{"material": {
        "material_type": "press_release",
        "display_name": ("जिल्ला रौतहट, फतुवा विजयपुर नगरपालिका ... बडा अध्यक्षहरु "
                         "र नगर कार्यपालिका सदस्यहरु समेत २१ जनाउपर")}}],
    "entities": [{"nes_id": "https://jawafdehi.org/entity/location/district/rautahat-np0232"}],
}

JHAPA = {
    "slug": "case-079-cr-0071",
    "title": "CIAA Special Court Case 079-CR-0071: ज्ञानेन्द्र चौधरी समेत ४",
    "description": (
        f"दमक नगरपालिकाका वातावरण अधिकृत {HELD_NAME} र मिक्लाजुङ गाउँपालिकाका "
        "असिस्टेन्ट सव-इन्जिनियर विवेक कार्कीले बढी उत्खनन नियन्त्रण नगरेको।"),
    "evidence": [{"material": {
        "material_type": "press_release",
        "display_name": ("जिल्ला झापा, दमक नगरपालिकाका वातावरण अधिकृत ज्ञानेन्द्र "
                         "चौधरी, मिक्लाजुङ्ग गाउँपालिका मोरङ्गका असिस्टेन्ट सव-इन्जिनियर")}}],
    "entities": [
        {"nes_id": "https://jawafdehi.org/entity/location/district/jhapa-np0104"},
        {"nes_id": "https://jawafdehi.org/entity/location/district/jhapa"},
    ],
}


def _card(payload, names=(HELD_NAME,), court_cases=()):
    return case_identity(payload, names, court_cases=court_cases)


class _Recorder:
    """An `invoke_json` stub that records its call and returns a canned reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, system, content, **kwargs):
        self.calls.append({"system": system, "content": content, **kwargs})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


ACTIONABLE = {
    "verdict": "different",
    "confidence": "high",
    "evidence": ("Rautahat elected ward chair versus Jhapa contracted "
                 "environment officer; different districts and posts."),
    "per_case": {"case-078-cr-0118": "वडा अध्यक्ष, रौतहट",
                 "case-079-cr-0071": "वातावरण अधिकृत, झापा"},
}


# ---------------------------------------------------------------- card building

def test_the_press_release_title_is_what_the_card_leads_with():
    card = _card(JHAPA)
    assert card.press_titles == (JHAPA["evidence"][0]["material"]["display_name"],)


def test_a_non_press_release_material_is_not_mistaken_for_one():
    payload = dict(JHAPA, evidence=[
        {"material": {"material_type": "court_order", "display_name": "फैसला"}}])
    assert _card(payload).press_titles == ()


def test_the_two_spellings_of_one_district_collapse_to_one():
    # `079-CR-0071` really carries both `district/jhapa-np0104` and a bare
    # `district/jhapa`. Counted as two, `discriminator` would fall back to the
    # case number for a case that has exactly one district.
    assert _card(JHAPA).districts == ("jhapa",)


def test_a_location_that_is_not_a_district_is_ignored():
    payload = dict(JHAPA, entities=[
        {"nes_id": "https://jawafdehi.org/entity/location/localunit/damak-municipality-11107"}])
    assert _card(payload).districts == ()


def test_the_name_mention_is_excerpted_from_the_description():
    card = _card(JHAPA)
    excerpt = card.mentions["ज्ञानेन्द्र चौधरी"][0]
    assert "वातावरण अधिकृत" in excerpt


def test_a_name_absent_from_the_description_gets_no_excerpt():
    card = _card(JHAPA, names=("सीता शर्मा",))
    assert "सीता शर्मा" not in card.mentions


def test_mentions_are_capped_so_one_long_judgment_cannot_fill_the_prompt():
    payload = dict(JHAPA, description=(HELD_NAME + " ") * 40)
    card = _card(payload)
    assert len(card.mentions["ज्ञानेन्द्र चौधरी"]) == MAX_MENTIONS


def test_consecutive_mentions_do_not_yield_the_same_excerpt_twice():
    # Windows advance past the END of the previous window, so two mentions a
    # few characters apart produce ONE excerpt rather than two near-copies.
    payload = dict(JHAPA, description=f"{HELD_NAME} र {HELD_NAME}")
    assert len(_card(payload).mentions["ज्ञानेन्द्र चौधरी"]) == 1


def test_a_case_with_no_identifying_field_admits_it():
    bare = {"slug": "case-x", "title": "समेत ४", "description": "", "evidence": [],
            "entities": []}
    assert not _card(bare).carries_identity("ज्ञानेन्द्र चौधरी")


def test_a_title_alone_does_not_count_as_identity():
    # Every title in this corpus follows one template, so two of them differ
    # only in the co-defendant count -- that is not a distinguishing fact.
    titled = {"slug": "case-x", "title": JHAPA["title"], "description": "",
              "evidence": [], "entities": []}
    assert not _card(titled).carries_identity("ज्ञानेन्द्र चौधरी")


# ------------------------------------------------------------- discriminator

def test_the_discriminator_prefers_the_single_district():
    assert discriminator(_card(JHAPA)) == "jhapa"


def test_two_districts_fall_back_to_the_court_case_number():
    # The Mawa Khola is the Jhapa/Morang border, so the real case binds both
    # districts and neither is "the" district of the accused.
    payload = dict(JHAPA, entities=JHAPA["entities"] + [
        {"nes_id": "https://jawafdehi.org/entity/location/district/morang-np0105"}])
    card = _card(payload, court_cases=("079-CR-0071",))
    assert discriminator(card) == "079-cr-0071"


def test_no_district_and_no_court_case_yields_no_discriminator():
    bare = {"slug": "case-x", "title": "", "description": "", "evidence": [],
            "entities": []}
    assert discriminator(_card(bare)) == ""


# ------------------------------------------------------------------- verdicts

def test_a_high_confidence_verdict_with_evidence_is_actionable():
    assert HeldVerdict(**ACTIONABLE).is_actionable


@pytest.mark.parametrize("field,value", [
    ("confidence", "medium"),
    ("verdict", "unclear"),
])
def test_a_verdict_short_of_the_bar_is_not_actionable(field, value):
    assert not HeldVerdict(**{**ACTIONABLE, field: value}).is_actionable


def test_a_truncated_evidence_string_is_not_actionable():
    # `salvage_json` closes the open string of a reply cut off at max_tokens,
    # so a truncated verdict arrives well-formed and near-empty.
    short = HeldVerdict(**{**ACTIONABLE, "evidence": "रौतहट र झापा"})
    assert len(short.evidence) < EVIDENCE_FLOOR
    assert not short.is_actionable


def test_a_verdict_with_no_per_case_detail_is_not_actionable():
    assert not HeldVerdict(**{**ACTIONABLE, "per_case": {}}).is_actionable


def test_a_failed_call_is_not_actionable_even_if_it_parsed():
    assert not HeldVerdict(**ACTIONABLE, failed=True).is_actionable


# ------------------------------------------------------- compare_identities

def test_the_real_collision_is_settled_as_two_people():
    stub = _Recorder(ACTIONABLE)
    got = compare_identities(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)], stub)
    assert (got.verdict, got.is_actionable) == ("different", True)


def test_both_press_releases_reach_the_prompt():
    content = build_content(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)])
    assert "रौतहट" in content and "झापा" in content
    assert "वातावरण अधिकृत" in content and "वडा अध्यक्ष" in content


def test_the_prompt_names_every_case_slug_the_reply_must_cover():
    content = build_content(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)])
    assert RAUTAHAT["slug"] in content and JHAPA["slug"] in content


def test_a_thin_card_is_held_without_spending_a_call():
    bare = {"slug": "case-x", "title": "", "description": "", "evidence": [],
            "entities": []}
    stub = _Recorder(ACTIONABLE)
    got = compare_identities(HELD_NAME, [_card(JHAPA), _card(bare)], stub)
    assert stub.calls == []
    assert (got.verdict, got.is_actionable) == ("unclear", False)
    assert "no press release" in got.evidence


def test_one_thin_card_among_three_holds_the_whole_name():
    """Two rich cards no longer license a call that includes a thin one.

    The reply could describe the thin case's slug from nothing, satisfy
    `covers`, go actionable, and the binder would then bind an entity on the one
    case carrying no identifying evidence at all.
    """
    bare = {"slug": "case-thin", "title": "", "description": "", "evidence": [],
            "entities": []}
    stub = _Recorder(ACTIONABLE)
    got = compare_identities(HELD_NAME,
                             [_card(RAUTAHAT), _card(JHAPA), _card(bare)], stub)
    assert stub.calls == []
    assert not got.is_actionable
    assert "case-thin" in got.evidence


def test_three_rich_cards_are_still_compared_in_one_call():
    # The guard must not refuse a name simply for being on three cases.
    third = dict(JHAPA, slug="case-080-cr-0001")
    stub = _Recorder(ACTIONABLE)
    compare_identities(HELD_NAME,
                       [_card(RAUTAHAT), _card(JHAPA), _card(third)], stub)
    assert len(stub.calls) == 1


def test_a_raising_provider_leaves_the_name_held_and_says_the_model_failed():
    stub = _Recorder(RuntimeError("provider down"))
    got = compare_identities(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)], stub)
    assert (got.failed, got.is_actionable) == (True, False)
    assert "RuntimeError" in got.evidence


def test_a_reply_that_is_not_a_dict_fails_rather_than_binding():
    got = compare_identities(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)],
                             _Recorder(["different"]))
    assert (got.failed, got.is_actionable) == (True, False)


def test_an_unknown_verdict_word_fails_rather_than_binding():
    reply = {**ACTIONABLE, "verdict": "probably the same"}
    got = compare_identities(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)],
                             _Recorder(reply))
    assert (got.failed, got.is_actionable) == (True, False)


def test_a_verdict_silent_about_one_case_is_downgraded_not_acted_on():
    # Actionable on its own fields, but it never described `078-CR-0118`, so it
    # has not actually separated that case from the other.
    reply = {**ACTIONABLE,
             "per_case": {"case-079-cr-0071": "वातावरण अधिकृत, झापा"}}
    got = compare_identities(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)],
                             _Recorder(reply))
    assert (got.verdict, got.is_actionable) == ("unclear", False)
    assert "case-078-cr-0118" in got.evidence


def test_the_configured_tier_reaches_the_provider():
    stub = _Recorder(ACTIONABLE)
    compare_identities(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)], stub,
                       tier="premium")
    assert stub.calls[0]["tier"] == "premium"


# ------------------------------------------------------------- compare_held

def test_one_call_per_held_name_not_per_case_and_not_per_pair():
    stub = _Recorder(ACTIONABLE)
    held = {"ज्ञानेन्द्र चौधरी": frozenset({RAUTAHAT["slug"], JHAPA["slug"]})}
    cards = {RAUTAHAT["slug"]: _card(RAUTAHAT), JHAPA["slug"]: _card(JHAPA)}
    verdicts = compare_held(held, cards, stub)
    assert len(stub.calls) == 1
    assert set(verdicts) == set(held)


def test_a_name_on_three_cases_is_still_one_call():
    third = dict(JHAPA, slug="case-080-cr-0001")
    stub = _Recorder(ACTIONABLE)
    held = {"ज्ञानेन्द्र चौधरी": frozenset(
        {RAUTAHAT["slug"], JHAPA["slug"], third["slug"]})}
    cards = {RAUTAHAT["slug"]: _card(RAUTAHAT), JHAPA["slug"]: _card(JHAPA),
             third["slug"]: _card(third)}
    compare_held(held, cards, stub)
    assert len(stub.calls) == 1


def test_each_verdict_is_reported_as_it_lands():
    seen = []
    held = {"ज्ञानेन्द्र चौधरी": frozenset({RAUTAHAT["slug"], JHAPA["slug"]})}
    cards = {RAUTAHAT["slug"]: _card(RAUTAHAT), JHAPA["slug"]: _card(JHAPA)}
    compare_held(held, cards, _Recorder(ACTIONABLE),
                 on_verdict=lambda n, s, v: seen.append((n, tuple(s), v.verdict)))
    assert seen == [(HELD_NAME, (RAUTAHAT["slug"], JHAPA["slug"]), "different")]


def test_a_card_missing_for_one_case_still_compares_what_it_has():
    # A pass-1 read failure removes a case from `identities`. The remaining
    # single card cannot be compared, so the name stays held -- it must not
    # raise a KeyError and take the whole run down.
    held = {"ज्ञानेन्द्र चौधरी": frozenset({RAUTAHAT["slug"], "case-gone"})}
    verdicts = compare_held(held, {RAUTAHAT["slug"]: _card(RAUTAHAT)},
                            _Recorder(ACTIONABLE))
    assert not verdicts["ज्ञानेन्द्र चौधरी"].is_actionable


def test_the_system_prompt_forbids_reasoning_from_the_shared_name():
    from casework.held_identity import SYSTEM
    assert "shared name is NOT evidence" in SYSTEM
    assert '"unclear"' in SYSTEM


def test_an_empty_card_map_never_calls_the_model():
    stub = _Recorder(ACTIONABLE)
    assert compare_held({}, {}, stub) == {}
    assert stub.calls == []


def test_a_card_is_a_frozen_dataclass_so_a_verdict_cannot_edit_its_source():
    with pytest.raises(Exception):
        _card(JHAPA).slug = "case-other"


def test_case_identity_needs_no_api_object():
    # The whole point: cards are built from a payload pass 1 already read, so a
    # comparison adds no HTTP request to a run measured at 8.8 per case.
    assert isinstance(_card(JHAPA), CaseIdentity)


# --------------------------------------------------- review findings 1, 2, 3

def test_a_district_office_is_not_read_as_a_district():
    # This corpus binds government offices under
    # `organization/government/district/dfo`. A bare `/district/` substring test
    # reads that as the district `dfo` -- and `discriminator` would then bake
    # `-dfo` into a permanent public person IRI.
    payload = dict(JHAPA, entities=[
        {"nes_id": "https://jawafdehi.org/entity/organization/government/district/dfo"}])
    card = _card(payload)
    assert card.districts == ()
    assert not discriminator(card).endswith("dfo")


def test_a_legacy_scheme_district_iri_is_refused():
    # The repo's IRI rules forbid reintroducing `entity:<prefix>/<slug>`, and a
    # substring test on `location/district/` accepted it. `parse_entity_iri`
    # raises on the legacy form, so it can never reach a public person IRI as a
    # discriminator.
    payload = dict(JHAPA, entities=[{"nes_id": "entity:location/district/jhapa"}])
    assert _card(payload).districts == ()


def test_a_district_nested_under_another_prefix_is_refused():
    # `organization/foo/location/district/bar` contains the district path but is
    # not a district. Equality on the parsed prefix rejects it; a substring
    # test did not.
    payload = dict(JHAPA, entities=[{
        "nes_id": "https://jawafdehi.org/entity/organization/foo/location/district/bar"}])
    assert _card(payload).districts == ()


def test_a_malformed_bind_iri_does_not_break_the_card():
    payload = dict(JHAPA, entities=[{"nes_id": "not-an-iri"}, {"nes_id": None}, {}])
    assert _card(payload).districts == ()


def test_a_district_office_does_not_suppress_the_real_district():
    # Counted as a second "district", the office would push a
    # single-district case onto the court-number fallback.
    payload = dict(JHAPA, entities=JHAPA["entities"] + [
        {"nes_id": "https://jawafdehi.org/entity/organization/government/district/dfo"}])
    assert discriminator(_card(payload)) == "jhapa"


@pytest.mark.parametrize("material_type", ["press_release", "ciaa_press_release",
                                           "charge_sheet"])
def test_every_established_press_type_contributes_its_title(material_type):
    # `PRESS_TYPES` is deliberately wide: `charge_sheet` was measured at 100%
    # MARKDOWN coverage against 8.6% for `press_release`, so accepting only the
    # latter would leave the commonest case with no title, no comparison, and
    # the name held -- the feature no-oping on what it exists for.
    payload = dict(JHAPA, evidence=[{"material": {
        "material_type": material_type,
        "display_name": "जिल्ला झापा, दमक नगरपालिकाका वातावरण अधिकृत"}}])
    assert _card(payload).press_titles != ()
    assert _card(payload).carries_identity("ज्ञानेन्द्र चौधरी")


def test_a_court_order_still_does_not_count_as_a_press_title():
    payload = dict(JHAPA, evidence=[
        {"material": {"material_type": "court_order", "display_name": "फैसला"}}])
    assert _card(payload).press_titles == ()


def test_two_cases_in_one_district_are_not_splittable():
    # Each discriminates to the same district, so both would derive one entity
    # slug and the split would land as the merge it was ordered to prevent.
    a = CaseIdentity(slug="case-a", districts=("jhapa",))
    b = CaseIdentity(slug="case-b", districts=("jhapa",))
    assert not splittable([a, b])


def test_two_cases_sharing_one_court_number_are_not_splittable():
    # Duplicate case records citing one court reference: a known prod condition.
    a = CaseIdentity(slug="case-a", court_cases=("079-CR-0071",))
    b = CaseIdentity(slug="case-b", court_cases=("079-CR-0071",))
    assert not splittable([a, b])


def test_a_case_with_no_discriminator_at_all_is_not_splittable():
    a = CaseIdentity(slug="case-a", districts=("jhapa",))
    assert not splittable([a, CaseIdentity(slug="case-b")])


def test_distinct_districts_are_splittable():
    a = CaseIdentity(slug="case-a", districts=("rautahat",))
    b = CaseIdentity(slug="case-b", districts=("jhapa",))
    assert splittable([a, b])


def test_the_configured_usage_accumulator_reaches_the_provider():
    stub = _Recorder(ACTIONABLE)
    sentinel = object()
    compare_identities(HELD_NAME, [_card(RAUTAHAT), _card(JHAPA)], stub,
                       usage=sentinel)
    assert stub.calls[0]["usage"] is sentinel
