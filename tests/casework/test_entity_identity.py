"""Identity helpers for entities this package creates: slug + prefix rules.

Both are pure functions over strings. No API, no Django, no LLM -- which is why
they live apart from `enrich_related_entities` and are tested without a harness.
"""

import pytest

from casework.entity_identity import (
    MAX_SLUG_LENGTH,
    entity_slug,
    prefix_is_creatable,
)

# The live prefix list is `SELECT DISTINCT prefix` over existing entities
# (`entities/persistence.py:313`), so it is whatever is in use -- not a
# whitelist. A trimmed sample of the real 66.
LIVE = frozenset({
    "person",
    "location", "location/district", "location/province",
    "organization", "organization/contractor",
    "organization/government", "organization/government/department",
    "organization/government/district/dfo",
    "organization/government/provincial/sudurpashchim",
})


# --- entity_slug ----------------------------------------------------------


def test_slug_transliterates_a_devanagari_person_name():
    assert entity_slug("हेम राज बिष्ट") == "hema-raja-bishta"


def test_slug_does_not_double_the_name():
    # `to_roman_colloquial` returns TWO spellings joined by a space -- a strict
    # transliteration and a schwa-dropped one ("hema raja bishta hem raj bisht")
    # -- because it feeds a search index that wants to match either. Slugging
    # that output yields `hema-raja-bishta-hem-raj-bisht`.
    slug = entity_slug("हेम राज बिष्ट")
    assert slug.count("bisht") == 1
    assert "hem-raj" not in slug


def test_slug_drops_punctuation_and_parentheses():
    # Real extracted name from case 078-CR-0038: a maiden name in parentheses.
    assert entity_slug("रुबी जि.सी. (बिष्ट)") == "rubi-ji-si-bishta"


def test_slug_passes_latin_text_through_lowercased():
    assert entity_slug("UOB Singapore") == "uob-singapore"


def test_slug_never_starts_or_ends_with_a_hyphen():
    # The IRI grammar is `[a-z0-9][a-z0-9-]*` -- a leading hyphen is invalid and
    # a trailing one is silently ugly.
    slug = entity_slug("  ,वन निर्देशनालय,  ")
    assert not slug.startswith("-")
    assert not slug.endswith("-")
    assert "--" not in slug


def test_slug_is_capped_so_the_iri_stays_under_the_limit():
    # MAX_IRI_LENGTH is 300 and the prefix eats into it, so the slug is bounded
    # well below that rather than at it.
    long_name = "वैज्ञानिक वन ब्यवस्थापन अध्ययन उपसमिति " * 12
    assert len(entity_slug(long_name)) <= 80


def test_slug_returns_empty_when_nothing_survives():
    # A name of pure punctuation cannot become a valid slug. Returning "" lets
    # the caller skip and record it, rather than building an invalid IRI.
    assert entity_slug("!!! ??? ---") == ""
    assert entity_slug("") == ""


def test_slug_is_stable_across_calls():
    # Two runs over the same case must produce the same IRI, or a re-run creates
    # a duplicate entity instead of hitting the already-exists path.
    assert entity_slug("मानस नर्सरी, धनगढी") == entity_slug("मानस नर्सरी, धनगढी")


# --- prefix_is_creatable --------------------------------------------------


def test_prefix_already_in_use_is_creatable():
    assert prefix_is_creatable("organization/government/district/dfo", LIVE) is True


def test_new_leaf_on_an_existing_branch_is_creatable():
    # `organization/government` exists, so a forest-office category under it is
    # the hierarchy growing where it already has a trunk.
    assert prefix_is_creatable("organization/government/forest", LIVE) is True


def test_a_typo_in_the_trunk_is_not_creatable():
    # `organizaton/government` is in no live prefix, so this has no parent. This
    # is the case the rule exists for: an unchecked prefix is self-ratifying,
    # because /api/entity_prefixes reports whatever exists.
    assert prefix_is_creatable("organizaton/government/police", LIVE) is False


