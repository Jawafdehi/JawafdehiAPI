"""The stage builder and its merge rules. Pure dicts, no API."""

import pytest

from casework.case_stages import (
    STAGE_INITIAL,
    deciding_hearing,
    merge_trial_stages,
    reference_end,
    reference_start,
    trial_stage,
)

IRI = "https://jawafdehi.org/courtcase/special/079-CR-0151"


def _record(reg=None, hearings=(), status=None, iri=IRI):
    return {"court": "special", "number": "079-CR-0151", "iri": iri,
            "detail": {"registration_date_ad": reg, "case_status": status},
            "hearings": list(hearings), "parties": []}


def _hearing(date, status="फैसला", decision=""):
    return {"hearing_date_ad": date, "case_status": status,
            "decision_type": decision}


# ── reading one docket ───────────────────────────────────────────────────────

def test_the_start_is_that_dockets_own_registration_date():
    assert reference_start(_record(reg="2022-08-01")) == "2022-08-01"


def test_a_docket_with_no_registration_date_has_no_start():
    assert reference_start(_record()) == ""


def test_the_deciding_hearing_is_picked_by_date_not_list_position():
    record = _record(hearings=[_hearing("2024-06-04"), _hearing("2024-06-03")])
    assert deciding_hearing(record["hearings"])["hearing_date_ad"] == "2024-06-04"


def test_a_non_verdict_hearing_never_decides():
    record = _record(hearings=[_hearing("2024-06-04", status="आदेश")])
    assert deciding_hearing(record["hearings"]) is None
    assert reference_end(record) == ""


def test_the_end_falls_back_to_the_case_status_string():
    record = _record(status="फैसला (मिती: २०८१/०२/२२)")
    assert reference_end(record) == "2024-06-04"


# ── building one stage ───────────────────────────────────────────────────────

def test_a_decided_trial_becomes_a_complete_initial_stage():
    record = _record(reg="2022-08-01", hearings=[_hearing("2024-06-04")])
    assert trial_stage(record) == {
        "stage": STAGE_INITIAL, "start": "2022-08-01", "end": "2024-06-04",
        "courtcase_iri": IRI,
    }


def test_an_undecided_trial_carries_no_end_key_at_all():
    stage = trial_stage(_record(reg="2022-08-01"))
    assert stage == {"stage": STAGE_INITIAL, "start": "2022-08-01",
                     "courtcase_iri": IRI}
    assert "end" not in stage


# ── the merge rules ──────────────────────────────────────────────────────────

def test_a_case_with_no_stages_gets_the_proposal_appended():
    proposal = trial_stage(_record(reg="2022-08-01", hearings=[_hearing("2024-06-04")]))
    stages, changes = merge_trial_stages([], [proposal])
    assert stages == [proposal]
    assert any("added" in c for c in changes)


def test_a_stage_citing_this_docket_is_corrected_in_place():
    existing = [{"stage": STAGE_INITIAL, "start": "2022-07-30",
                 "courtcase_iri": IRI, "notes": "काठमाडौं"}]
    proposal = trial_stage(_record(reg="2022-08-01", hearings=[_hearing("2024-06-04")]))
    stages, changes = merge_trial_stages(existing, [proposal])
    assert len(stages) == 1
    assert stages[0]["start"] == "2022-08-01"
    assert stages[0]["end"] == "2024-06-04"
    # Keys this stage does not own survive untouched.
    assert stages[0]["notes"] == "काठमाडौं"
    assert any("2022-07-30" in c and "2022-08-01" in c for c in changes)


def test_an_absent_court_date_never_deletes_a_stored_one():
    # The docket has not decided. Blanking the stored end would flip a
    # concluded case back to ongoing.
    existing = [{"stage": STAGE_INITIAL, "start": "2022-08-01",
                 "end": "2024-06-04", "courtcase_iri": IRI}]
    stages, changes = merge_trial_stages(
        existing, [trial_stage(_record(reg="2022-08-01"))])
    assert stages[0]["end"] == "2024-06-04"
    assert any("kept" in c and "end" in c for c in changes)


def test_an_iri_less_initial_stage_is_adopted_when_there_is_one_trial():
    existing = [{"stage": STAGE_INITIAL, "start": "2022-07-30"}]
    proposal = trial_stage(_record(reg="2022-08-01", hearings=[_hearing("2024-06-04")]))
    stages, _ = merge_trial_stages(existing, [proposal])
    assert len(stages) == 1
    assert stages[0]["courtcase_iri"] == IRI
    assert stages[0]["start"] == "2022-08-01"


def test_an_iri_less_stage_is_not_adopted_when_there_are_two_trials():
    other = "https://jawafdehi.org/courtcase/special/079-CR-0152"
    existing = [{"stage": STAGE_INITIAL, "start": "2022-07-30"}]
    proposals = [trial_stage(_record(reg="2022-08-01")),
                 trial_stage(_record(reg="2022-09-01", iri=other))]
    stages, _ = merge_trial_stages(existing, proposals)
    assert len(stages) == 3
    assert stages[0] == {"stage": STAGE_INITIAL, "start": "2022-07-30"}


@pytest.mark.parametrize("stage", ["appeal", "review", "investigation", "other"])
def test_a_stage_of_another_kind_is_never_touched(stage):
    existing = [{"stage": stage, "start": "2024-07-01", "label": "x"}]
    proposal = trial_stage(_record(reg="2022-08-01"))
    stages, _ = merge_trial_stages(existing, [proposal])
    assert stages[0] == {"stage": stage, "start": "2024-07-01", "label": "x"}
    assert stages[1]["courtcase_iri"] == IRI


def test_an_initial_stage_citing_a_different_docket_is_left_alone():
    other = "https://jawafdehi.org/courtcase/supreme/080-CR-0081"
    existing = [{"stage": STAGE_INITIAL, "start": "2023-01-01",
                 "courtcase_iri": other}]
    stages, _ = merge_trial_stages(
        existing, [trial_stage(_record(reg="2022-08-01"))])
    assert len(stages) == 2
    assert stages[0]["courtcase_iri"] == other


def test_nothing_to_propose_returns_the_list_unchanged_and_no_changes():
    existing = [{"stage": STAGE_INITIAL, "start": "2022-08-01"}]
    stages, changes = merge_trial_stages(existing, [])
    assert stages == existing
    assert changes == []


def test_the_existing_list_is_never_mutated():
    existing = [{"stage": STAGE_INITIAL, "start": "2022-07-30",
                 "courtcase_iri": IRI}]
    snapshot = [dict(s) for s in existing]
    merge_trial_stages(existing, [trial_stage(_record(reg="2022-08-01"))])
    assert existing == snapshot


def test_a_rerun_over_its_own_output_changes_nothing():
    proposal = trial_stage(_record(reg="2022-08-01", hearings=[_hearing("2024-06-04")]))
    once, _ = merge_trial_stages([], [proposal])
    twice, changes = merge_trial_stages(once, [proposal])
    assert twice == once
    assert changes == []


def test_a_non_dict_entry_in_the_stored_list_is_dropped_not_carried():
    # The column is a JSONField; a hand-edited row can hold anything, and a
    # non-record would 422 the whole write on the way back out.
    stages, _ = merge_trial_stages(["junk", None], [])
    assert stages == []
