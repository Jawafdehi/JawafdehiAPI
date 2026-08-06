"""Tests for the pure NES entity resolver (casework/entity_resolver.py).

Every string here is real: the extracted names come from the July A/B run logs
(work/2026-07-17-enricher-extraction/), the NES ids and stored names come from
prod reads. A wrong bind attaches a named person to a corruption case they had
nothing to do with, so the false-positive tests are the point of this file.
"""
import inspect

import pytest

from casework.entity_resolver import (
    BIND,
    ELECTION_RECORD_MARKERS,
    MIN_BIND_SCORE,
    NO_MATCH,
    _name_vetoes,
    PROVINCE_NAME_FORMS,
    REVIEW,
    apply_document_veto,
    has_devanagari,
    asserted_province,
    candidate_name_forms,
    comparable_name_forms,
    is_election_candidate_record,
    match_score,
    name_tokens,
    names_an_institution,
    names_the_province,
    normalise_name,
    qualified_siblings,
    resolve,
    token_forms,
    tokens_equal,
)


def test_zero_width_joiner_is_stripped():
    # Prod hazard: person/anish-shrestha-219986 stores श्रेष्‍ठ with U+200D, and
    # the extraction logs hold both ईश्वरप्रसाद and ईश्‍वरप्रसाद.
    assert normalise_name("अनिष श्रेष्‍ठ") == normalise_name("अनिष श्रेष्ठ")


def test_trailing_visarga_artifact_is_stripped():
    # The logs contain "टिकाराम ज्ञवालीः" — a stray visarga on the extracted string.
    assert normalise_name("टिकाराम ज्ञवालीः") == normalise_name("टिकाराम ज्ञवाली")


def test_embedded_and_trailing_whitespace_collapses():
    assert normalise_name("  राम   बहादुर\tथापा  ") == "राम बहादुर थापा"


def test_honorifics_are_dropped_from_tokens():
    with_honorific = [raw for raw, _ in name_tokens("श्री अनिष श्रेष्ठ")]
    assert with_honorific == [raw for raw, _ in name_tokens("अनिष श्रेष्ठ")]


def test_devanagari_token_carries_its_romanisations():
    forms = token_forms("श्रेष्ठ")
    assert "shreshtha" in forms
    assert "shreshth" in forms


def test_latin_variant_table_bridges_shrestha_spellings():
    # to_roman_colloquial gives श्रेष्ठ -> "shreshtha", so the common Latin
    # spelling "Shrestha" only meets it through the curated variant table.
    assert tokens_equal(name_tokens("Shrestha")[0], name_tokens("श्रेष्ठ")[0])
    assert tokens_equal(name_tokens("Shreshtha")[0], name_tokens("श्रेष्ठ")[0])


def test_mixed_script_string_tokenises_both_sides():
    raws = [raw for raw, _ in name_tokens("Anish श्रेष्ठ")]
    assert raws == ["anish", "श्रेष्ठ"]


def test_identical_name_scores_one():
    assert match_score("अनिष श्रेष्ठ", "अनिष श्रेष्ठ") == pytest.approx(1.0)


def test_cross_script_four_tokens_scores_above_threshold():
    # Four tokens, every one matched through romanisation rather than identity:
    # 1.0 - 4 * 0.02 = 0.92. The stored name is person/ram-krishna-thapa-magar-180346.
    score = match_score("Ram Krishna Thapa Magar", "राम कृष्ण थापा मगर")
    assert score == pytest.approx(0.92)
    assert score >= MIN_BIND_SCORE


def test_omitted_middle_particle_still_binds():
    # "राम थापा" against "राम बहादुर थापा": बहादुर is a curated particle, so the
    # omission costs 0.05 and the two anchors carry the match.
    score = match_score("राम थापा", "राम बहादुर थापा")
    assert score == pytest.approx(0.95)
    assert score >= MIN_BIND_SCORE


def test_two_omitted_particles_fall_below_threshold():
    # Two stacked guesses is not near-certain. With a FLAT particle penalty this
    # would score 0.90 and bind, which is why the second omission costs more.
    score = match_score("राम थापा", "राम बहादुर प्रसाद थापा")
    assert score == pytest.approx(0.80)
    assert score < MIN_BIND_SCORE
    score_cross = match_score("Ram Thapa", "राम बहादुर प्रसाद थापा")
    assert score_cross == pytest.approx(0.76)
    assert score_cross < MIN_BIND_SCORE


def test_reordered_name_parts_still_match():
    # A verifiable swap: shrestha is a curated surname, anish is not, so the
    # reorder is safe to read as surname-first vs given-name-first. Pinned to
    # the exact score (2 tokens matched only through romanisation, 1.0 - 2 *
    # 0.02 = 0.96) so a future edit to the swap rule can't silently change it.
    score = match_score("Shrestha Anish", "अनिष श्रेष्ठ")
    assert score == pytest.approx(0.96)
    assert score >= MIN_BIND_SCORE


def test_reversal_with_no_surname_anchor_scores_zero():
    # The bug this whole rule exists for: राम and कृष्ण are both plain given
    # names, so nothing on either side says which end is the surname. Without
    # the curated SURNAMES check this scored a perfect 1.0 — indistinguishable
    # from an exact match — on nothing but a coincidental permutation.
    assert match_score("कृष्ण राम", "राम कृष्ण") == 0.0


def test_reversal_of_two_surnames_scores_zero():
    # थापा and मगर are both curated surnames, so a swap can't tell which end
    # is meant to be the surname either. Failing closed here is correct: this
    # module never guesses when a rule can't decide.
    assert match_score("थापा मगर", "मगर थापा") == 0.0


def test_three_token_given_name_reversal_scores_zero():
    # राम and कृष्ण swap places but थापा -- the true surname -- stays last on
    # both sides. The anchor check only ever looks at first-and-last, so this
    # is the load-bearing reason the swap rule is survivable at all: the first
    # anchors (कृष्ण vs राम) don't match straight, and swapping pairs कृष्ण
    # against थापा, which doesn't match either. No interior reordering can
    # sneak past the anchor gate.
    assert match_score("कृष्ण राम थापा", "राम कृष्ण थापा") == 0.0


def test_swapped_anchors_with_non_particle_interior_token_scores_zero():
    # शाह is a curated surname, विजय is not, so the swap itself is allowed —
    # both anchors pair up. But विक्रम, the interior token left in the longer
    # name, is not a droppable particle, so the particle rule (not the anchor
    # rule) is what stops this. Reversed form of person/bija-bikram-shaha-178948's
    # false-positive trap.
    assert match_score("शाह विजय", "विजय विक्रम शाह") == 0.0


