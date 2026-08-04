"""Tests for the shared indexing helpers (name→titles, translit, delete 404)."""

from __future__ import annotations

from unittest.mock import MagicMock

from jawafdehi_shared.search.indexing import (
    delete_doc,
    flatten_strings,
    has_devanagari,
    name_to_titles,
    title_translit,
    type_token,
)


def test_name_to_titles_language_map():
    assert name_to_titles({"ne": "नाम", "en": "Name"}) == ("नाम", "Name")


def test_name_to_titles_string_script_bucketing():
    assert name_to_titles("Kathmandu") == (None, "Kathmandu")
    ne, en = name_to_titles("काठमाडौं")
    assert ne == "काठमाडौं"
    assert en is None


def test_name_to_titles_np_alias_and_empty():
    assert name_to_titles({"np": "नाम"}) == ("नाम", None)
    assert name_to_titles(None) == (None, None)
    assert name_to_titles({"en": "   "}) == (None, None)


def test_has_devanagari():
    assert has_devanagari("नेपाल") is True
    assert has_devanagari("Nepal") is False


def test_title_translit_carries_romanization():
    out = title_translit("शेर बहादुर", None)
    assert out  # a non-empty romanized bridge string
    assert title_translit(None, None) is None


# ---------------------------------------------------------------------------
# flatten_strings / type_token
# ---------------------------------------------------------------------------
#
# These two were byte-identical private copies in entities/search_index.py and
# materials/search_index.py, and neither copy was ever tested directly — only
# through each app's build_doc. They are shared now, so a change here moves BOTH
# the NES entity index and the NGM material index at once; that is what these
# pin.


def test_flatten_strings_handles_every_json_ld_shape():
    assert flatten_strings("  Nepal  ") == ["Nepal"]
    # language map -> every value, dict order preserved
    assert flatten_strings({"ne": "नेपाल", "en": "Nepal"}) == ["नेपाल", "Nepal"]
    # lists and tuples recurse, and nest arbitrarily deep
    assert flatten_strings(["a", ("b",), [{"en": "c"}]]) == ["a", "b", "c"]


def test_flatten_strings_drops_empty_and_non_strings():
    # Blank/whitespace-only strings are dropped, NOT kept as "".
    assert flatten_strings(["a", "", "   ", None, 42, True, {}, []]) == ["a"]
    assert flatten_strings(None) == []
    # Duplicates are NOT deduped — callers that need that do it themselves
    # (build_doc's `if ident not in identifiers` guard depends on this).
    assert flatten_strings(["a", "a"]) == ["a", "a"]


def test_type_token_renders_at_type():
    assert type_token("Person") == "Person"
    # A multi-type document comma-joins, matching how the promoted
    # entity_type / material_type columns store it.
    assert type_token(["Person", "PublicOfficial"]) == "Person,PublicOfficial"
    # Absent @type is "" (a falsy token), never the string "None".
    assert type_token(None) == ""
    assert type_token([]) == ""


def test_delete_doc_swallows_404():
    client = MagicMock()
    err = Exception("not found")
    err.status_code = 404
    client.delete.side_effect = err
    # A 404 (already gone) is treated as success — no raise.
    delete_doc(client, "nes-entities", "iri")


def test_delete_doc_reraises_non_404():
    client = MagicMock()
    err = Exception("boom")
    err.status_code = 500
    client.delete.side_effect = err
    try:
        delete_doc(client, "nes-entities", "iri")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 500
    else:  # pragma: no cover
        raise AssertionError("expected re-raise on a non-404 error")
