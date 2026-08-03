"""Tests for the pure NES entity resolver (casework/entity_resolver.py).

Every string here is real: the extracted names come from the July A/B run logs
(work/2026-07-17-enricher-extraction/), the NES ids and stored names come from
prod reads. A wrong bind attaches a named person to a corruption case they had
nothing to do with, so the false-positive tests are the point of this file.
"""
import pytest

from casework.entity_resolver import (
    BIND,
    MIN_BIND_SCORE,
    NO_MATCH,
    REVIEW,
    match_score,
    name_tokens,
    normalise_name,
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