def test_swapped_anchors_with_one_character_surname_difference_scores_zero():
    # The reordered form of the श्रेष्ट/श्रेष्ठ one-character trap: the anchor
    # check rejects it before the surname rule is even consulted, because
    # श्रेष्ट and श्रेष्ठ's form-sets don't intersect either straight or swapped.
    assert match_score("श्रेष्ट अनिष", "अनिष श्रेष्ठ") == 0.0


def test_swapped_anchors_with_missing_surname_scores_zero():
    # Reordered form of the घुरनी देवी trap: खत्वे pairs with neither घुरनी nor
    # देवी under the swap, so the anchor check rejects it outright.
    assert match_score("खत्वे घुरनी", "घुरनी देवी") == 0.0


def test_partial_match_with_different_surname_scores_zero():
    # Real trap: the extracted घुरनी देवी खत्वे has no NES entry, but प्रोड holds
    # "घुरनी देवी" — the anchors do not both match, so this must never bind.
    assert match_score("घुरनी देवी खत्वे", "घुरनी देवी") == 0.0


def test_shared_surname_with_different_given_name_scores_zero():
    assert match_score("अनुप कुमार खत्री", "अंकुर खत्री") == 0.0


def test_non_particle_omission_scores_zero():
    # The probe's false positive. Both anchors match — विजय and शाह — so only the
    # particle rule stops this. विक्रम is not a droppable particle, and whether
    # the two names are the same man is a caseworker's call, not a scorer's.
    # Stored name of person/bija-bikram-shaha-178948, verified against prod.
    assert match_score("विजय शाह", "विजय विक्रम शाह") == 0.0


def test_one_character_difference_in_the_surname_scores_zero():
    # श्रेष्ठ vs श्रेष्ट — no edit distance exists in this module, by design.
    assert match_score("अनिष श्रेष्ट", "अनिष श्रेष्ठ") == 0.0


def test_empty_name_scores_zero():
    assert match_score("", "अनिष श्रेष्ठ") == 0.0
    assert match_score("अनिष श्रेष्ठ", "") == 0.0


def _candidate(iri, ne=None, en=None):
    return {"id": iri, "title": {"ne": ne, "en": en}}


ANISH_A = _candidate(
    "https://jawafdehi.org/entity/person/anish-shrestha-219986",
    ne="अनिष श्रेष्‍ठ", en="Anish Shrestha",
)
ANISH_B = _candidate(
    "https://jawafdehi.org/entity/person/anish-shrestha-285096",
    ne="अनिष श्रेष्ठ", en="Anish Shrestha",
)
ANKUR = _candidate(
    "https://jawafdehi.org/entity/person/amkura-khatri-2de9b3",
    ne="अंकुर खत्री", en="Ankur Khatri",
)


def test_single_confident_candidate_binds():
    decision = resolve("अंकुर खत्री", [ANKUR])
    assert decision.verdict == BIND
    assert decision.nes_id == "https://jawafdehi.org/entity/person/amkura-khatri-2de9b3"


def test_two_distinct_entities_with_the_same_name_never_bind():
    # Both of these are real prod rows, tied at BM25 182.17. Auto-binding the
    # higher-scoring one is exactly the defamation case.
    decision = resolve("अनिष श्रेष्ठ", [ANISH_A, ANISH_B])
    assert decision.verdict == REVIEW
    assert "ambiguous" in decision.reason
    assert decision.nes_id is None


def test_same_entity_matching_on_two_name_forms_still_binds():
    # One id, matched via both its ne and en title — not an ambiguity.
    decision = resolve("Anish Shrestha", [ANISH_A])
    assert decision.verdict == BIND


def test_name_absent_from_nes_is_no_match():
    decision = resolve("खगेन्द्र पराजुली", [ANKUR, ANISH_A])
    assert decision.verdict == NO_MATCH
    assert decision.nes_id is None


def test_generic_institutional_name_goes_to_review():
    office = _candidate(
        "https://jawafdehi.org/entity/organization/jilla-vana-karyalaya-f4548e",
        ne="जिल्ला वन कार्यालय",
    )
    decision = resolve("जिल्ला वन कार्यालय", [office])
    assert decision.verdict == REVIEW
    assert "generic" in decision.reason


def test_composite_activity_location_name_is_never_split_or_bound():
    district = _candidate(
        "https://jawafdehi.org/entity/location/district/mugu", ne="मुगु जिल्ला",
    )
    decision = resolve("जिल्ला वन कार्यालय - मुगु जिल्ला", [district])
    assert decision.verdict in (REVIEW, NO_MATCH)
    assert decision.nes_id is None


def test_composite_name_against_its_own_exact_candidate_hits_the_composite_veto():
    # test_composite_activity_location_name_is_never_split_or_bound passes
    # through NO_MATCH (nothing qualifies), so it never reaches the composite
    # veto in _name_vetoes. Score a candidate against the FULL composite
    # string so the veto branch itself -- and its dependence on
    # normalise_name preserving the " - " separator -- has a regression test.
    composite = _candidate(
        "https://jawafdehi.org/entity/organization/jilla-vana-karyalaya-mugu-a1b2c3",
        ne="जिल्ला वन कार्यालय - मुगु जिल्ला",
    )
    decision = resolve("जिल्ला वन कार्यालय - मुगु जिल्ला", [composite])
    assert decision.verdict == REVIEW
    assert "composite" in decision.reason
    assert decision.nes_id is None


def test_generic_institutional_name_in_latin_script_goes_to_review():
    # GENERIC_TOKENS must cover both scripts, like MIDDLE_PARTICLES and
    # SURNAMES: token_forms only adds a romanisation for a non-ASCII token, so
    # a fully-Latin institutional name never reaches a Devanagari-only set.
    office = _candidate(
        "https://jawafdehi.org/entity/organization/district-forest-office-aa11bb",
        en="District Forest Office",
    )
    decision = resolve("District Forest Office", [office])
    assert decision.verdict == REVIEW
    assert "generic" in decision.reason


# ---------------------------------------------------------------------------
# The unqualified-institution veto. STRUCTURAL, not lexical: an institution-type
# name that NES also holds with a locality appended is the family, not a member,
# so the district-less record it matches asserts no district. This replaced a
# curated-vocabulary test that fired only when the DOMAIN word happened to be on
# a list -- `जिल्ला वन कार्यालय` was held because वन was added, and
# `मालपोत कार्यालय` bound at 1.00 to a district-less bucket because मालपोत was
# not. Cost on the labelled set: precision stays 1.000, recall 0.872 -> 0.846.
# ---------------------------------------------------------------------------

_MALPOT_BUCKET = _candidate(
    "https://jawafdehi.org/entity/organization/malapota-karyalaya-44fbce",
    ne="मालपोत कार्यालय",
)
_MALPOT_KALANKI = _candidate(
    "https://jawafdehi.org/entity/organization/malpot-land-revenue-office-kalanki",
    ne="मालपोत कार्यालय, कलंकी",
)
_MALPOT_SURKHET = _candidate(
    "https://jawafdehi.org/entity/organization/malapota-karyalaya-surkheta-b5bb2d",
    ne="मालपोत कार्यालय सुर्खेत",
)


