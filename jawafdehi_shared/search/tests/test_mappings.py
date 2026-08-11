"""Tests for the bilingual OpenSearch index settings & common mappings.

Pure-python (no Django, no live OpenSearch). Asserts the analysis chains and the
common document mapping are well-formed dicts carrying the bilingual analyzers
from the research config.
"""

from jawafdehi_shared.search.mappings import common_mappings, index_settings


def test_index_settings_is_wellformed_dict():
    s = index_settings()
    assert isinstance(s, dict)
    analysis = s["analysis"]
    assert set(analysis) >= {"char_filter", "filter", "analyzer"}


def test_required_bilingual_analyzers_present():
    analyzers = index_settings()["analysis"]["analyzer"]
    # The bilingual chains the research mandates: a Devanagari analyzer, a Roman
    # analyzer, the translit bridge, plus the mixed-script and exact analyzers.
    for name in ("devanagari", "roman", "translit_bridge", "mixed_script", "exact_normalized"):
        assert name in analyzers, name
        assert analyzers[name]["type"] == "custom"


def test_devanagari_chain_uses_icu_and_indic_normalization():
    analysis = index_settings()["analysis"]
    deva = analysis["analyzer"]["devanagari"]
    assert deva["tokenizer"] == "icu_tokenizer"
    assert "indic_normalization" in deva["filter"]
    assert "decimal_digit" in deva["filter"]
    # NFC char filter (icu_normalizer) must run pre-tokenize.
    assert "nfc_normalizer" in deva["char_filter"]
    assert analysis["char_filter"]["nfc_normalizer"]["type"] == "icu_normalizer"
    # No Nepali stemmer (research: Hindi stemmer corrupts Nepali recall).
    assert "hindi" not in deva["filter"]


def test_translit_bridge_uses_icu_transform_any_latin():
    analysis = index_settings()["analysis"]
    bridge = analysis["analyzer"]["translit_bridge"]
    assert "to_latin" in bridge["filter"]
    to_latin = analysis["filter"]["to_latin"]
    assert to_latin["type"] == "icu_transform"
    assert "Any-Latin" in to_latin["id"]


def test_strip_zerowidth_char_filter_present():
    cf = index_settings()["analysis"]["char_filter"]["strip_zerowidth"]
    assert cf["type"] == "mapping"
    assert any("200C" in m for m in cf["mappings"])
    assert any("200D" in m for m in cf["mappings"])


def test_common_mappings_has_expected_fields():
    props = common_mappings()["properties"]
    expected = {
        "iri",
        "type",
        "source_app",
        "case_type",
        "title_ne",
        "title_en",
        "title_translit",
        "body",
        "keywords",
        "identifiers",
        "created_at",
        "updated_at",
        "date",
        "date_bs",
        "weight",
        "raw",
    }
    assert expected <= set(props)


def test_weight_is_a_sortable_numeric():
    """A text mapping would make the ``featured`` sort silently meaningless."""
    props = common_mappings()["properties"]
    assert props["weight"]["type"] == "integer"


def test_case_type_is_keyword():
    props = common_mappings()["properties"]
    assert props["case_type"]["type"] == "keyword"


def test_title_fields_have_sortable_keyword_subfield():
    """Text titles aren't sortable; the ``.keyword`` subfield enables title sort."""
    props = common_mappings()["properties"]
    assert props["title_ne"]["fields"]["keyword"]["type"] == "keyword"
    assert props["title_en"]["fields"]["keyword"]["type"] == "keyword"


def test_iri_and_identifiers_are_keyword():
    props = common_mappings()["properties"]
    assert props["iri"]["type"] == "keyword"
    assert props["identifiers"]["type"] == "keyword"
    # Bikram Sambat carried verbatim as keyword, never coerced to a date.
    assert props["date_bs"]["type"] == "keyword"


def test_raw_is_disabled_object():
    raw = common_mappings()["properties"]["raw"]
    assert raw["type"] == "object"
    assert raw["enabled"] is False


def test_bilingual_title_analyzers():
    props = common_mappings()["properties"]
    assert props["title_ne"]["analyzer"] == "devanagari"
    assert props["title_en"]["analyzer"] == "roman"
    assert props["title_translit"]["analyzer"] == "translit_bridge"


def test_keywords_dual_indexed_keyword_and_text():
    kw = common_mappings()["properties"]["keywords"]
    assert kw["type"] == "keyword"
    assert kw["fields"]["text"]["type"] == "text"


def test_returns_fresh_copies():
    # Mutating one result must not affect the next.
    a = common_mappings()
    a["properties"]["iri"]["type"] = "MUTATED"
    assert common_mappings()["properties"]["iri"]["type"] == "keyword"

    s = index_settings()
    s["analysis"]["analyzer"]["devanagari"]["tokenizer"] = "MUTATED"
    assert index_settings()["analysis"]["analyzer"]["devanagari"]["tokenizer"] == "icu_tokenizer"
