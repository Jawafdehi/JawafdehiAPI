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
    """The CR-series forum rule, now scoped to the court it was written for.

    It originally matched ``NNN-CR-NNNN`` on ANY court, on the premise that a CR
    series means a CIAA prosecution. The corpus says otherwise: no high or district
    court has a CR series at all, and the Supreme Court's is a general criminal
    docket (SEXUAL_OFFENSE 869, HOMICIDE 762, CORRUPTION 665 in a 4k sample). The
    patanhc case this test used was synthetic.
    """
    c = _case(
        case_type="____ novel ____", court_id="special", case_number="081-CR-0009"
    )
    assert sv.court_case_public_visible(c) is True

    # Same shape, wrong court: an unmapped Supreme criminal case is NOT admitted
    # to a "curated corruption slice" on the strength of its number alone.
    other = _case(
        case_type="____ novel ____", court_id="supreme", case_number="081-CR-0009"
    )
    assert sv.court_case_public_visible(other) is False


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


class TestCorruptionForumIsCourtScoped:
    """``NNN-CR-NNNN`` is not a CIAA marker on its own.

    The Supreme Court's general criminal register uses the same shape (7,133 rows
    of homicide, forgery, cheque dishonour). Matching the number alone swept that
    whole docket into an index whose stated scope is a curated corruption slice.
    It stayed hidden only by accident — every Supreme row carried the coarse
    'फौजदारी', which maps to OTHER_CRIMINAL and is excluded as procedural — so the
    moment those rows were backfilled with their real charges, an estimated 5,093
    became public, ~1,200 of them homicide.
    """

    def test_supreme_homicide_is_not_in_the_corruption_forum(self):
        case = _case(court_id="supreme", case_number="081-CR-1641",
                     case_type="कर्तव्य ज्यान")
        assert sv.in_corruption_forum(case) is False
        assert sv.court_case_public_visible(case) is False

    def test_special_court_cr_still_is(self):
        case = _case(court_id="special", case_number="076-CR-0294",
                     case_type="नक्कली प्रमाण पत्र")
        assert sv.in_corruption_forum(case) is True

    def test_a_supreme_corruption_appeal_is_still_shown(self):
        """The fix costs nothing: SHOW_CODES carries it on the code axis."""
        case = _case(court_id="supreme", case_number="071-CR-0306",
                     case_type="भ्रष्टाचार")
        assert sv.in_corruption_forum(case) is False
        assert sv.court_case_public_visible(case) is True

    def test_a_district_criminal_docket_is_not_a_corruption_forum(self):
        case = _case(court_id="kathmandudc", case_number="080-CR-0012",
                     case_type="कर्तव्य ज्यान")
        assert sv.court_case_public_visible(case) is False

    def test_the_sensitive_floor_is_unaffected(self):
        case = _case(court_id="special", case_number="076-CR-0294",
                     case_type="जवरजस्ती करणी")
        assert sv.court_case_public_visible(case) is False