def test_unqualified_office_name_is_held_when_nes_holds_qualified_siblings():
    # All three are real prod rows. The bare one scores 1.00 and would bind.
    decision = resolve(
        "मालपोत कार्यालय", [_MALPOT_BUCKET, _MALPOT_KALANKI, _MALPOT_SURKHET])
    assert decision.verdict == REVIEW
    assert decision.nes_id is None
    assert "unqualified institution name" in decision.reason
    # The reason must name a sibling, or a caseworker cannot see what to add.
    assert "कलंकी" in decision.reason or "सुर्खेत" in decision.reason


def test_the_same_office_name_carrying_its_district_still_binds():
    # The district-qualified name is not a prefix of anything, so it binds even
    # with the bare bucket sitting in the same candidate list. This is the half
    # of the behaviour that keeps the veto from costing correct office binds.
    decision = resolve(
        "मालपोत कार्यालय, कलंकी", [_MALPOT_BUCKET, _MALPOT_KALANKI, _MALPOT_SURKHET])
    assert decision.verdict == BIND
    assert decision.nes_id == (
        "https://jawafdehi.org/entity/organization/malpot-land-revenue-office-kalanki")


def test_a_place_that_owns_ward_offices_still_binds():
    # PREFIX, not subset, and this is why. NES holds a ward office per ward
    # INSIDE अदानचुली गाउँपालिका, and every one of those titles CONTAINS the
    # municipality's name -- at the END. Those are children of the entity, not
    # other instances of it. A subset test would refuse all seven municipality
    # binds in the labelled set on top of मालपोत कार्यालय (measured: 33 correct
    # binds down to 26, recall 0.846 -> 0.667).
    palika = _candidate(
        "https://jawafdehi.org/entity/location/localunit/adanchuli-gaunpalika-60306",
        ne="अदानचुली गाउँपालिका",
    )
    ward = _candidate(
        "https://jawafdehi.org/entity/organization/vada-na-1-ko-karyalaya-ada-9c1d2e",
        ne="वडा नं. 1 को कार्यालय, अदानचुली गाउँपालिका",
    )
    decision = resolve("अदानचुली गाउँपालिका", [palika, ward])
    assert decision.verdict == BIND
    assert decision.nes_id == (
        "https://jawafdehi.org/entity/location/localunit/adanchuli-gaunpalika-60306")


def test_the_veto_does_not_depend_on_a_curated_domain_word():
    # राजस्व (revenue) is on NO list in this module -- that is the point. The
    # veto reads the candidate list's shape, so a domain word nobody curated
    # behaves exactly like मालपोत. This is the test that fails if someone
    # re-implements the veto as a vocabulary check.
    bucket = _candidate(
        "https://jawafdehi.org/entity/organization/rajasva-karyalaya-7fa1b2",
        ne="राजस्व कार्यालय",
    )
    qualified = _candidate(
        "https://jawafdehi.org/entity/organization/rajasva-karyalaya-birgunja-3c4d5e",
        ne="राजस्व कार्यालय बिरगंज",
    )
    decision = resolve("राजस्व कार्यालय", [bucket, qualified])
    assert decision.verdict == REVIEW
    assert "unqualified institution name" in decision.reason


def test_a_person_name_never_reaches_the_bucket_test():
    # No organisational-form word, so the gate excludes it. Without that gate a
    # two-token person name would be vetoed by any longer namesake in the
    # candidate list, which is a recall cost with no precision benefit -- the
    # ambiguity veto already handles same-name entities.
    ram = _candidate(
        "https://jawafdehi.org/entity/person/rama-thapa-4c5d6e", ne="राम थापा")
    longer = _candidate(
        "https://jawafdehi.org/entity/person/rama-thapa-magara-7f8a9b",
        ne="राम थापा मगर",
    )
    assert resolve("राम थापा", [ram, longer]).verdict == BIND


def test_the_winners_own_longer_alternate_title_is_not_a_sibling():
    # One entity carrying both "प्रहरी कार्यालय" and a longer title must not veto
    # itself. `qualified_siblings` excludes the winning IRI for exactly this.
    same = {
        "id": "https://jawafdehi.org/entity/organization/prahari-karyalaya-1a2b3c",
        "title": {"ne": "प्रहरी कार्यालय", "en": "Prahari Karyalaya Nepal"},
    }
    assert resolve("प्रहरी कार्यालय", [same]).verdict == BIND


def test_qualified_siblings_dedupes_a_repeated_search_row():
    # The frozen capture's closest real analogue is not a repeat: "मालपोत
    # कार्यालय, पर्सा" is two DISTINCT IRIs at an identical score
    # (location/malpot-parsa, organization/malapota-karyalaya-parsa-3fcb31,
    # both 124.35) -- a shared title, not one IRI returned twice. A genuine
    # repeat is constructed here instead, because one must not inflate the
    # count the review reason prints.
    repeated = [_MALPOT_SURKHET, dict(_MALPOT_SURKHET)]
    assert qualified_siblings("मालपोत कार्यालय", repeated) == ("मालपोत कार्यालय सुर्खेत",)


def test_qualified_siblings_ignores_a_shorter_or_equal_title():
    assert qualified_siblings("मालपोत कार्यालय", [_MALPOT_BUCKET]) == ()


def test_names_an_institution_is_the_gate_and_reads_both_scripts():
    assert names_an_institution("मालपोत कार्यालय")
    assert names_an_institution("District Forest Office")
    assert not names_an_institution("अंकुर खत्री")
    assert not names_an_institution("Norton Rose Fulbright LLP")


def test_single_token_name_goes_to_review():
    nepal = _candidate("https://jawafdehi.org/entity/location/nepal", en="Nepal")
    decision = resolve("Nepal", [nepal])
    assert decision.verdict == REVIEW
    assert "single token" in decision.reason


def test_malformed_iri_candidate_is_dropped_before_scoring():
    # A non-canonical host must never reach the API. The old test fixture
    # https://nes.jawafdehi.org/entity/1 is exactly this shape.
    bad = _candidate("https://nes.jawafdehi.org/entity/1", ne="अंकुर खत्री")
    decision = resolve("अंकुर खत्री", [bad])
    assert decision.verdict == NO_MATCH
    assert decision.candidates == ()


def test_decision_records_every_scoring_candidate_for_the_review_file():
    decision = resolve("अनिष श्रेष्ठ", [ANISH_A, ANISH_B])
    assert len(decision.candidates) == 2
    scores = [score for score, _, _ in decision.candidates]
    assert scores == sorted(scores, reverse=True)


