"""
Tests for PATCH /api/cases/{id}/ (RFC 6902 JSON Patch endpoint).
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from tests.conftest import create_user_with_role

User = get_user_model()

URL = "/api/cases/{}/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_case(**kwargs) -> Case:
    defaults = dict(
        title="Test case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="Some description",
        short_description="Short",
        timeline=[{"date": "2024-01-01", "title": "Event one"}],
        evidence=[],
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _authed_client(user) -> APIClient:
    # OIDC-only migration: DRF token auth was removed. force_authenticate sets
    # request.user directly (auth-scheme-agnostic) so the authorization logic
    # under test is still exercised.
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _contributor(name="rishi") -> User:
    return create_user_with_role(name, f"{name}@example.com", "Caseworker")


# ---------------------------------------------------------------------------
# Auth / permission tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_requires_authentication():
    case = _make_case()
    client = APIClient()
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "New"}],
        format="json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_patch_returns_403_for_unassigned_contributor():
    case = _make_case()
    user = _contributor("sunita")
    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Hacked"}],
        format="json",
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Successful patch operations
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_replace_scalar_field():
    user = _contributor("hari")
    case = _make_case()
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Updated title"}],
        format="json",
    )
    assert response.status_code == 200
    assert response.data["title"] == "Updated title"
    case.refresh_from_db()
    assert case.title == "Updated title"


@pytest.mark.django_db
def test_patch_replace_timeline_item_title():
    user = _contributor("sita")
    case = _make_case(
        timeline=[
            {"date": "2024-01-01", "title": "First event"},
            {"date": "2024-02-01", "title": "Second event"},
        ]
    )
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/timeline/0/title", "value": "Renamed"}],
        format="json",
    )
    assert response.status_code == 200
    timeline = response.data["timeline"]
    assert timeline[0]["title"] == "Renamed"
    assert timeline[1]["title"] == "Second event"


@pytest.mark.django_db
def test_patch_timeline_preserves_date_bs_and_span_fields():
    """PATCH must not strip the optional date_bs/end_date/end_date_bs fields."""
    user = _contributor("kamala")
    case = _make_case()
    case.contributors.add(user)

    new_timeline = [
        {
            "date": "1989-07-14",
            "date_bs": "2046-03-30",
            "end_date": "2020-07-15",
            "end_date_bs": "2077-03-31",
            "title": "जाँच अवधि",
            "description": "Investigation period span.",
        },
        {
            "date": "2025-02-09",
            "date_bs": "2081-10-27",
            "title": "मुद्दा दर्ता",
        },
    ]
    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/timeline", "value": new_timeline}],
        format="json",
    )
    assert response.status_code == 200
    timeline = response.data["timeline"]
    assert timeline[0]["date_bs"] == "2046-03-30"
    assert timeline[0]["end_date"] == "2020-07-15"
    assert timeline[0]["end_date_bs"] == "2077-03-31"
    assert timeline[1]["date_bs"] == "2081-10-27"

    case.refresh_from_db()
    assert case.timeline[0]["end_date"] == "2020-07-15"
    assert case.timeline[0]["end_date_bs"] == "2077-03-31"
    assert case.timeline[1]["date_bs"] == "2081-10-27"


@pytest.mark.django_db
def test_patch_timeline_rejects_malformed_date_bs():
    """A malformed date_bs in a PATCHed timeline is rejected (422)."""
    user = _contributor("nabin")
    case = _make_case()
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/timeline",
                "value": [
                    {"date": "2025-02-09", "date_bs": "2081/10/27", "title": "X"}
                ],
            }
        ],
        format="json",
    )
    assert response.status_code == 422


@pytest.mark.django_db
def test_patch_add_appends_timeline_item():
    user = _contributor("ram")
    case = _make_case(timeline=[{"date": "2024-01-01", "title": "First"}])
    case.contributors.add(user)

    new_item = {"date": "2025-03-15", "title": "New event", "description": "Details"}
    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "add", "path": "/timeline/-", "value": new_item}],
        format="json",
    )
    assert response.status_code == 200
    assert len(response.data["timeline"]) == 2
    assert response.data["timeline"][-1]["title"] == "New event"


@pytest.mark.django_db
def test_patch_remove_timeline_item():
    user = _contributor("gita")
    case = _make_case(
        timeline=[
            {"date": "2024-01-01", "title": "Keep"},
            {"date": "2024-02-01", "title": "Remove me"},
        ]
    )
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "remove", "path": "/timeline/1"}],
        format="json",
    )
    assert response.status_code == 200
    timeline = response.data["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["title"] == "Keep"


@pytest.mark.django_db
def test_patch_add_entity_with_relationship_type():
    user = _contributor("kiran")
    case = _make_case()
    case.contributors.add(user)
    entity = "https://jawafdehi.org/entity/person/prachanda"

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {
                    "nes_id": entity,
                    "relationship_type": RelationshipType.ACCUSED,
                },
            }
        ],
        format="json",
    )
    assert response.status_code == 200
    entity_types = [e["type"] for e in response.data["entities"]]
    assert RelationshipType.ACCUSED in entity_types
    assert CaseEntityRelationship.objects.filter(
        case=case, nes_id=entity, relationship_type=RelationshipType.ACCUSED
    ).exists()


@pytest.mark.django_db
def test_patch_add_location_entity():
    user = _contributor("kiran-loc")
    case = _make_case()
    case.contributors.add(user)
    entity = "https://jawafdehi.org/entity/location/district/kathmandu"

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {
                    "nes_id": entity,
                    "relationship_type": RelationshipType.LOCATION,
                    "notes": "Primary location",
                },
            }
        ],
        format="json",
    )
    assert response.status_code == 200
    entity_types = [e["type"] for e in response.data["entities"]]
    assert RelationshipType.LOCATION in entity_types
    rel = CaseEntityRelationship.objects.get(case=case, nes_id=entity)
    assert rel.relationship_type == RelationshipType.LOCATION
    assert rel.notes == "Primary location"


@pytest.mark.django_db
def test_patch_replace_evidence_list():
    user = _contributor("bikash")
    case = _make_case()
    case.contributors.add(user)
    new_evidence = [{"source_id": "src-001", "description": "Key document"}]

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/evidence", "value": new_evidence}],
        format="json",
    )
    assert response.status_code == 200
    assert response.data["evidence"] == new_evidence
    case.refresh_from_db()
    assert case.evidence == new_evidence


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_400_for_malformed_patch_body():
    user = _contributor("sabita")
    case = _make_case()
    case.contributors.add(user)

    client = _authed_client(user)
    # Send a dict instead of a list — invalid RFC 6902
    response = client.patch(
        URL.format(case.slug),
        data={"op": "replace", "path": "/title", "value": "x"},
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_patch_400_for_invalid_json_patch_operation():
    user = _contributor("manish")
    case = _make_case()
    case.contributors.add(user)

    client = _authed_client(user)
    # Reference a path index that doesn't exist
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "remove", "path": "/timeline/99"}],
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_patch_403_for_unauthorized_state_transition_to_published():
    user = _contributor("deepak")
    case = _make_case()
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": "PUBLISHED"}],
        format="json",
    )
    assert response.status_code == 403
    case.refresh_from_db()
    assert case.state == CaseState.DRAFT


@pytest.mark.django_db
def test_patch_200_for_draft_to_in_review_transition():
    user = _contributor("deepak-2")
    case = _make_case(
        description="Detailed allegation description",
        key_allegations=["Primary allegation"],
    )
    case.contributors.add(user)
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id="https://jawafdehi.org/entity/person/ram-prasad-gautam",
        relationship_type=RelationshipType.ACCUSED,
    )

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": "IN_REVIEW"}],
        format="json",
    )

    assert response.status_code == 200
    assert response.data["state"] == CaseState.IN_REVIEW
    case.refresh_from_db()
    assert case.state == CaseState.IN_REVIEW


@pytest.mark.django_db
def test_patch_400_for_draft_to_in_review_missing_required_fields():
    user = _contributor("deepak-3")
    case = _make_case(key_allegations=[])
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": "IN_REVIEW"}],
        format="json",
    )

    assert response.status_code == 400
    assert "entities" in response.data
    assert "key_allegations" in response.data
    case.refresh_from_db()
    assert case.state == CaseState.DRAFT


@pytest.mark.django_db
def test_patch_rejects_removed_case_id_path():
    # ``case_id`` has been dropped from the Case model (the slug is now the
    # case identifier). A patch targeting the removed field is rejected: the
    # snapshot has no ``/case_id`` member, so the JSON Patch fails to apply.
    user = _contributor("priya")
    case = _make_case()
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/case_id", "value": "case-tampered"}],
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_patch_422_for_blocked_path_case_type():
    user = _contributor("nisha")
    case = _make_case(case_type=CaseType.CORRUPTION)
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/case_type", "value": "CORRUPTION"}],
        format="json",
    )
    assert response.status_code == 422
    case.refresh_from_db()
    assert case.case_type == CaseType.CORRUPTION


@pytest.mark.django_db
def test_patch_422_for_invalid_nes_id():
    user = _contributor("anjali")
    case = _make_case()
    case.contributors.add(user)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {"nes_id": "not-a-valid-id", "relationship_type": "accused"},
            }
        ],
        format="json",
    )
    assert response.status_code == 422


@pytest.mark.django_db
def test_patch_scalar_only_does_not_touch_entity_relationships():
    """Scalar-only PATCH must not delete/recreate entity relationships."""
    user = _contributor("binod")
    case = _make_case()
    case.contributors.add(user)

    entity = "https://jawafdehi.org/entity/person/bijaya-shumsher"
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id=entity,
        relationship_type=RelationshipType.ACCUSED,
    )
    rel_pk_before = CaseEntityRelationship.objects.get(case=case, nes_id=entity).pk

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Updated Title"}],
        format="json",
    )
    assert response.status_code == 200
    # The relationship row must be the exact same DB row (same pk).
    rel_pk_after = CaseEntityRelationship.objects.get(case=case, nes_id=entity).pk
    assert (
        rel_pk_before == rel_pk_after
    ), "Scalar-only PATCH must not delete and recreate entity relationships"