def test_a_brand_new_root_is_not_creatable():
    assert prefix_is_creatable("ministry/forest", LIVE) is False
    assert prefix_is_creatable("ministry", LIVE) is False


def test_an_existing_single_segment_prefix_is_creatable():
    assert prefix_is_creatable("person", LIVE) is True


@pytest.mark.parametrize("bad", [
    "",
    "Organization/Government",        # uppercase breaks the IRI grammar
    "organization//government",       # empty segment
    "organization/government/a/b/c",  # 5 segments; the grammar allows 4
    "organization/government/pol ice",
    None,
])
def test_a_malformed_prefix_is_never_creatable(bad):
    assert prefix_is_creatable(bad, LIVE) is False


def test_a_deeper_leaf_needs_its_immediate_parent_not_just_a_grandparent():
    # `organization/government` exists but `organization/government/revenue`
    # does not, so a child of the latter has no parent to hang from. Checking
    # any ancestor rather than the immediate parent would let a typo'd middle
    # segment through.
    assert prefix_is_creatable("organization/government/revenue/customs", LIVE) is False


# --------------------------------------------------------------------------
# The English name decides the slug.
#
# These firms are English names WRITTEN IN DEVANAGARI, so sounding them back
# out gives `phareshta-debhalapamenta-enda-indashtrija` for "Forest Development
# and Industries". The IRI is permanent, so that is not merely ugly: a
# caseworker adding the same firm by hand authors
# `forest-development-and-industries`, and NES ends up holding the company
# twice with nothing linking the two.
# --------------------------------------------------------------------------


def test_slug_prefers_the_english_name_when_the_extraction_supplies_one():
    slug = entity_slug("फरेष्ट डेभलपमेन्ट एण्ड इण्डष्ट्रिज",
                       name_en="Forest Development and Industries")
    assert slug == "forest-development-and-industries"


def test_slug_transliterates_when_no_english_name_is_supplied():
    # Unchanged behaviour: a genuinely Nepali name has no English form to
    # prefer, and transliteration is right for it.
    assert entity_slug("हेम राज विष्ट") == "hema-raja-vishta"


def test_slug_falls_back_to_transliteration_when_english_yields_nothing():
    # The model returned punctuation, or a string of nothing sluggable. Falling
    # back beats returning "" -- "" makes the caller skip a real entity.
    assert entity_slug("हेम राज विष्ट", name_en="!!!") == "hema-raja-vishta"


def test_slug_falls_back_when_the_english_name_is_blank():
    assert entity_slug("हेम राज विष्ट", name_en="") == "hema-raja-vishta"


def test_slug_falls_back_when_the_english_name_is_not_a_string():
    assert entity_slug("हेम राज विष्ट", name_en=123) == "hema-raja-vishta"


def test_an_english_slug_is_length_capped_like_any_other():
    long_en = "Global Wild Farming and Agroforestry and Timber and Nursery " \
              "and Plantation Private Limited Company"
    slug = entity_slug("ग्लोवल वाइल्ड फार्मिङ", name_en=long_en)
    assert len(slug) <= MAX_SLUG_LENGTH
    # Cut on a hyphen, so the slug never ends mid-word.
    assert not slug.endswith("-")
    assert long_en.lower().startswith(slug.split("-")[0])


def test_an_english_slug_is_stable_across_calls():
    # A re-run must find the entity it created, not mint a second one.
    args = ("ग्लोवल वाइल्ड फार्मिङ एण्ड एग्रोफरेष्ट्री प्रा.लि.",)
    kwargs = {"name_en": "Global Wild Farming and Agroforestry Pvt. Ltd."}
    assert entity_slug(*args, **kwargs) == entity_slug(*args, **kwargs)


def test_the_english_slug_drops_punctuation_the_iri_grammar_forbids():
    slug = entity_slug("विध मानेजमेन्ट प्रा.लि.",
                       name_en="Vidh Management Pvt. Ltd.")
    assert slug == "vidh-management-pvt-ltd"