def test_organisation_filed_under_person_prefix_still_binds():
    # Prod files "Ncell Pvt. Ltd." under person/ with @type Person. There is no
    # person/org type veto, on purpose — one would reject correct binds.
    ncell = _candidate(
        "https://jawafdehi.org/entity/person/ncell-pvt-ltd-11aa22", en="Ncell Pvt. Ltd.",
    )
    assert resolve("Ncell Pvt. Ltd.", [ncell]).verdict == BIND





# ---------------------------------------------------------------------------
# Where two Devanagari spellings may and may not meet.
#
# This section used to describe `to_roman_colloquial`'s fold as a deliberate,
# bounded property limited to vowel length. It was neither bounded nor limited to
# that: the same fold made कमल equal कमला, and गणेश equal गनेश. Matching now goes
# through `_matra_length_key` for same-script pairs, and romanisation only ever
# bridges ACROSS scripts.
#
# These tests pin the boundary from both sides -- what must still match, and what
# must never -- so a future widening fails loudly instead of quietly binding a
# namesake.
# ---------------------------------------------------------------------------


def test_an_inserted_matra_no_longer_folds_even_on_a_real_variant():
    # Prod: person/mingna-lhamu-sherpa-328030 stores मिङमा ल्हामु शेर्पा while the
    # extractor emitted मिङमा ल्हमु शेर्पा -- genuinely one person, and this pair
    # USED to match at 0.98 because both romanise to "lhamu".
    #
    # It no longer does, and that is a deliberate trade. The fold that matched
    # these two was romanisation-based, and the same mechanism made कमल equal
    # कमला -- a masculine name binding to a feminine entity at 0.98. Matra
    # LENGTH still folds (निधि/निधी below); an INSERTED ा does not, because a
    # final ा is Nepali's masculine -> feminine marker and no positional rule
    # separating "internal ा" from "final ा" has been measured yet.
    #
    # So this name is now a no-match a caseworker resolves in five minutes,
    # rather than a mechanism that can attach the wrong person to a corruption
    # case. See `tokens_equal`.
    assert not tokens_equal(name_tokens("ल्हमु")[0], name_tokens("ल्हामु")[0])
    assert match_score("मिङमा ल्हमु शेर्पा", "मिङमा ल्हामु शेर्पा") == 0.0


def test_the_fold_that_matched_lhamu_would_also_bind_a_feminine_namesake():
    # The reason the test above gave up its bind. Both directions, so neither
    # ordering sneaks through.
    assert match_score("कमल थापा", "कमला थापा") == 0.0
    assert match_score("कमला थापा", "कमल थापा") == 0.0
    # And the consonant collapses the same mechanism allowed: ण/न and श/ष each
    # romanise to one Latin letter.
    assert match_score("गणेश थापा", "गनेश थापा") == 0.0
    assert match_score("आशिष राई", "आषिश राई") == 0.0


def test_vowel_length_fold_is_what_lets_a_real_variant_bind():
    # The same fold earns its keep: निधि against the stored निधी is a spelling
    # variant of one person, not two people.
    assert match_score("रमेश कुमार निधि", "रमेश कुमार निधी") == pytest.approx(0.98)


def test_consonant_difference_still_does_not_match():
    # The boundary. श्रेष्ठ vs श्रेष्ट is a CONSONANT difference (ठ vs ट), and it
    # must never bind -- this is the case the "no edit distance" rule exists for.
    assert match_score("अनिष श्रेष्ठ", "अनिष श्रेष्ट") == 0.0
    assert resolve(
        "अनिष श्रेष्ट",
        [{"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
          "title": {"ne": "अनिष श्रेष्ठ"}}],
    ).verdict == NO_MATCH


# ---------------------------------------------------------------------------
# is_election_candidate_record -- the ECN veto predicate. Six of the 39
# first-pass binds across the labelled set were namesake ward candidates in the
# wrong district; this is what refuses them.
# ---------------------------------------------------------------------------

_ECN_ID = {"@type": "PropertyValue", "propertyID": "ecn-candidate-id", "value": "318984"}
_CBS_ID = {"@type": "PropertyValue", "propertyID": "cbs-local-unit-code", "value": "60306"}


def test_ecn_marker_in_a_list_is_detected():
    # person/raj-bahadur-bam-318984: an elected Ward Member in Kalikot, while the
    # DRAFT case that extracted the name concerns an acting Chief Administrative
    # Officer. Case identifiers stay out of git; see
    # work/2026-08-03-Fix-related_entities-enricher/ for the specifics.
    assert is_election_candidate_record({"identifier": [_ECN_ID]}) is True


def test_ecn_marker_as_a_bare_dict_is_detected():
    # `identifier` is single-valued rather than a list on some documents.
    assert is_election_candidate_record({"identifier": _ECN_ID}) is True


def test_portal_backlog_document_is_not_an_election_record():
    # person/amkura-khatri-2de9b3 -- a real CIAA portal entity, correctly bound
    # by a caseworker on case-080-cr-0064. No identifier at all.
    assert is_election_candidate_record(
        {"@id": "https://jawafdehi.org/entity/person/amkura-khatri-2de9b3",
         "name": {"ne": "अंकुर खत्री"}, "dateCreated": "2026-07-03T19:04:58Z"}
    ) is False


def test_another_identifier_type_is_not_an_election_record():
    # location/localunit/adanchuli-gaunpalika-60306 carries a CBS code, and is a
    # correct bind. Only the ECN marker vetoes.
    assert is_election_candidate_record({"identifier": [_CBS_ID]}) is False


@pytest.mark.parametrize(
    "document",
    [
        None,
        {},
        "not a document",
        {"identifier": None},
        {"identifier": "ecn-candidate-id"},
        {"identifier": ["ecn-candidate-id", None, 7]},
        {"identifier": [None, _ECN_ID]},
    ],
)
def test_malformed_documents_do_not_raise(document):
    # A veto that raises on a shape it did not expect is a veto that gets
    # wrapped in a bare except and silently disabled. It must only ever answer.
    assert isinstance(is_election_candidate_record(document), bool)


def test_ecn_marker_still_found_past_a_non_dict_member():
    assert is_election_candidate_record({"identifier": [None, _ECN_ID]}) is True


# ---------------------------------------------------------------------------
# apply_document_veto -- the pure downgrade. resolve() stays untouched; the I/O
# belongs to the caller.
# ---------------------------------------------------------------------------

_BAM_CANDIDATES = [
    {"id": "https://jawafdehi.org/entity/person/raj-bahadur-bam-318984",
     "title": {"ne": "राज बहादुर बम"}},
]


def test_veto_downgrades_an_ecn_bind_to_review():
    decision = resolve("राज बहादुर बम", _BAM_CANDIDATES)
    assert decision.verdict == BIND

    vetoed = apply_document_veto(decision, {"identifier": [_ECN_ID]})

    assert vetoed.verdict == REVIEW
    assert vetoed.nes_id is None
    assert "ecn-candidate-id" in vetoed.reason
    # The caseworker still needs the evidence to judge -- Task 7 writes these
    # into the review file, so a veto must not blank them.
    assert vetoed.candidates == decision.candidates
    assert vetoed.score == decision.score
    assert vetoed.matched_name == decision.matched_name


