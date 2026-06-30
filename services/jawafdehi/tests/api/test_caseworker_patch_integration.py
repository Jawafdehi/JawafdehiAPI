"""
Integration tests for the caseworker PATCH API.

These tests validate end-to-end behavior with realistic case state,
multiple patch operations, permissions, and persistence guarantees.
"""

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

URL = "/api/cases/{}/"


def _make_case(**kwargs):
    defaults = {
        "title": "Nagarik Land Irregularity",
        "case_type": CaseType.CORRUPTION,
        "state": CaseState.DRAFT,
        "short_description": "Initial short description",
        "description": "Initial long description",
        "tags": ["land", "procurement"],
        "key_allegations": ["Initial allegation"],
        "timeline": [
            {"date": "2024-01-01", "title": "Complaint lodged"},
            {"date": "2024-02-10", "title": "Inquiry opened"},
        ],
        "evidence": [{"source_id": "src-old", "description": "Old file"}],
    }
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _authed_client(user):
    # OIDC-only migration: DRF token auth was removed. force_authenticate sets
    # request.user directly (auth-scheme-agnostic) so the authorization logic
    # under test is still exercised.
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_patch_multi_operation_end_to_end_persists_all_changes():
    user = create_user_with_role("sarita", "sarita@example.com", "Caseworker")
    case = _make_case()
    case.contributors.add(user)

    # Entities are owned by NES; binds hold the canonical NES id directly.
    alleged_1 = "https://jawafdehi.org/entity/person/sushil-adhikari"
    alleged_2 = "https://jawafdehi.org/entity/person/maya-gurung"
    related = "https://jawafdehi.org/entity/org/kathmandu-metropolitan-city"
    location = "https://jawafdehi.org/entity/location/district/kathmandu"

    patch_ops = [
        {"op": "replace", "path": "/title", "value": "Updated accountability case"},
        {"op": "replace", "path": "/tags", "value": ["public-fund", "audit"]},
        {
            "op": "replace",
            "path": "/evidence",
            "value": [{"source_id": "src-2026", "description": "Audit report"}],
        },
        {
            "op": "add",
            "path": "/timeline/-",
            "value": {"date": "2024-03-15", "title": "Hearing scheduled"},
        },
        {
            "op": "add",
            "path": "/entities/-",
            "value": {
                "nes_id": alleged_1,
                "relationship_type": RelationshipType.ACCUSED,
            },
        },
        {
            "op": "add",
            "path": "/entities/-",
            "value": {
                "nes_id": alleged_2,
                "relationship_type": RelationshipType.ACCUSED,
            },
        },
        {
            "op": "add",
            "path": "/entities/-",
            "value": {
                "nes_id": related,
                "relationship_type": RelationshipType.RELATED,
            },
        },
        {
            "op": "add",
            "path": "/entities/-",
            "value": {
                "nes_id": location,
                "relationship_type": RelationshipType.LOCATION,
            },
        },
    ]

    response = _authed_client(user).patch(
        URL.format(case.slug), data=patch_ops, format="json"
    )

    assert response.status_code == 200
    assert response.data["title"] == "Updated accountability case"
    assert response.data["tags"] == ["public-fund", "audit"]
    assert response.data["timeline"][-1]["title"] == "Hearing scheduled"
    assert response.data["evidence"] == [
        {"source_id": "src-2026", "description": "Audit report"}
    ]

    case.refresh_from_db()
    assert case.title == "Updated accountability case"
    assert case.tags == ["public-fund", "audit"]
    assert case.timeline[-1]["title"] == "Hearing scheduled"
    assert set(
        CaseEntityRelationship.objects.filter(
            case=case,
            relationship_type=RelationshipType.ACCUSED,
        ).values_list("nes_id", flat=True)
    ) == {alleged_1, alleged_2}
    assert set(
        CaseEntityRelationship.objects.filter(
            case=case,
            relationship_type=RelationshipType.RELATED,
        ).values_list("nes_id", flat=True)
    ) == {related}
    assert set(
        CaseEntityRelationship.objects.filter(
            case=case,
            relationship_type=RelationshipType.LOCATION,
        ).values_list("nes_id", flat=True)
    ) == {location}


@pytest.mark.django_db
def test_patch_rejects_unauthorized_state_transition_in_multi_op_without_partial_write():
    user = create_user_with_role("dipesh", "dipesh@example.com", "Caseworker")
    case = _make_case(title="Original title")
    case.contributors.add(user)

    patch_ops = [
        {"op": "replace", "path": "/title", "value": "Should not persist"},
        {"op": "replace", "path": "/state", "value": "PUBLISHED"},
    ]

    response = _authed_client(user).patch(
        URL.format(case.slug), data=patch_ops, format="json"
    )

    assert response.status_code == 403
    case.refresh_from_db()
    assert case.title == "Original title"
    assert case.state == CaseState.DRAFT


@pytest.mark.django_db
def test_admin_can_patch_without_assignment():
    admin = create_user_with_role("rekha", "rekha@example.com", "Admin")
    case = _make_case(title="Case before admin edit")

    patch_ops = [{"op": "replace", "path": "/title", "value": "Edited by admin"}]
    response = _authed_client(admin).patch(
        URL.format(case.slug), data=patch_ops, format="json"
    )

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.title == "Edited by admin"


@pytest.mark.django_db
def test_invalid_post_patch_payload_produces_422_and_no_persistence():
    user = create_user_with_role("anup", "anup@example.com", "Caseworker")
    case = _make_case(title="Stable title")
    case.contributors.add(user)

    patch_ops = [
        {"op": "replace", "path": "/title", "value": "Transient title"},
        {"op": "replace", "path": "/timeline/0/date", "value": "not-a-date"},
    ]

    response = _authed_client(user).patch(
        URL.format(case.slug), data=patch_ops, format="json"
    )

    assert response.status_code == 422
    case.refresh_from_db()
    assert case.title == "Stable title"
    assert case.timeline[0]["date"] == "2024-01-01"
