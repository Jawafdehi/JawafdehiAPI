"""
Tests for the ``?entity=<iri>`` reverse-lookup filter on GET /api/cases/.

Powers the "Related cases" section on an entity's record page: the published
cases that cite a given NES entity, with accused/alleged citations floated to
the top and reverse-chronological within each tier.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from tests.conftest import create_case_with_entities

X = "https://jawafdehi.org/entity/person/test-x"
Y = "https://jawafdehi.org/entity/person/test-y"


def _get(entity):
    resp = APIClient().get("/api/cases/", {"entity": entity})
    assert resp.status_code == 200
    return resp.data


@pytest.mark.django_db
class TestCaseEntityFilter:
    def test_returns_only_citing_published_cases(self):
        create_case_with_entities(
            slug="ef-accused",
            title="Accused case",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[X],
        )
        create_case_with_entities(
            slug="ef-related",
            title="Related case",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            related_entities=[X],
        )
        # Published but does NOT cite X -> excluded.
        create_case_with_entities(
            slug="ef-other",
            title="Other entity",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[Y],
        )
        # Cites X but IN_REVIEW -> hidden from the anonymous reverse lookup.
        create_case_with_entities(
            slug="ef-inreview",
            title="In review",
            state=CaseState.IN_REVIEW,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[X],
        )

        data = _get(X)

        assert data["count"] == 2
        assert {c["slug"] for c in data["results"]} == {"ef-accused", "ef-related"}

    def test_unknown_entity_returns_empty(self):
        create_case_with_entities(
            slug="ef-lonely",
            title="Lonely",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[X],
        )

        data = _get("https://jawafdehi.org/entity/person/nobody")

        assert data["count"] == 0
        assert data["results"] == []

    def test_same_entity_two_roles_on_one_case_deduped(self):
        # A case may cite the same entity in more than one role (the unique key
        # is (case, nes_id, relationship_type)). The filter must return the case
        # ONCE (this is why .distinct() exists) and tier it as accused.
        case = create_case_with_entities(
            slug="ef-multirole",
            title="Multi-role",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            related_entities=[X],
        )
        CaseEntityRelationship.objects.create(
            case=case, nes_id=X, relationship_type=RelationshipType.ACCUSED
        )

        data = _get(X)

        assert data["count"] == 1
        assert [c["slug"] for c in data["results"]] == ["ef-multirole"]

    def test_anon_state_param_cannot_widen_past_published(self):
        create_case_with_entities(
            slug="ef-scoped-inreview",
            title="In review",
            state=CaseState.IN_REVIEW,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[X],
        )

        # Even explicitly asking for IN_REVIEW, an anonymous caller stays scoped
        # to PUBLISHED (scoping runs before the state filter).
        resp = APIClient().get("/api/cases/", {"entity": X, "state": "IN_REVIEW"})

        assert resp.status_code == 200
        assert resp.data["count"] == 0

    def test_accused_alleged_float_to_top(self):
        now = timezone.now()

        # Related case is the MOST recent; it must still sort below every
        # accused/alleged citation.
        rel = create_case_with_entities(
            slug="ord-related",
            title="Related (newest)",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            related_entities=[X],
        )
        acc = create_case_with_entities(
            slug="ord-accused",
            title="Accused (oldest)",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[X],
        )
        alleged = Case.objects.create(
            slug="ord-alleged",
            title="Alleged (middle)",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
        )
        CaseEntityRelationship.objects.create(
            case=alleged, nes_id=X, relationship_type=RelationshipType.ALLEGED
        )

        # auto_now_add ignores create-time created_at; set it explicitly.
        Case.objects.filter(pk=rel.pk).update(created_at=now)
        Case.objects.filter(pk=alleged.pk).update(created_at=now - timedelta(days=1))
        Case.objects.filter(pk=acc.pk).update(created_at=now - timedelta(days=2))

        order = [c["slug"] for c in _get(X)["results"]]

        # accused/alleged tier first (reverse-chron within: alleged newer than
        # accused), then the related case last despite being the newest overall.
        assert order == ["ord-alleged", "ord-accused", "ord-related"]