def test_veto_leaves_a_clean_bind_alone():
    decision = resolve("राज बहादुर बम", _BAM_CANDIDATES)
    assert apply_document_veto(decision, {"identifier": [_CBS_ID]}) == decision


def test_veto_leaves_review_and_no_match_untouched_so_it_is_safe_to_call_always():
    review = resolve(
        "अनिष श्रेष्ठ",
        [{"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
          "title": {"ne": "अनिष श्रेष्ठ"}},
         {"id": "https://jawafdehi.org/entity/person/anish-shrestha-285096",
          "title": {"ne": "अनिष श्रेष्ठ"}}],
    )
    assert review.verdict == REVIEW
    assert apply_document_veto(review, {"identifier": [_ECN_ID]}) == review

    no_match = resolve("खगेन्द्र पराजुली", [])
    assert no_match.verdict == NO_MATCH
    assert apply_document_veto(no_match, {"identifier": [_ECN_ID]}) == no_match


@pytest.mark.parametrize("document", [None, {}, "junk", 0, [], "", [{"identifier": []}]])
def test_veto_fails_closed_when_the_document_cannot_be_read(document):
    # The caller feeds this the result of a SECOND HTTP read. A WAF 403, a 502, a
    # 404 on an entity renamed between the two calls, or an empty body must not
    # hand back the bind. Whether raj-bahadur-bam-318984 is the right man is
    # exactly what the unread document would have told us -- so the answer is
    # REVIEW, not a guess in either direction.
    decision = resolve("राज बहादुर बम", _BAM_CANDIDATES)
    assert decision.verdict == BIND

    unverified = apply_document_veto(decision, document)

    assert unverified.verdict == REVIEW
    assert unverified.nes_id is None
    assert unverified.candidates == decision.candidates


@pytest.mark.parametrize(
    "document",
    [
        {"identifier": None, "hasOccupation": None},
        {"identifier": [], "hasOccupation": []},
        {"identifier": [_CBS_ID]},
    ],
)
def test_a_document_with_no_ecn_marker_is_readable_and_still_binds(document):
    # The boundary that matters, and it is easy to get backwards. "No ECN
    # identifier" is an ANSWER, not a failed read: measured against production,
    # 25 of 40 entity documents the resolver would bind are exactly
    # {"identifier": null, "hasOccupation": null} -- the CIAA portal entities that
    # human caseworkers correctly bound as accused. Treating that shape as
    # unverified would downgrade every one of them and drop recall to near zero.
    # Fail-closed applies to a document we could not READ, not to one that simply
    # carries no marker.
    decision = resolve("राज बहादुर बम", _BAM_CANDIDATES)
    assert apply_document_veto(decision, document) == decision


def test_unreadable_document_and_ecn_record_give_different_reasons():
    # A caseworker reading the review file must be able to tell "this is an
    # election candidate" (a judgement) from "I could not check" (a retry).
    decision = resolve("राज बहादुर बम", _BAM_CANDIDATES)

    unverified = apply_document_veto(decision, None)
    vetoed = apply_document_veto(decision, {"identifier": [_ECN_ID]})

    assert "unavailable" in unverified.reason
    assert "ecn-candidate-id" not in unverified.reason
    assert "ecn-candidate-id" in vetoed.reason
    assert unverified.reason != vetoed.reason


def test_veto_never_upgrades_a_review_or_no_match_even_on_a_bad_document():
    # Fail-closed must not become fail-anything: a non-BIND stays exactly as
    # resolve() left it, whatever the document looks like.
    no_match = resolve("खगेन्द्र पराजुली", [])
    assert apply_document_veto(no_match, None) == no_match
    assert apply_document_veto(no_match, {}) == no_match


def test_resolve_still_binds_on_names_alone_and_takes_no_document():
    # Tasks 3 and 4 are reviewed and settled: the document veto is a separate
    # function and resolve() knows nothing about documents. Assert the signature,
    # not just the behaviour -- a document parameter creeping in here would move
    # I/O back into the pure layer.
    #
    # `candidates_complete` is a plain bool (did the caller's search run out of
    # results), so it does not reopen that door. The guarantee this test protects
    # is "nothing that implies I/O", asserted directly below rather than by an
    # exact list that any harmless addition breaks.
    params = list(inspect.signature(resolve).parameters)
    assert params == ["extracted_name", "candidates", "candidates_complete"]
    forbidden = ("document", "api", "client", "session", "fetch", "get", "url")
    assert not [p for p in params if any(word in p for word in forbidden)]

    decision = resolve("राज बहादुर बम", _BAM_CANDIDATES)
    assert decision.verdict == BIND
    assert decision.nes_id == "https://jawafdehi.org/entity/person/raj-bahadur-bam-318984"


# ---------------------------------------------------------------------------
# nec-candidate-id -- the ~6,743 ELECTED ward heads
# (docs/nes/sourcing/ward-chairs/RESULTS.md). The veto keyed on ecn-candidate-id
# alone until 2026-08-03 and let this whole population through. An elected
# official is likelier to appear in a CIAA case than a losing candidate, so this
# is the worse half of the hazard.
# ---------------------------------------------------------------------------

_NEC_ID = {"@type": "PropertyValue", "propertyID": "nec-candidate-id", "value": "194108"}


def test_nec_candidate_id_is_an_election_record():
    # person/tejnath-paudel-ward-51208-8: Ward Chairperson of ward 8, Badhaiyatal
    # Gaunpalika in BARDIYA, bound by a human as accused on a SOLUKHUMBU case.
    assert is_election_candidate_record({"identifier": [_NEC_ID]}) is True


def test_nec_marked_bind_is_downgraded_and_the_reason_names_the_marker():
    candidates = [{"id": "https://jawafdehi.org/entity/person/tejnath-paudel-ward-51208-8",
                   "title": {"ne": "तेजनाथ पौडेल"}}]
    decision = resolve("तेजनाथ पौडेल", candidates)
    assert decision.verdict == BIND

    vetoed = apply_document_veto(decision, {"identifier": [_NEC_ID]})

    assert vetoed.verdict == REVIEW
    assert vetoed.nes_id is None
    # A caseworker must be able to tell a losing candidate from a sitting ward head.
    assert "nec-candidate-id" in vetoed.reason


def test_both_markers_live_in_one_curated_set():
    # A third sourcing load adding a third marker should be a one-line change.
    assert ELECTION_RECORD_MARKERS == frozenset({"ecn-candidate-id", "nec-candidate-id"})


