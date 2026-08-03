"""Tests for the pure NES entity resolver (casework/entity_resolver.py).

Every string here is real: the extracted names come from the July A/B run logs
(work/2026-07-17-enricher-extraction/), the NES ids and stored names come from
prod reads. A wrong bind attaches a named person to a corruption case they had
nothing to do with, so the false-positive tests are the point of this file.
"""
import pytest

from casework.entity_resolver import (
    MIN_BIND_SCORE,
    match_score,
    name_tokens,
    normalise_name,
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
    assert match_score("Shrestha Anish", "अनिष श्रेष्ठ") >= MIN_BIND_SCORE


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
