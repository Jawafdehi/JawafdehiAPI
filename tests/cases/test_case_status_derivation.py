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


def _ungraded_accused(case, name):
    """An ACCUSED bind whose ``outcome`` is really NULL.

    ``bulk_create`` skips ``save()``, which is the only thing that would
    normalize it to CHARGED -- the same path a legacy row took.
    """
    return CaseEntityRelationship.objects.bulk_create(
        [
            CaseEntityRelationship(
                case=case,
                nes_id=f"https://jawafdehi.org/entity/person/{name}",
                relationship_type=RelationshipType.ACCUSED,
                outcome=None,
            )
        ]
    )[0]


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
def test_a_null_outcome_on_an_accused_bind_blocks_concluded():
    """A partly-graded roster is not a decided case.

    ``save()`` normalizes a missing outcome to CHARGED, so this shape does not
    arrive through the ORM -- but the ``outcome_only_on_accused`` CHECK
    constraint permits it (it only forbids an outcome on a NON-accused bind),
    so a bulk write or a row predating that normalization can hold it. Reading
    CONCLUDED off the graded subset would badge the public card "Resolved"
    over named people with no recorded verdict.
    """
    case = _case(
        dates={"stages": [{"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}]}
    )
    _accused(case, RelationshipOutcome.CONVICTED, "graded")
    _ungraded_accused(case, "ungraded")

    assert case.status != CaseStatus.CONCLUDED


@pytest.mark.django_db
def test_a_null_outcome_does_not_make_a_case_ongoing_either():
    """It is not decided, but nothing says a court is still sitting."""
    case = _case(
        dates={"stages": [{"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}]}
    )
    _accused(case, RelationshipOutcome.CONVICTED, "graded")
    _ungraded_accused(case, "ungraded")

    assert case.status == CaseStatus.OTHERS


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


# ── the search index reads the property, not a second copy of the rules ──────


def test_the_index_down_maps_every_status_the_model_can_produce():
    """Drift guard. Adding a lifecycle to ``CaseStatus``/``StatusOverride``
    without deciding its legacy bucket must fail HERE, not silently dump the new
    value into the deployed SPA's three-value facet."""
    from cases.search_index import LEGACY_CASE_STATUS

    assert set(LEGACY_CASE_STATUS) == set(CaseStatus.values) | set(
        StatusOverride.values
    )


@pytest.mark.django_db
def test_the_index_doc_carries_the_real_derived_status_and_stage_fields():
    """End-to-end over a real ``Case``: the indexer reads the ``status``
    PROPERTY (a DB read for the accused binds), not a private re-derivation."""
    from cases.search_index import build_indexed_doc

    case = _case(
        state=CaseState.PUBLISHED,
        case_track=CaseTrack.CIAA,
        dates={"stages": [{"stage": "initial", "start": "2024-02-25"}]},
    )
    doc = build_indexed_doc(case)

    assert doc["status"] == CaseStatus.ONGOING
    assert doc["case_status"] == "ongoing"
    assert doc["case_track"] == "ciaa"
    assert doc["proceedings_started_on"] == "2024-02-25"
    assert "proceedings_decided_on" not in doc
    # The archive sort key follows the proceeding start.
    assert doc["date"] == "2024-02-25"
    # The stage list is display-only, never an indexed field.
    assert doc["raw"]["card"]["stages"] == [
        {"stage": "initial", "start": "2024-02-25"}
    ]
    assert "dates" not in doc


@pytest.mark.django_db
def test_a_concluded_case_indexes_as_the_legacy_closed_bucket():
    from cases.search_index import build_indexed_doc

    case = _case(
        state=CaseState.PUBLISHED,
        dates={
            "stages": [{"stage": "initial", "start": "2024-02-25", "end": "2024-05-22"}]
        },
    )
    _accused(case, RelationshipOutcome.CONVICTED)
    doc = build_indexed_doc(case)

    assert doc["status"] == CaseStatus.CONCLUDED
    assert doc["case_status"] == "closed"
    assert doc["proceedings_decided_on"] == "2024-05-22"


@pytest.mark.django_db
def test_a_dormant_case_indexes_as_others_not_ongoing():
    """The override exists precisely to stop an abandoned case reading as live;
    mapping it to the legacy ``ongoing`` would put the lie straight back."""
    from cases.search_index import build_indexed_doc

    case = _case(
        state=CaseState.PUBLISHED,
        status_override=StatusOverride.DORMANT,
        dates={"stages": [{"stage": "initial", "start": "2019-02-25"}]},
    )
    doc = build_indexed_doc(case)

    assert doc["status"] == StatusOverride.DORMANT
    assert doc["case_status"] == "others"