# ---------------------------------------------------------------------------
# The province veto. Many provincial bodies' stored titles do not name their
# province, and prod holds exactly ONE entity titled "वन तथा वातावरण मन्त्रालय" --
# Gandaki's (verified 2026-08-03: 305 search hits, one exact title.ne match). So a
# DRAFT district-forest case in Bara, which is in Madhesh province, matched it at
# 1.00 and the ambiguity veto could not fire, because the name really is unique in
# NES. Unique in NES, not unique in reality. Uniqueness is the whole argument:
# where a bare title IS duplicated the ambiguity veto already holds it. Found by a
# smoke run over unseen DRAFT cases.
#
# Pure: needs only the extracted name and the candidate IRI, both already in hand.
# No HTTP call, and resolve()'s signature is unchanged.
# ---------------------------------------------------------------------------

_GANDAKI_MOFESC = [
    {"id": "https://jawafdehi.org/entity/organization/government/provincial/gandaki/mofesc",
     "title": {"ne": "वन तथा वातावरण मन्त्रालय", "en": "Ministry of Forests and Environment"}},
]


def test_province_scoped_candidate_is_held_when_the_name_omits_the_province():
    decision = resolve("वन तथा वातावरण मन्त्रालय", _GANDAKI_MOFESC)

    assert decision.verdict == REVIEW
    assert decision.nes_id is None
    # Name the province, so a caseworker sees immediately what to check.
    assert "gandaki" in decision.reason
    # And keep the evidence for the review file.
    assert decision.candidates and decision.candidates[0][0] == 1.0


def test_province_scoped_candidate_binds_only_when_both_names_carry_the_province():
    """The allow-path, and it is narrower than it looks. Worth stating exactly.

    Adding the province to the EXTRACTED name alone does not open the path -- it
    closes it, one layer earlier. `गण्डकी` and `प्रदेश` are not middle particles, so
    against the province-less stored title `match_score` is 0.0 and the row is
    NO_MATCH before any veto runs. The province veto's allow-path therefore only
    fires when the STORED title carries the province too.

    The permissive branch is LIVE, not decoration. Three Lumbini bodies store the
    province in both titles and all three BIND at 1.00, in Devanagari and in
    English (verified against prod 2026-08-03):
    `provincial/lumbini/moeap` (`आर्थिक मामिला तथा योजना मन्त्रालय, लुम्बिनी प्रदेश`),
    `provincial/lumbini/molmac` and `provincial/lumbini/ppsc`. All three are in the
    frozen capture. So the shape that reviews is a bare name against a bare title,
    not "every province-scoped candidate".

    The allow-path's only key is a province token the LLM happened to emit. Nothing
    corroborates it against the case; if the extractor writes the wrong province
    into the name, this binds the wrong province's body. It costs 0 of the 39
    labelled binds either way.
    """
    # Province in the extracted name only -> the NAME match fails first.
    assert match_score("गण्डकी प्रदेश वन तथा वातावरण मन्त्रालय",
                       "वन तथा वातावरण मन्त्रालय") == 0.0
    assert resolve("गण्डकी प्रदेश वन तथा वातावरण मन्त्रालय",
                   _GANDAKI_MOFESC).verdict == NO_MATCH

    # Province in both -> the names match AND the veto is satisfied.
    decision = resolve(
        "गण्डकी प्रदेश वन तथा वातावरण मन्त्रालय",
        [{"id": "https://jawafdehi.org/entity/organization/government/provincial/gandaki/mofesc",
          "title": {"ne": "गण्डकी प्रदेश वन तथा वातावरण मन्त्रालय"}}],
    )
    assert decision.verdict == BIND
    assert decision.nes_id.endswith("/provincial/gandaki/mofesc")


def test_names_the_province_reads_both_scripts():
    # Reuses the same token machinery as every other comparison here, so a Latin
    # extraction reaches the Devanagari spellings and vice versa.
    assert names_the_province("गण्डकी प्रदेश स्वास्थ्य मन्त्रालय", "gandaki") is True
    assert names_the_province("Gandaki Province Ministry of Health", "gandaki") is True
    assert names_the_province("स्वास्थ्य मन्त्रालय", "gandaki") is False
    assert names_the_province("मधेश प्रदेश स्वास्थ्य मन्त्रालय", "gandaki") is False
    # वाग्मती is how NES stores it; बागमती is how people write it. Both accepted.
    assert names_the_province("वाग्मती प्रदेश स्वास्थ्य मन्त्रालय", "bagmati") is True
    assert names_the_province("बागमती प्रदेश स्वास्थ्य मन्त्रालय", "bagmati") is True


@pytest.mark.parametrize(
    ("iri", "expected"),
    [
        ("https://jawafdehi.org/entity/organization/government/provincial/gandaki/mofesc",
         "gandaki"),
        ("https://jawafdehi.org/entity/organization/government/provincial/madhesh/mohp",
         "madhesh"),
        # Not province-scoped -- these must NOT be touched.
        ("https://jawafdehi.org/entity/organization/government/body/nid", ""),
        ("https://jawafdehi.org/entity/location/localunit/adanchuli-gaunpalika-60306", ""),
        ("https://jawafdehi.org/entity/person/khusilala-saha-865cdc", ""),
        ("https://jawafdehi.org/entity/location/province/gandaki-np04", ""),
    ],
)
def test_asserted_province_only_fires_on_a_provincial_segment(iri, expected):
    assert asserted_province(iri) == expected


def test_localunit_binds_are_untouched_by_the_province_veto():
    # The regression that rules out the blunt fix. Vetoing every locality-scoped
    # IRI (/provincial|/district|/localunit/) would cost 7 of the 33 labelled-set
    # binds -- recall 0.846 -> 0.667 -- because for those 7 the locality IS the
    # entity's own name.
    decision = resolve(
        "अदानचुली गाउँपालिका",
        [{"id": "https://jawafdehi.org/entity/location/localunit/adanchuli-gaunpalika-60306",
          "title": {"ne": "अदानचुली गाउँपालिका"}}],
    )
    assert decision.verdict == BIND


def test_an_unrecognised_province_slug_fails_closed():
    # Nepal has seven provinces and all seven are curated, so an unknown slug
    # means NES changed. Unverifiable is REVIEW, never BIND -- same rule as the
    # unreadable-document branch of the document veto.
    decision = resolve(
        "स्वास्थ्य मन्त्रालय",
        [{"id": "https://jawafdehi.org/entity/organization/government/provincial/atlantis/moh",
          "title": {"ne": "स्वास्थ्य मन्त्रालय"}}],
    )
    assert decision.verdict == REVIEW
    assert "unrecognised province" in decision.reason


