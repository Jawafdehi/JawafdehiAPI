"""A verdict ``outcome`` is allowed only on an ACCUSED bind; every other role
must leave it NULL.

Enforced at three layers, each covered below:
  1. model ``save()`` normalization (non-accused -> NULL; accused default charged),
  2. the ``outcome_only_on_accused`` DB CHECK constraint (backstops bulk/raw writes),
  3. the caseworker PATCH ``EntityPatchItemSerializer`` (a 400 beats a 500).
"""

import pytest
from django.db import IntegrityError, transaction

from cases.caseworker_serializers import EntityPatchItemSerializer
from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipOutcome,
    RelationshipType,
)

ACCUSED_IRI = "https://jawafdehi.org/entity/person/test-defendant-abc123"
ORG_IRI = "https://jawafdehi.org/entity/organization/napi-office-def456"


def _case() -> Case:
    return Case.objects.create(
        title="Outcome guard test",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
    )


# --- layer 1: model save() normalization -----------------------------------


@pytest.mark.django_db
def test_save_nulls_outcome_for_non_accused():
    rel = CaseEntityRelationship.objects.create(
        case=_case(),
        nes_id=ORG_IRI,
        relationship_type=RelationshipType.RELATED,
        outcome=RelationshipOutcome.CONVICTED,  # must be discarded
    )
    rel.refresh_from_db()
    assert rel.outcome is None


@pytest.mark.django_db
def test_save_defaults_accused_to_charged():
    rel = CaseEntityRelationship.objects.create(
        case=_case(),
        nes_id=ACCUSED_IRI,
        relationship_type=RelationshipType.ACCUSED,
    )
    rel.refresh_from_db()
    assert rel.outcome == RelationshipOutcome.CHARGED


@pytest.mark.django_db
def test_save_keeps_accused_verdict():
    rel = CaseEntityRelationship.objects.create(
        case=_case(),
        nes_id=ACCUSED_IRI,
        relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.ACQUITTED,
    )
    rel.refresh_from_db()
    assert rel.outcome == RelationshipOutcome.ACQUITTED


# --- layer 2: DB CHECK constraint (bypasses save() via bulk_create) ---------


@pytest.mark.django_db
def test_check_constraint_blocks_verdict_on_non_accused():
    case = _case()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CaseEntityRelationship.objects.bulk_create(
                [
                    CaseEntityRelationship(
                        case=case,
                        nes_id=ORG_IRI,
                        relationship_type=RelationshipType.RELATED,
                        outcome=RelationshipOutcome.CONVICTED,
                    )
                ]
            )


@pytest.mark.django_db
def test_check_constraint_allows_null_non_accused_and_accused_verdict():
    case = _case()
    CaseEntityRelationship.objects.bulk_create(
        [
            CaseEntityRelationship(
                case=case,
                nes_id=ORG_IRI,
                relationship_type=RelationshipType.RELATED,
                outcome=None,
            ),
            CaseEntityRelationship(
                case=case,
                nes_id=ACCUSED_IRI,
                relationship_type=RelationshipType.ACCUSED,
                outcome=RelationshipOutcome.CONVICTED,
            ),
        ]
    )
    assert CaseEntityRelationship.objects.filter(case=case).count() == 2


# --- layer 3: caseworker PATCH serializer -----------------------------------


def test_serializer_rejects_verdict_on_non_accused():
    ser = EntityPatchItemSerializer(
        data={
            "nes_id": ORG_IRI,
            "relationship_type": "related",
            "outcome": "convicted",
        }
    )
    assert not ser.is_valid()
    assert "outcome" in ser.errors


def test_serializer_rejects_even_charged_on_non_accused():
    ser = EntityPatchItemSerializer(
        data={
            "nes_id": ORG_IRI,
            "relationship_type": "location",
            "outcome": "charged",
        }
    )
    assert not ser.is_valid()
    assert "outcome" in ser.errors


def test_serializer_allows_verdict_on_accused():
    ser = EntityPatchItemSerializer(
        data={
            "nes_id": ACCUSED_IRI,
            "relationship_type": "accused",
            "outcome": "convicted",
        }
    )
    assert ser.is_valid(), ser.errors


def test_serializer_allows_null_outcome_on_non_accused():
    ser = EntityPatchItemSerializer(
        data={
            "nes_id": ORG_IRI,
            "relationship_type": "related",
            "outcome": None,
        }
    )
    assert ser.is_valid(), ser.errors


def test_serializer_allows_omitted_outcome_on_non_accused():
    ser = EntityPatchItemSerializer(
        data={"nes_id": ORG_IRI, "relationship_type": "related"}
    )
    assert ser.is_valid(), ser.errors
