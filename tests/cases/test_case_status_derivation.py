"""``Case.status`` -- derived from stages plus entity outcomes, with an override."""

import pytest

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseStatus,
    CaseTrack,
    CaseType,
    RelationshipOutcome,
    RelationshipType,
    StatusOverride,
)


def _case(**kwargs) -> Case:
    defaults = dict(
        title="Status case", offence_type=CaseType.CORRUPTION, state=CaseState.DRAFT
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _accused(case, outcome, name="one"):
    return CaseEntityRelationship.objects.create(
        case=case,
        nes_id=f"https://jawafdehi.org/entity/person/{name}",
        relationship_type=RelationshipType.ACCUSED,
        outcome=outcome,
    )


@pytest.mark.django_db
def test_rule_1_an_override_wins_over_everything():
    case = _case(
        status_override=StatusOverride.WITHDRAWN,
        dates={"stages": [{"stage": "initial", "start": "2024-02-25"}]},
    )
    assert case.status == StatusOverride.WITHDRAWN


@pytest.mark.django_db
def test_rule_2_an_open_court_stage_is_ongoing():
    case = _case(dates={"stages": [{"stage": "initial", "start": "2024-02-25"}]})
    _accused(case, RelationshipOutcome.ACQUITTED)
    assert case.status == CaseStatus.ONGOING


@pytest.mark.django_db
def test_rule_2_a_pending_appeal_keeps_a_decided_trial_ongoing():
    """The 22 published cases with a Special Court verdict and a live CIAA
    appeal: every recorded outcome is terminal, and the case is not over."""
    case = _case(
        dates={
            "stages": [
                {"stage": "initial", "start": "2024-02-25", "end": "2024-05-22"},
                {"stage": "appeal", "start": "2025-04-11"},
            ]
        }
    )
    _accused(case, RelationshipOutcome.ACQUITTED)
    assert case.status == CaseStatus.ONGOING


@pytest.mark.django_db
def test_rule_3_a_remand_is_ongoing_before_the_new_stage_is_registered():
    case = _case(
        dates={"stages": [{"stage": "appeal", "start": "2024-01-01", "end": "2025-01-01"}]}
    )
    _accused(case, RelationshipOutcome.REMANDED)
    assert case.status == CaseStatus.ONGOING


@pytest.mark.django_db
def test_rule_4_a_closed_court_stage_with_terminal_outcomes_is_concluded():
    case = _case(
        dates={"stages": [{"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}]}
    )
    _accused(case, RelationshipOutcome.ACQUITTED, "a")
    _accused(case, RelationshipOutcome.CONVICTED, "b")
    assert case.status == CaseStatus.CONCLUDED


@pytest.mark.django_db
def test_rule_4_a_charged_defendant_blocks_concluded():
    case = _case(
        dates={"stages": [{"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}]}
    )
    _accused(case, RelationshipOutcome.ACQUITTED, "a")
    _accused(case, RelationshipOutcome.CHARGED, "b")
    assert case.status != CaseStatus.CONCLUDED


@pytest.mark.django_db
def test_rule_5_an_open_investigation_alone_is_under_investigation():
    case = _case(dates={"stages": [{"stage": "investigation", "start": "2023-01-12"}]})
    assert case.status == CaseStatus.UNDER_INVESTIGATION


@pytest.mark.django_db
def test_rule_6_a_case_with_no_stages_is_others():
    assert _case().status == CaseStatus.OTHERS


@pytest.mark.django_db
def test_a_closed_investigation_with_no_court_stage_is_not_under_investigation():
    case = _case(
        dates={
            "stages": [
                {"stage": "investigation", "start": "2023-01-12", "end": "2024-02-25"}
            ]
        }
    )
    assert case.status == CaseStatus.OTHERS


# ── case_track ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_case_track_defaults_to_null_rather_than_a_guess():
    """The ~2,900 drafts have no sources to classify from; null is honest and
    queryable, a wrong inference is neither."""
    assert _case().case_track is None


@pytest.mark.django_db
def test_case_track_stores_the_route_into_court():
    case = _case(case_track=CaseTrack.PUBLIC_PROSECUTOR)
    case.refresh_from_db()
    assert case.case_track == CaseTrack.PUBLIC_PROSECUTOR


def test_the_track_vocabulary_is_the_six_routes():
    assert set(CaseTrack.values) == {
        "ciaa",
        "money_laundering",
        "public_prosecutor",
        "writ",
        "arbitration",
        "other",
    }