def test_all_seven_provinces_are_curated():
    # Derived from prod on 2026-08-03 by sweeping /api/search/ for
    # /provincial/<slug>/ segments; see the fix-round-4 report for the sweep.
    assert set(PROVINCE_NAME_FORMS) == {
        "koshi", "madhesh", "bagmati", "gandaki", "lumbini", "karnali", "sudurpashchim",
    }
    # Each carries at least one Devanagari and one Latin spelling, otherwise a
    # bilingual extraction can only match one of the two scripts.
    for province, forms in PROVINCE_NAME_FORMS.items():
        assert any(not f.isascii() for f in forms), province
        assert any(f.isascii() for f in forms), province
    # The IRI slug itself must be accepted: to_roman_colloquial does not
    # reproduce it (बागमती folds to "bagamati", not "bagmati").
    for province, forms in PROVINCE_NAME_FORMS.items():
        assert province in forms, f"{province} slug spelling missing from its own forms"


def test_province_veto_reason_is_distinct_from_the_document_veto_reasons():
    province = resolve("वन तथा वातावरण मन्त्रालय", _GANDAKI_MOFESC)
    ecn = apply_document_veto(
        resolve("राज बहादुर बम", _BAM_CANDIDATES), {"identifier": [_ECN_ID]})
    unreadable = apply_document_veto(resolve("राज बहादुर बम", _BAM_CANDIDATES), None)

    reasons = {province.reason, ecn.reason, unreadable.reason}
    assert len(reasons) == 3, "a caseworker cannot tell the three vetoes apart"
    assert "province" in province.reason
    assert "ecn-candidate-id" in ecn.reason
    assert "unavailable" in unreadable.reason


_LUMBINI_MOEAP = [
    {"id": "https://jawafdehi.org/entity/organization/government/provincial/lumbini/moeap",
     "title": {"ne": "आर्थिक मामिला तथा योजना मन्त्रालय, लुम्बिनी प्रदेश",
               "en": "Ministry of Finance and Planning, Lumbini Province"}},
]


@pytest.mark.parametrize("extracted", [
    "आर्थिक मामिला तथा योजना मन्त्रालय, लुम्बिनी प्रदेश",
    "Ministry of Finance and Planning, Lumbini Province",
])
def test_the_province_allow_path_is_live_against_a_real_prod_entity(extracted):
    # Not a hypothetical. provincial/lumbini/moeap stores the province in BOTH
    # titles (verified against prod 2026-08-03), so the veto is satisfied through
    # names_the_province and the row binds in either script. molmac and ppsc are
    # the same shape. This is what stops the rule being decoration.
    decision = resolve(extracted, _LUMBINI_MOEAP)
    assert decision.verdict == BIND
    assert decision.nes_id.endswith("/provincial/lumbini/moeap")


def test_province_forms_carry_no_redundant_romanisation():
    """Every form must earn its place: extra spellings only OPEN the allow-path.

    A Devanagari entry already matches an extracted Devanagari token through its
    own raw form, so that token's romanisation adds nothing. Anything here that is
    the fold of a Devanagari sibling is dead weight pointing the wrong way.
    """
    for province, forms in PROVINCE_NAME_FORMS.items():
        folds = {f for dev in forms if not dev.isascii()
                 for f in token_forms(dev) if f.isascii()}
        redundant = {f for f in forms if f.isascii() and f in folds and f != province}
        assert not redundant, (
            f"{province}: {sorted(redundant)} are romanisations of Devanagari forms "
            "already in the set, so they widen the allow-path for nothing")
        # Exactly one Latin form -- the IRI slug.
        assert {f for f in forms if f.isascii()} == {province}, province


# ---------------------------------------------------------------------------
# The truncation veto. It asks whether the window EDGE still sits inside the
# winner's relevance band -- not how many rows came back. A count-based test was
# wrong in both directions: it missed a full 50-row page that stopped early on
# relevance (where a same-name block really can straddle the boundary) and it
# fired on every full 200-row response whose block had genuinely ended.
# ---------------------------------------------------------------------------


_BAM = "https://jawafdehi.org/entity/person/raj-bahadur-bam-318984"


def _bam(score):
    return {"id": _BAM, "title": {"ne": "राज बहादुर बम"}, "score": score}


def _filler(n, score):
    """Rows that cannot match the name, only carry a relevance score."""
    return [{"id": f"https://jawafdehi.org/entity/person/padding-name-{i:06d}",
             "title": {"ne": f"असम्बन्धित नाम {i}"}, "score": score}
            for i in range(n)]


def test_a_window_edge_inside_the_winners_band_refuses_the_bind():
    # Descending relevance, and the LAST row fetched still scores as high as the
    # match -- so an equally-relevant same-name entity can sit just past the edge
    # where the ambiguity check cannot see it. This is the straddle that a row
    # count missed: real search stopped `संजय प्रसाद यादव` on a FULL 50-row page,
    # far under any cap, and that name's own duplicates score 130.981 and 130.564
    # rather than identically.
    candidates = [_bam(130.981)] + _filler(49, 130.981)
    decision = resolve("राज बहादुर बम", candidates, candidates_complete=False)
    assert decision.verdict == REVIEW
    assert decision.nes_id is None
    assert "truncated mid-block" in decision.reason


def test_a_window_edge_below_the_winners_band_still_binds():
    # The spurious-review case. `बुद्दीसागर सुवेदी` really returns 200 rows whose
    # edge (21.579) is below its top (25.134): paging stopped because the block
    # ended, so every tied candidate WAS seen. A count-based veto threw away every
    # bind on a full 200-row response.
    candidates = [_bam(25.134)] + _filler(199, 21.579)
    assert len(candidates) == 200
    assert resolve("राज बहादुर बम", candidates,
                   candidates_complete=False).verdict == BIND


def test_a_complete_result_set_is_never_vetoed_for_truncation():
    # A short page means the results ran out, so there is no edge to worry about
    # however the scores fall -- otherwise a 2-row exhaustive answer whose match
    # ranks last would be refused for nothing.
    candidates = [_bam(50.0), {"id": "https://jawafdehi.org/entity/person/other-1",
                               "title": {"ne": "असम्बन्धित नाम"}, "score": 50.0}]
    assert resolve("राज बहादुर बम", candidates,
                   candidates_complete=True).verdict == BIND
    # And completeness defaults to True, so a hand-built list is taken at its word.
    assert resolve("राज बहादुर बम", candidates).verdict == BIND


def test_an_unscored_candidate_list_does_not_crash_the_veto():
    # Search rows always carry `score`, but a hand-built list or a stub may not.
    # Absent scores read as 0.0, which makes edge == winner and refuses -- the
    # cautious direction.
    candidates = [{"id": _BAM, "title": {"ne": "राज बहादुर बम"}}]
    assert resolve("राज बहादुर बम", candidates,
                   candidates_complete=False).verdict == REVIEW


