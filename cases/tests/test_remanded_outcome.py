"""``remanded`` on RelationshipOutcome.

Without it a Supreme Court remand (बदर गरी पुनः इन्साफ) reads as concluded --
every recorded outcome is terminal -- until someone registers the new शुरु
stage, which can be months. It is non-terminal, like ``charged``.
"""

import pytest

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipOutcome,
    RelationshipType,
    TERMINAL_OUTCOMES,
)

ACCUSED_IRI = "https://jawafdehi.org/entity/person/remand-subject"


def test_remanded_is_a_declared_outcome():
    assert RelationshipOutcome.REMANDED == "remanded"
    assert "remanded" in {value for value, _label in RelationshipOutcome.choices}


def test_remanded_is_not_terminal():
    """The whole point: a remanded defendant's case is not over."""
    assert RelationshipOutcome.REMANDED not in TERMINAL_OUTCOMES
    assert RelationshipOutcome.CHARGED not in TERMINAL_OUTCOMES
    assert TERMINAL_OUTCOMES == frozenset(
        {
            RelationshipOutcome.CONVICTED,
            RelationshipOutcome.ACQUITTED,
            RelationshipOutcome.ABATED,
        }
    )


@pytest.mark.django_db
def test_a_remanded_bind_persists_on_an_accused():
    case = Case.objects.create(
        title="Remand case", offence_type=CaseType.CORRUPTION, state=CaseState.DRAFT
    )
    bind = CaseEntityRelationship.objects.create(
        case=case,
        nes_id=ACCUSED_IRI,
        relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.REMANDED,
    )

    bind.refresh_from_db()
    assert bind.outcome == RelationshipOutcome.REMANDED


@pytest.mark.django_db
def test_remanded_is_nulled_on_a_non_accused_role():
    """``save()`` normalises rather than raising: an outcome is meaningful only
    for ACCUSED, and the new value must be normalised like the existing ones
    (the ``outcome_only_on_accused`` CHECK constraint is the backstop)."""
    case = Case.objects.create(
        title="Remand case 2", offence_type=CaseType.CORRUPTION, state=CaseState.DRAFT
    )

    bind = CaseEntityRelationship.objects.create(
        case=case,
        nes_id="https://jawafdehi.org/entity/location/kathmandu",
        relationship_type=RelationshipType.LOCATION,
        outcome=RelationshipOutcome.REMANDED,
    )

    bind.refresh_from_db()
    assert bind.outcome is None
