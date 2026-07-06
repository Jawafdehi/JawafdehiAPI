"""Tests for the CaseCourtCaseReference join and the court_cases property.

Court-case references are stored as canonical court-case @id IRIs
(``https://<base>/courtcase/<court>/<case_number>``, lowercased) on the
CaseCourtCaseReference join, and the IRI is the ONLY accepted reference form
(mirroring ``nes_id``/``material_iri``). ``Case.court_cases`` is a settable
property over that join. There is NO other reference format anywhere —
admin rows and API payloads are IRIs; importers build IRIs from their source
(court, number) parts via ``courtcase_iri_from_parts``.
"""

import pytest
from django.core.exceptions import ValidationError

from cases.models import Case, CaseCourtCaseReference, CaseState, CaseType
from cases.services.priority_case_loader import filter_by_priority
from cases.validators import (
    courtcase_iri_from_parts,
    parse_courtcase_ref,
    validate_court_cases,
    validate_courtcase_iri,
)
from review import ngm_client

SPECIAL_0111 = "https://jawafdehi.org/courtcase/special/080-cr-0111"
SUPREME_0111 = "https://jawafdehi.org/courtcase/supreme/080-cr-0111"
SPECIAL_0007 = "https://jawafdehi.org/courtcase/special/080-cr-0007"
SUPREME_0007 = "https://jawafdehi.org/courtcase/supreme/080-cr-0007"


