"""
Tests for retrieving a case by a court case reference.

GET /api/cases/{lookup}/ accepts a slug or a court case reference of the form
{court_identifier}:{case_number}. The case number is normalized like NGM
(casing, zero-padding, Devanagari digits) before being matched against the
case's court_cases list.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities


@pytest.mark.django_db
def test_retrieve_published_case_by_exact_court_case_reference():
    case = create_case_with_entities(
        title="Court Case Lookup",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        court_cases=["supreme:081-CR-0081"],
    )

    response = APIClient().get("/api/cases/supreme:081-CR-0081/")

    assert response.status_code == 200
    assert response.data["case_id"] == case.case_id


@pytest.mark.django_db
@pytest.mark.parametrize(
    "lookup",
    [
        "supreme:81-cr-81",  # missing zero-padding
        "supreme:081-cr-0081",  # lowercase middle code
        "supreme:०८१-CR-००८१",  # Devanagari digits
    ],
)
def test_retrieve_normalizes_court_case_number(lookup):
    case = create_case_with_entities(
        title="Normalize Lookup",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        court_cases=["supreme:081-CR-0081"],
    )

    response = APIClient().get(f"/api/cases/{lookup}/")

    assert response.status_code == 200
    assert response.data["case_id"] == case.case_id


@pytest.mark.django_db
def test_retrieve_unknown_court_case_returns_404():
    create_case_with_entities(
        title="Other Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        court_cases=["supreme:081-CR-0081"],
    )

    response = APIClient().get("/api/cases/supreme:099-CR-9999/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_retrieve_invalid_court_case_number_returns_404():
    response = APIClient().get("/api/cases/supreme:not-a-number/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_court_case_lookup_does_not_expose_draft_to_anonymous():
    create_case_with_entities(
        title="Secret Draft",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        court_cases=["supreme:081-CR-0081"],
    )

    response = APIClient().get("/api/cases/supreme:081-CR-0081/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_court_case_lookup_returns_most_recent_when_multiple_cases_match():
    create_case_with_entities(
        title="Older Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        court_cases=["supreme:081-CR-0081"],
    )
    newer = create_case_with_entities(
        title="Newer Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        court_cases=["supreme:081-CR-0081"],
    )

    response = APIClient().get("/api/cases/supreme:081-CR-0081/")

    assert response.status_code == 200
    assert response.data["case_id"] == newer.case_id


@pytest.mark.django_db
def test_court_case_lookup_is_case_insensitive_on_court_identifier():
    case = create_case_with_entities(
        title="Mixed Case Identifier",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        court_cases=["supreme:081-CR-0081"],
    )

    response = APIClient().get("/api/cases/Supreme:081-CR-0081/")

    assert response.status_code == 200
    assert response.data["case_id"] == case.case_id


@pytest.mark.django_db
def test_court_case_lookup_does_not_shadow_published_with_unauthorized_draft():
    older_published = create_case_with_entities(
        title="Older Published Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        court_cases=["supreme:081-CR-0081"],
    )
    create_case_with_entities(
        title="Newer Draft Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        court_cases=["supreme:081-CR-0081"],
    )

    user = get_user_model().objects.create_user(
        username="regular_user", password="password"
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get("/api/cases/supreme:081-CR-0081/")

    assert response.status_code == 200
    assert response.data["case_id"] == older_published.case_id
