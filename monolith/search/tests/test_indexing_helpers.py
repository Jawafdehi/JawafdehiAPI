"""Tests for the shared indexing helpers (name→titles, translit, delete 404)."""

from __future__ import annotations

from unittest.mock import MagicMock

from jawafdehi_shared.search.indexing import (
    delete_doc,
    has_devanagari,
    name_to_titles,
    title_translit,
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