def test_candidate_name_forms_excludes_the_iri_slug():
    # A safety property, not tidiness. The slug romanises, so scoring it turned a
    # Devanagari-vs-Devanagari comparison into a cross-script one -- the one place
    # romanisation still bridges -- and bound कमल थापा to a कमला थापा entity at
    # 0.96 even though the two TITLES score 0.00.
    result = {"id": "https://jawafdehi.org/entity/person/kamala-thapa-111111",
              "title": {"ne": "कमला थापा", "en": "Kamala Thapa"}}
    assert candidate_name_forms(result) == ("कमला थापा", "Kamala Thapa")
    assert not [f for f in candidate_name_forms(result) if "-" in f]
    assert resolve("कमल थापा", [dict(result, score=190.0)]).verdict == NO_MATCH


# ---------------------------------------------------------------------------
# Script preference. These exist because `comparable_name_forms` shipped with
# `isascii()` as its script test and NO direct test, so three ways of defeating
# it went unnoticed until an adversarial pass reproduced a wrong bind.
# ---------------------------------------------------------------------------


_KAMALA = {"id": "https://jawafdehi.org/entity/person/kamala-thapa-111111",
           "score": 190.0}


@pytest.mark.parametrize("en_title, label", [
    ("Kamala Thapa", "clean ASCII"),
    ("Kamala\xa0Thapa", "non-breaking space"),
    ("Kamala Thapa‍", "trailing zero-width joiner"),
    ("Kamalа Thapa", "Cyrillic lookalike"),
])
def test_a_latin_english_title_cannot_bridge_to_a_devanagari_extraction(en_title, label):
    # `isascii()` asked "is EVERY character ASCII", so one non-ASCII character
    # anywhere made a Latin title claim to be Devanagari, pass the same-script
    # filter, and win the max() at 0.96 -- while the Devanagari title it should
    # have been compared against scored 0.00. All four spellings are realistic:
    # 18 of the 7,882 fixture rows have a non-ASCII title.en, and
    # person/rambabu-kalwar-273907 really does store a Cyrillic у.
    candidate = dict(_KAMALA, title={"ne": "कमला थापा", "en": en_title})
    assert resolve("कमल थापा", [candidate]).verdict == NO_MATCH, label


def test_has_devanagari_is_not_isascii():
    assert has_devanagari("कमला थापा")
    assert not has_devanagari("Kamala Thapa")
    # The whole point: these are NOT ASCII, but they are not Devanagari either.
    assert not has_devanagari("Kamala\xa0Thapa")
    assert not has_devanagari("Kamalа Thapa")
    assert not has_devanagari("")


def test_a_candidate_with_no_devanagari_name_goes_to_review_not_bind():
    # The comparison can only run through romanisation here, which cannot tell a
    # masculine name from its feminine form. 118 of the 7,882 fixture rows are in
    # this state (114 organisations, 4 people), so it is a real shape, and the
    # match may well be correct -- it just is not provable here.
    for title in ({"en": "Kamala Thapa"}, {"ne": "Kamala Thapa", "en": "Kamala Thapa"}):
        decision = resolve("कमल थापा", [dict(_KAMALA, title=title)])
        assert decision.verdict == REVIEW
        assert decision.nes_id is None
        assert "across scripts" in decision.reason


def test_the_devanagari_title_is_preferred_when_both_are_present():
    candidate = {"id": "https://jawafdehi.org/entity/person/anish-shrestha-219986",
                 "title": {"ne": "अनिष श्रेष्ठ", "en": "Anish Shrestha"}, "score": 190.0}
    assert comparable_name_forms("अनिष श्रेष्ठ", candidate) == ("अनिष श्रेष्ठ",)
    # A LATIN extraction still gets both, because cross-script is its only route.
    assert comparable_name_forms("Anish Shrestha", candidate) == (
        "अनिष श्रेष्ठ", "Anish Shrestha")
    # And it still binds that way -- this fix must not break Latin extractions.
    assert resolve("Anish Shrestha", [candidate]).verdict == BIND


# --------------------------------------------------------------------------
# A district name is one token, and that is not a weakness.
#
# The single-token veto exists because a lone token is a weak anchor in an open
# name space -- one surname, one word of a firm's name. Districts are the
# opposite: a closed gazetteer of 77, ingested with official codes
# (location/district/jhapa-np0104), where the whole name IS one token. Vetoing
# them means the location section can never bind anything, which is what the
# 2026-08-06 extraction hardening found. See
# docs/entity-extraction-hardening-design.md.
# --------------------------------------------------------------------------


def test_a_single_token_district_name_binds_its_gazetteer_entry():
    district = _candidate(
        "https://jawafdehi.org/entity/location/district/kathmandu-np0261",
        ne="काठमाडौं", en="Kathmandu")
    decision = resolve("काठमाडौं", [district])
    assert decision.verdict == BIND
    assert decision.nes_id.endswith("/location/district/kathmandu-np0261")


def test_a_single_token_province_name_binds_too():
    province = _candidate(
        "https://jawafdehi.org/entity/location/province/koshi-np01",
        ne="कोशी", en="Koshi")
    assert resolve("कोशी", [province]).verdict == BIND


def test_the_exemption_does_not_reach_the_bare_location_prefix():
    # `location/` holds countries and hand-added places with no gazetteer code.
    # A lone token there is the weak anchor the veto was written for -- and
    # `test_single_token_name_goes_to_review` pins the country case.
    place = _candidate("https://jawafdehi.org/entity/location/kathamadaum-98646f",
                       ne="काठमाडौं")
    decision = resolve("काठमाडौं", [place])
    assert decision.verdict == REVIEW
    assert "single token" in decision.reason


def test_the_exemption_does_not_reach_organisations():
    org = _candidate("https://jawafdehi.org/entity/organization/kathamadaum",
                     ne="काठमाडौं")
    assert resolve("काठमाडौं", [org]).verdict == REVIEW


def test_a_composite_name_is_still_vetoed_against_a_district():
    # The exemption is for the single-token rule only. `घरजग्गा सम्पत्ति -
    # काठमाडौं` describes seized property; matching it to Kathmandu district
    # would assert the description IS the district.
    district = _candidate(
        "https://jawafdehi.org/entity/location/district/kathmandu-np0261",
        ne="काठमाडौं", en="Kathmandu")
    decision = resolve("घरजग्गा सम्पत्ति - काठमाडौं", [district])
    assert decision.verdict != BIND
    # Refused on score here, before the veto is consulted -- but the veto is
    # still armed behind it, and it is the one the CREATE gate relies on.
    assert "composite" in _name_vetoes("घरजग्गा सम्पत्ति - काठमाडौं",
                                       district["id"])


def test_name_vetoes_without_a_candidate_still_refuses_a_single_token():
    # The create gate calls it with no candidate at all
    # (`enrich_related_entities._cannot_create`). Nothing to exempt against,
    # so every veto stays on.
    assert "single token" in _name_vetoes("काठमाडौं")
    assert _name_vetoes("काठमाडौं", None) == _name_vetoes("काठमाडौं")
