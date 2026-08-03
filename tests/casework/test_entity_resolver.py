"""Tests for the pure NES entity resolver (casework/entity_resolver.py).

Every string here is real: the extracted names come from the July A/B run logs
(work/2026-07-17-enricher-extraction/), the NES ids and stored names come from
prod reads. A wrong bind attaches a named person to a corruption case they had
nothing to do with, so the false-positive tests are the point of this file.
"""
from casework.entity_resolver import (
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
