"""Unit tests for ``courts.search_visibility`` — the public-search show/hide gate.

Pure-function tests over lightweight fake cases + the real committed
``case_type_codes.json.gz`` map; no DB needed (the published-reference set is
patched in).
"""

from types import SimpleNamespace

import pytest

from courts import search_visibility as sv


def _case(
    *,
    case_type=None,
    court_id="patanhc",
    case_number="081-CI-0001",
    is_deleted=False,
    iri="https://jawafdehi.org/courtcase/patanhc/081-ci-0001",
):
    return SimpleNamespace(
        case_type=case_type,
        court_id=court_id,
        case_number=case_number,
        is_deleted=is_deleted,
        iri=iri,
    )


@pytest.fixture(autouse=True)
def _empty_published(monkeypatch):
    # Default: no PUBLISHED references (patched, so no DB hit). Overridden per-test.
    monkeypatch.setattr(sv, "_published_iris", frozenset())


def test_case_type_code_known_and_unknown():
    assert sv.case_type_code("भ्रष्टाचार") == "CORRUPTION"
    assert sv.case_type_code("सम्बन्ध विच्छेद") == "DIVORCE"
    assert sv.case_type_code("____ not a real case type ____") is None
    assert sv.case_type_code(None) is None


def test_is_cr_series():
    assert sv.is_cr_series("081-CR-0081")
    assert not sv.is_cr_series("081-CI-0081")
    assert not sv.is_cr_series(None)


def test_show_code_is_visible():
    assert sv.court_case_public_visible(_case(case_type="भ्रष्टाचार")) is True


def test_private_code_is_hidden():
    # लेनदेन → MONEYLENDING_DEBT (HIDE) in an ordinary court.
    assert sv.court_case_public_visible(_case(case_type="लेनदेन")) is False


def test_sensitive_floor_overrides_forum():
    # DIVORCE is sensitive → hidden even in the Special Court forum.
    c = _case(case_type="सम्बन्ध विच्छेद", court_id="special")
    assert sv.case_type_code(c.case_type) == "DIVORCE"
    assert sv.court_case_public_visible(c) is False


def test_forum_shows_nonprocedural_unknown():
    # An unmapped case_type in the Special Court (not procedural) → shown by forum.
    c = _case(case_type="____ novel charge ____", court_id="special")
    assert sv.case_type_code(c.case_type) is None
    assert sv.court_case_public_visible(c) is True


def test_forum_hides_procedural():
    # निवेदन → MISC_PETITION (procedural): a special-court petition stays hidden.
    c = _case(case_type="निवेदन", court_id="special")
    assert sv.case_type_code(c.case_type) == "MISC_PETITION"
    assert sv.court_case_public_visible(c) is False


def test_cr_series_forum_shows():
    c = _case(
        case_type="____ novel ____", court_id="patanhc", case_number="081-CR-0009"
    )
    assert sv.court_case_public_visible(c) is True


def test_is_deleted_is_hidden():
    c = _case(case_type="भ्रष्टाचार", is_deleted=True)
    assert sv.court_case_public_visible(c) is False


def test_published_link_shows_otherwise_hidden(monkeypatch):
    iri = "https://jawafdehi.org/courtcase/patanhc/081-ci-0777"
    c = _case(case_type="लेनदेन", case_number="081-CI-0777", iri=iri)
    assert sv.court_case_public_visible(c) is False
    monkeypatch.setattr(sv, "_published_iris", frozenset({iri}))
    assert sv.court_case_public_visible(c) is True


def test_published_link_does_not_override_sensitive(monkeypatch):
    iri = "https://jawafdehi.org/courtcase/patanhc/081-ci-0888"
    c = _case(case_type="सम्बन्ध विच्छेद", case_number="081-CI-0888", iri=iri)
    monkeypatch.setattr(sv, "_published_iris", frozenset({iri}))
    assert sv.court_case_public_visible(c) is False