def _make_case(**kwargs) -> Case:
    defaults = dict(
        title="Court ref test case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def test_parse_courtcase_ref_parses_iris_only():
    assert parse_courtcase_ref(SPECIAL_0111) == ("special", "080-cr-0111")
    # The legacy short form is NOT a stored reference form.
    assert parse_courtcase_ref("special:080-CR-0111") is None
    assert parse_courtcase_ref(None) is None
    assert parse_courtcase_ref("") is None
    assert parse_courtcase_ref("https://example.org/entity/person/foo") is None
    assert parse_courtcase_ref(["x"]) is None


def test_validate_courtcase_iri_strict():
    validate_courtcase_iri(SPECIAL_0111)
    # Short form, foreign host, unknown court, uppercase grammar: all rejected.
    with pytest.raises(ValidationError):
        validate_courtcase_iri("special:080-CR-0111")
    with pytest.raises(ValidationError):
        validate_courtcase_iri("https://elsewhere.example/courtcase/special/080-cr-0111")
    with pytest.raises(ValidationError):
        validate_courtcase_iri("https://jawafdehi.org/courtcase/not-a-real-court/123")
    with pytest.raises(ValidationError):
        validate_courtcase_iri("https://jawafdehi.org/courtcase/special/080-CR-0111")


def test_courtcase_iri_from_parts_builds_canonical_iris():
    # For producers whose source data arrives as separate (court, number)
    # fields, e.g. the CIAA importer. There is no string input format.
    assert courtcase_iri_from_parts("special", "080-CR-0111") == SPECIAL_0111
    assert courtcase_iri_from_parts("Supreme", "078-wc-0123") == (
        "https://jawafdehi.org/courtcase/supreme/078-wc-0123"
    )
    with pytest.raises(ValidationError):
        courtcase_iri_from_parts("not-a-real-court", "123")
    with pytest.raises(ValidationError):
        courtcase_iri_from_parts("special", "")
    with pytest.raises(ValidationError):
        courtcase_iri_from_parts("special", "no spaces allowed")


def test_validate_court_cases_iri_list_only():
    validate_court_cases([])
    validate_court_cases([SPECIAL_0111, SUPREME_0111])
    with pytest.raises(ValidationError):
        validate_court_cases(SPECIAL_0111)  # string, not list
    with pytest.raises(ValidationError):
        validate_court_cases(["special:080-CR-0111"])  # short form rejected


# ---------------------------------------------------------------------------
# Model property + join sync
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_with_iris_stores_join_rows():
    case = _make_case(court_cases=[SPECIAL_0111, SUPREME_0111])

    assert case.court_cases == [SPECIAL_0111, SUPREME_0111]
    rows = list(case.courtcase_references.all())
    assert [r.courtcase_iri for r in rows] == [SPECIAL_0111, SUPREME_0111]
    assert [r.ordinal for r in rows] == [0, 1]

    fresh = Case.objects.get(pk=case.pk)
    assert fresh.court_cases == [SPECIAL_0111, SUPREME_0111]


@pytest.mark.django_db
def test_setter_deduplicates():
    case = _make_case(court_cases=[SPECIAL_0111, SPECIAL_0111])
    assert case.court_cases == [SPECIAL_0111]
    assert case.courtcase_references.count() == 1


@pytest.mark.django_db
def test_reassignment_replaces_join_rows():
    case = _make_case(court_cases=[SPECIAL_0111])
    case.court_cases = [SUPREME_0111]
    case.save()

    fresh = Case.objects.get(pk=case.pk)
    assert fresh.court_cases == [SUPREME_0111]
    assert CaseCourtCaseReference.objects.filter(case=case).count() == 1


@pytest.mark.django_db
def test_resaving_same_refs_is_a_noop_on_the_join():
    # Row identity (and thus created_at provenance + the audit trail) must
    # survive saves that don't change the reference list.
    case = _make_case(court_cases=[SPECIAL_0111])
    row_id = case.courtcase_references.get().id
    case.court_cases = [SPECIAL_0111]
    case.save()
    assert case.courtcase_references.get().id == row_id


@pytest.mark.django_db
def test_assign_none_clears_references():
    case = _make_case(court_cases=[SPECIAL_0111])
    case.court_cases = None
    case.save()
    assert Case.objects.get(pk=case.pk).court_cases == []


def test_setter_rejects_non_iri_refs():
    case = Case(title="x", case_type=CaseType.CORRUPTION)
    with pytest.raises(ValidationError):
        case.court_cases = ["special:080-CR-0111"]  # short form: IRIs only
    with pytest.raises(ValidationError):
        case.court_cases = ["not-a-real-court:123"]
    with pytest.raises(ValidationError):
        case.court_cases = SPECIAL_0111  # string, not list


@pytest.mark.django_db
def test_slug_generated_from_courtcase_iri():
    # Slugs must start with a letter, so a number-led base gets the "case-"
    # prefix (pre-existing rule); the case number still leads the slug.
    case = _make_case(court_cases=[SPECIAL_0111])
    assert case.slug.startswith("case-080-cr-0111-")


# ---------------------------------------------------------------------------
# Priority-case filtering (join lookup, vendor-agnostic)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_filter_by_priority_matches_special_and_supreme_via_join():
    special = _make_case(title="special ref", court_cases=[SPECIAL_0007])
    supreme = _make_case(title="supreme ref", court_cases=[SUPREME_0007])
    other = _make_case(
        title="other ref",
        court_cases=["https://jawafdehi.org/courtcase/special/081-cr-9999"],
    )
    none = _make_case(title="no refs")

    matched = filter_by_priority(Case.objects.all(), ["080-CR-0007"])
    ids = set(matched.values_list("id", flat=True))
    assert special.id in ids
    assert supreme.id in ids
    assert other.id not in ids
    assert none.id not in ids


@pytest.mark.django_db
def test_filter_by_priority_distinct_on_multiple_matching_refs():
    case = _make_case(court_cases=[SPECIAL_0007, SUPREME_0007])
    matched = filter_by_priority(Case.objects.all(), ["080-CR-0007"])
    assert list(matched.values_list("id", flat=True)) == [case.id]


# ---------------------------------------------------------------------------
# Review-pipeline ref parsing (dict-shaped cases from the API)
# ---------------------------------------------------------------------------


def test_ngm_client_parse_court_ref_is_iri_only():
    assert ngm_client.parse_court_ref(SPECIAL_0111) == ("special", "080-cr-0111")
    # The colon spelling is fully retired — nothing accepts it.
    assert ngm_client.parse_court_ref("special:080-CR-0111") is None
    assert ngm_client.parse_court_ref("https://x/entity/person/foo") is None
    assert ngm_client.parse_court_ref("special:../../etc") is None


def test_ngm_client_court_refs_for_case_passes_iris_through():
    case = {"court_cases": [SPECIAL_0111, "special:080-CR-0111", "garbage"]}
    assert ngm_client.court_refs_for_case(case) == [SPECIAL_0111]
