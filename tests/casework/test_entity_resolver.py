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
    MIN_BIND_SCORE,
    NO_MATCH,
    REVIEW,
    apply_document_veto,
    candidate_name_forms,
    is_election_candidate_record,
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


def test_slug_suffix_strip_still_removes_every_real_id_suffix():
    # Every real id suffix in the prod-verified fixtures used across this file:
    # person/anish-shrestha-219986, -285096, person/amkura-khatri-2de9b3,
    # organization/jilla-vana-karyalaya-f4548e, person/khusilala-saha-865cdc,
    # person/ncell-pvt-ltd-11aa22. The tightened regex must still strip all six.
    for suffix in ("219986", "285096", "2de9b3", "f4548e", "865cdc", "11aa22"):
        forms = candidate_name_forms(
            {"id": f"https://jawafdehi.org/entity/person/ram-thapa-{suffix}"}
        )
        assert forms == ("ram thapa",)


def test_slug_suffix_strip_does_not_eat_a_distinguishing_trailing_digit():
    # organization/chameliya-hydropower-2 -- the "2" distinguishes project 2
    # from project 1, it is not an id suffix. The old "\d+$" branch stripped
    # any trailing digit run, manufacturing a synthetic "chameliya hydropower"
    # form that scored 1.0 against "Chameliya Hydropower" and outranked both
    # real titles.
    forms = candidate_name_forms(
        {"id": "https://jawafdehi.org/entity/organization/chameliya-hydropower-2"}
    )
    assert forms == ("chameliya hydropower 2",)
    decision = resolve(
        "Chameliya Hydropower",
        [{"id": "https://jawafdehi.org/entity/organization/chameliya-hydropower-2"}],
    )
    assert decision.verdict != BIND


def test_slug_suffix_strip_does_not_eat_a_letters_only_name_segment():
    # person/ram-bahadur-baba -- "baba" is 4 characters that all happen to fall
    # in [0-9a-f], but it is a name segment, not an id: it has no digit. The
    # old "[0-9a-f]{4,8}$" branch stripped it anyway, manufacturing a synthetic
    # "ram bahadur" form that scored a false 1.0 against "Ram Bahadur".
    forms = candidate_name_forms(
        {"id": "https://jawafdehi.org/entity/person/ram-bahadur-baba"}
    )
    assert forms == ("ram bahadur baba",)
    decision = resolve(
        "Ram Bahadur",
        [{"id": "https://jawafdehi.org/entity/person/ram-bahadur-baba"}],
    )
    assert decision.verdict != BIND


# ---------------------------------------------------------------------------
# The vowel-length fold. NOT a bug report -- an asserted property. The module
# docstring's claim is "no edit-distance ALGORITHM", and that stays true; this
# is `to_roman_colloquial` folding Devanagari vowel length, which is deliberate
# and shared with four platform indexers. These tests pin the exact boundary so
# nobody has to rediscover it, and so a future widening fails loudly.
# ---------------------------------------------------------------------------


def test_vowel_length_folds_so_lhamu_variants_match():
    # Prod: person/mingna-lhamu-sherpa-328030 stores मिङमा ल्हामु शेर्पा while the
    # extractor emitted मिङमा ल्हमु शेर्पा. Both romanise to "lhamu".
    assert tokens_equal(name_tokens("ल्हमु")[0], name_tokens("ल्हामु")[0])
    assert match_score("मिङमा ल्हमु शेर्पा", "मिङमा ल्हामु शेर्पा") == pytest.approx(0.98)


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
# is_election_candidate_record -- the ECN veto predicate. Five of the 39
# first-pass binds across the labelled set were namesake ward candidates in the
# wrong district; this is what refuses them.
# ---------------------------------------------------------------------------

_ECN_ID = {"@type": "PropertyValue", "propertyID": "ecn-candidate-id", "value": "318984"}
_CBS_ID = {"@type": "PropertyValue", "propertyID": "cbs-local-unit-code", "value": "60306"}


def test_ecn_marker_in_a_list_is_detected():
    # person/raj-bahadur-bam-318984: an elected Ward Member in Kalikot, while
    # case 080-CR-0175 concerns an acting Chief Administrative Officer.
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
    # identifier" is an ANSWER, not a failed read: 25 of the 39 frozen documents
    # in tests/casework/fixtures/entity_documents.json are exactly
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
    # Tasks 3 and 4 are reviewed and settled: the veto is a separate function and
    # resolve() knows nothing about documents. Assert the signature, not just the
    # behaviour -- a document parameter creeping in here would move I/O back into
    # the pure layer.
    assert list(inspect.signature(resolve).parameters) == ["extracted_name", "candidates"]

    decision = resolve("राज बहादुर बम", _BAM_CANDIDATES)
    assert decision.verdict == BIND
    assert decision.nes_id == "https://jawafdehi.org/entity/person/raj-bahadur-bam-318984"
