"""The court-record binder: dates, defendant resolution, and the patch it plans.

Coverage measured 2026-08-07 across the 307-case FY078/079 census: every court
case carries a registration date, 306 of 307 carry an end date (277 stated by
BOTH a deciding hearing and the case_status string, agreeing 277/277), and all
307 name at least one defendant.
"""

from casework.enrich_court_record import deciding_hearing, end_date, start_date


def _record(reg=None, hearings=(), status=None, parties=()):
    return {"court": "special", "number": "079-cr-0151",
            "detail": {"registration_date_ad": reg, "case_status": status},
            "hearings": list(hearings), "parties": list(parties)}


DECIDED = {"case_status": "फैसला", "decision_type": "सफाई",
           "hearing_date_ad": "2024-06-04", "hearing_date_bs": "2081-02-22"}
ADJOURNED = {"case_status": "स्थगित", "decision_type": "पक्षबाट",
             "hearing_date_ad": "2024-05-27", "hearing_date_bs": "2081-02-14"}


def test_start_date_is_the_court_registration_date():
    assert start_date([_record(reg="2023-06-22")]) == "2023-06-22"


def test_start_date_takes_the_earliest_across_references():
    records = [_record(reg="2024-01-01"), _record(reg="2023-06-22")]
    assert start_date(records) == "2023-06-22"


def test_start_date_is_empty_when_no_reference_carries_one():
    assert start_date([_record(reg=None)]) == ""


def test_deciding_hearing_is_picked_by_date_not_list_position():
    # Real ordering from special/079-CR-0151: the verdict sorts BEFORE an
    # earlier order in the API response.
    later = {**DECIDED, "hearing_date_ad": "2024-06-04"}
    earlier = {"case_status": "आदेश", "hearing_date_ad": "2024-06-03"}
    assert deciding_hearing([later, earlier]) == later


def test_deciding_hearing_takes_the_latest_of_several_verdict_rows():
    # Both rows pass the फैसला filter, so this can only pass by comparing
    # dates -- neither decided[0] nor decided[-1] would satisfy both asserts.
    first = {**DECIDED, "hearing_date_ad": "2024-01-01"}
    last = {**DECIDED, "hearing_date_ad": "2024-06-04"}
    assert deciding_hearing([first, last]) == last
    assert deciding_hearing([last, first]) == last


def test_deciding_hearing_ignores_non_deciding_rows():
    assert deciding_hearing([ADJOURNED]) is None


def test_end_date_comes_from_the_deciding_hearing():
    value, reason = end_date([_record(reg="2023-06-22", hearings=[ADJOURNED, DECIDED])])
    assert value == "2024-06-04"
    assert reason == ""


def test_end_date_falls_back_to_the_case_status_string():
    value, reason = end_date([_record(status="फैसला (मिती: २०८१/०२/२२)")])
    assert value == "2024-06-04"
    assert reason == ""


def test_an_open_case_gets_no_end_date():
    value, reason = end_date([_record(status="विचाराधीन", hearings=[ADJOURNED])])
    assert value == ""
    assert "no decision" in reason


def test_a_half_decided_case_gets_no_end_date():
    # Two references, only one decided. Writing an end date here would flip the
    # public status chip to "concluded" on a case still being heard.
    records = [_record(hearings=[DECIDED]), _record(status="विचाराधीन")]
    value, reason = end_date(records)
    assert value == ""
    assert "not every court reference" in reason


def test_end_date_takes_the_latest_when_every_reference_decided():
    records = [
        _record(hearings=[DECIDED]),
        _record(hearings=[{**DECIDED, "hearing_date_ad": "2025-01-15"}]),
    ]
    value, _ = end_date(records)
    assert value == "2025-01-15"
