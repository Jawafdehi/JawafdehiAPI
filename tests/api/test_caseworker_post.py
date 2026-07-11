"""Tests for POST /api/cases/ draft creation endpoint."""

import pytest
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from tests.conftest import create_user_with_role

URL = "/api/cases/"


def _authed_client(user):
    # OIDC-only migration: DRF token auth was removed. force_authenticate sets
    # request.user directly (auth-scheme-agnostic) so the authorization logic
    # under test is still exercised.
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_post_requires_authentication():
    response = APIClient().post(
        URL,
        data={"title": "Unauthorized case", "case_type": CaseType.CORRUPTION},
        format="json",
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_post_creates_draft():
    # v3 authz: per-case contributor assignment is retired, so POST no longer
    # auto-adds the creator to a contributors set — it just creates the draft.
    user = create_user_with_role("ashok", "ashok@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data={
            "title": "Procurement irregularity",
            "case_type": CaseType.CORRUPTION,
            "short_description": "Initial draft",
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["title"] == "Procurement irregularity"
    assert response.data["state"] == CaseState.DRAFT
    assert response.data["case_type"] == CaseType.CORRUPTION
    assert response.data["slug"]

    case = Case.objects.get(pk=response.data["id"])
    assert case.state == CaseState.DRAFT


@pytest.mark.django_db
def test_post_court_cases_stores_iris():
    """court_cases takes canonical @id IRIs; stored on the reference join."""
    user = create_user_with_role("ashok-court", "ashok-court@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data={
            "title": "Court ref creation",
            "case_type": CaseType.CORRUPTION,
            "court_cases": ["https://jawafdehi.org/courtcase/special/080-cr-0111"],
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["court_cases"] == [
        "https://jawafdehi.org/courtcase/special/080-cr-0111"
    ]
    case = Case.objects.get(pk=response.data["id"])
    assert list(case.courtcase_references.values_list("courtcase_iri", flat=True)) == [
        "https://jawafdehi.org/courtcase/special/080-cr-0111"
    ]
    # The slug derives from the court case number ("case-" prefix: slugs must
    # start with a letter).
    assert response.data["slug"].startswith("case-080-cr-0111-")


@pytest.mark.django_db
def test_post_rejects_non_iri_court_refs():
    """Short-form refs and unknown courts are rejected — IRIs only."""
    user = create_user_with_role(
        "ashok-court2", "ashok-court2@example.com", "Caseworker"
    )
    client = _authed_client(user)

    for bad_refs in (
        ["special:080-CR-0111"],  # legacy short form
        ["not-a-real-court:123"],
        ["https://jawafdehi.org/courtcase/not-a-real-court/123"],
    ):
        response = client.post(
            URL,
            data={
                "title": "Bad court ref",
                "case_type": CaseType.CORRUPTION,
                "court_cases": bad_refs,
            },
            format="json",
        )
        assert response.status_code == 422, bad_refs
    assert Case.objects.filter(title="Bad court ref").count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case_type",
    [
        "CORRUPTION",
        "BRIBERY",
        "FORGERY",
        "EMBEZZLEMENT",
        "ABUSE_OF_OFFICE",
        "MONEY_LAUNDERING",
        "ILLEGAL_PROPERTY",
        "EXAM_RIGGING",
        "TAX_EVASION",
    ],
)
def test_post_creates_case_for_every_frontend_case_type(case_type):
    # The frontend's 9-member CaseType set (src/types/jds.ts) is authoritative;
    # each of its wire values must be accepted by POST /api/cases/.
    user = create_user_with_role("bipin", "bipin@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data={
            "title": f"Case of type {case_type}",
            "case_type": case_type,
            "short_description": "draft",
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["case_type"] == case_type
    assert Case.objects.get(pk=response.data["id"]).case_type == case_type


@pytest.mark.django_db
def test_post_creates_case_with_entity_relationships():
    user = create_user_with_role("bina", "bina@example.com", "Caseworker")
    # Entities are owned by NES; binds hold the canonical NES id directly.
    alleged = "https://jawafdehi.org/entity/person/prachanda"
    related = "https://jawafdehi.org/entity/org/kathmandu-metropolitan-city"
    location = "https://jawafdehi.org/entity/location/district/kathmandu"

    response = _authed_client(user).post(
        URL,
        data={
            "title": "Land use concern",
            "case_type": CaseType.CORRUPTION,
            "alleged_entities": [alleged],
            "related_entities": [related, location],
        },
        format="json",
    )

    assert response.status_code == 201
    alleged_ids = [
        e["nes_id"] for e in response.data["entities"] if e["type"] == "accused"
    ]
    related_ids = [
        e["nes_id"] for e in response.data["entities"] if e["type"] == "related"
    ]
    assert alleged_ids == [alleged]
    assert set(related_ids) == {related, location}
    assert CaseEntityRelationship.objects.filter(
        case_id=response.data["id"],
        nes_id=alleged,
        relationship_type=RelationshipType.ACCUSED,
    ).exists()


@pytest.mark.django_db
def test_post_rejects_non_draft_state():
    user = create_user_with_role("chandra", "chandra@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data={
            "title": "Should fail",
            "case_type": CaseType.CORRUPTION,
            "state": CaseState.PUBLISHED,
            "description": "Complete description",
            "key_allegations": ["An allegation"],
        },
        format="json",
    )

    assert response.status_code == 422
    assert "state" in response.data
    assert Case.objects.count() == 0


@pytest.mark.django_db
def test_post_rejects_missing_title():
    """Title-required rule is enforced on the API create path (model-layer rule,
    formerly re-invoked by CaseAdminForm.clean())."""
    user = create_user_with_role("farid", "farid@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data={"case_type": CaseType.CORRUPTION},
        format="json",
    )

    assert response.status_code == 422
    assert "title" in response.data
    assert Case.objects.count() == 0


@pytest.mark.django_db
def test_post_rejects_blank_title():
    user = create_user_with_role("gita", "gita@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data={"title": "   ", "case_type": CaseType.CORRUPTION},
        format="json",
    )

    assert response.status_code == 422
    assert Case.objects.count() == 0


@pytest.mark.django_db
def test_post_rejects_invalid_slug_format():
    """Slug FORMAT is enforced via the serializer's validate_slug validator (no
    admin form needed)."""
    user = create_user_with_role("hari", "hari@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data={
            "title": "Bad slug case",
            "case_type": CaseType.CORRUPTION,
            "slug": "1-cannot-start-with-digit",
        },
        format="json",
    )

    assert response.status_code == 422
    assert "slug" in response.data
    assert Case.objects.count() == 0


@pytest.mark.django_db
def test_post_draft_stays_lenient_without_allegations_or_description():
    """DRAFT create does NOT trigger the IN_REVIEW/PUBLISHED allegation and
    description gates — parity with the old admin-form create semantics."""
    user = create_user_with_role("indira", "indira@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data={"title": "Bare draft", "case_type": CaseType.CORRUPTION},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["state"] == CaseState.DRAFT


@pytest.mark.django_db
def test_post_rejects_array_payload():
    """Test that POST with array payload returns 422 with clear error message."""
    user = create_user_with_role("eshwar", "eshwar@example.com", "Caseworker")

    response = _authed_client(user).post(
        URL,
        data=[
            {"title": "First case", "case_type": CaseType.CORRUPTION},
            {"title": "Second case", "case_type": CaseType.CORRUPTION},
        ],
        format="json",
    )

    assert response.status_code == 422
    assert "detail" in response.data
    assert response.data["detail"] == "Request body must be a JSON object."
    assert Case.objects.count() == 0
