"""The importer flags cases whose NGM defendant roster is truncated.

NGM's Special-Court defendant parse is frequently incomplete versus the court's
own stated total (``"<lead> समेत N"`` or a bare ``"समेत"``). The guard records
that in ``missing_details`` so a case is not published with a truncated accused
list. The check compares against what NGM PARSED (not the NES-id-resolved subset)
and is advisory: a broken NGM read/parse skips it, never fatal.
"""

import pytest

from cases.models import CaseType
from cases.services.ciaa_draft_case_service import CIAADraftCaseService
from tests.conftest import create_case_with_entities

NGM_DETAILS = "cases.services.ngm_court_records.get_court_case_details"
COURT_CASE = {"court": "special", "case_no": "081-CR-0001"}


def _cell(defendant):
    return lambda court, case_no: {"case": {"defendant": defendant}}


def _draft_case():
    return create_case_with_entities(
        title="Truncation Guard Case",
        alleged_entities=["https://jawafdehi.org/entity/person/test-accused-1"],
        case_type=CaseType.CORRUPTION,
    )


@pytest.mark.django_db
def test_flag_when_stated_total_exceeds_parsed(monkeypatch):
    monkeypatch.setattr(NGM_DETAILS, _cell("प्रमुख प्रतिवादी समेत 5"))
    case = _draft_case()
    svc = CIAADraftCaseService()
    svc._flag_truncated_roster(COURT_CASE, case, parsed_count=2, bound_count=2)

    case.refresh_from_db()
    assert "ACCUSED LIST INCOMPLETE" in (case.missing_details or "")
    assert svc.stats["cases_flagged_truncated"] == 1


@pytest.mark.django_db
def test_flag_when_bare_samet_with_trailing_punctuation(monkeypatch):
    # A trailing danda after "समेत" must not defeat bare-samet detection.
    monkeypatch.setattr(NGM_DETAILS, _cell("प्रमुख प्रतिवादी समेत।"))
    case = _draft_case()
    svc = CIAADraftCaseService()
    svc._flag_truncated_roster(COURT_CASE, case, parsed_count=30, bound_count=30)

    case.refresh_from_db()
    assert "ACCUSED LIST INCOMPLETE" in (case.missing_details or "")


@pytest.mark.django_db
def test_no_flag_when_roster_complete(monkeypatch):
    monkeypatch.setattr(NGM_DETAILS, _cell("प्रमुख प्रतिवादी समेत 2"))
    case = _draft_case()
    before = case.missing_details
    svc = CIAADraftCaseService()
    svc._flag_truncated_roster(COURT_CASE, case, parsed_count=2, bound_count=2)

    case.refresh_from_db()
    assert case.missing_details == before
    assert svc.stats["cases_flagged_truncated"] == 0


@pytest.mark.django_db
def test_no_flag_when_parsed_matches_stated_despite_nes_skips(monkeypatch):
    # Complete roster (parsed == stated) but 2 defendants lacked a NES id, so
    # bound_count < parsed_count. This is a resolution gap, NOT source
    # truncation, and must not be mislabelled.
    monkeypatch.setattr(NGM_DETAILS, _cell("प्रमुख प्रतिवादी समेत 5"))
    case = _draft_case()
    before = case.missing_details
    svc = CIAADraftCaseService()
    svc._flag_truncated_roster(COURT_CASE, case, parsed_count=5, bound_count=3)

    case.refresh_from_db()
    assert case.missing_details == before
    assert svc.stats["cases_flagged_truncated"] == 0


@pytest.mark.django_db
def test_guard_skips_when_ngm_unavailable(monkeypatch):
    def _boom(court, case_no):
        raise RuntimeError("ngm read plane down")

    monkeypatch.setattr(NGM_DETAILS, _boom)
    case = _draft_case()
    before = case.missing_details
    svc = CIAADraftCaseService()
    # Must neither raise nor flag when the NGM plane is unreachable.
    svc._flag_truncated_roster(COURT_CASE, case, parsed_count=2, bound_count=2)

    case.refresh_from_db()
    assert case.missing_details == before
    assert svc.stats["cases_flagged_truncated"] == 0
