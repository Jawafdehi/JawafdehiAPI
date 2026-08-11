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
    RelationshipOutcome,
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

    # Entities are owned by NES; binds hold the canonical NES id directly.
    alleged_1 = "https://jawafdehi.org/entity/person/sushil-adhikari"
    alleged_2 = "https://jawafdehi.org/entity/person/maya-gurung"
    related = "https://jawafdehi.org/entity/org/kathmandu-metropolitan-city"
    location = "https://jawafdehi.org/entity/location/district/kathmandu"

    patch_ops = [
        {"op": "replace", "path": "/title", "value": "Updated accountability case"},
        {"op": "replace", "path": "/tags", "value": ["public-fund", "audit"]},
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
def test_patch_notes_persists_and_survives_read_back_for_caseworker():
    # BB-28 end-to-end: replace /notes on a case with no notes -> 200, the note
    # persists, and reloading the case through the read endpoint (the way the
    # editor reloads before the next PATCH) returns it to the caseworker.
    user = create_user_with_role("bimala", "bimala@example.com", "Caseworker")
    case = _make_case()
    assert case.notes == ""

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/notes", "value": "internal casework note"}],
        format="json",
    )
    assert response.status_code == 200, response.data

    case.refresh_from_db()
    assert case.notes == "internal casework note"

    # Reload via GET — the casework read serializer exposes notes to casework
    # roles (BB-04), so the editor's round-trip shows the saved note.
    reload = client.get(URL.format(case.slug))
    assert reload.status_code == 200
    assert reload.data["notes"] == "internal casework note"


@pytest.mark.django_db
def test_patch_weight_persists_and_survives_read_back():
    # PATCH is the ONLY way to set weight — Django admin is read-only for cases,
    # so the SPA `/admin` panel is the sole write surface. Guards the BB-28 shape:
    # a field the serializer accepts but no allowlist persists is silently dropped.
    user = create_user_with_role("bimala", "bimala@example.com", "Caseworker")
    case = _make_case()
    assert case.weight == 0

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/weight", "value": 100}],
        format="json",
    )
    assert response.status_code == 200, response.data

    case.refresh_from_db()
    assert case.weight == 100

    # The editor reloads before the next PATCH; it must see the value it just set
    # or it cannot tell what the current top weight is.
    reload = client.get(URL.format(case.slug))
    assert reload.status_code == 200
    assert reload.data["weight"] == 100


@pytest.mark.django_db
def test_patch_weight_accepts_negative_and_zero():
    """0 unranks; negatives demote below untouched cases (which sort as 0)."""
    user = create_user_with_role("bimala", "bimala@example.com", "Caseworker")
    case = _make_case(weight=42)
    client = _authed_client(user)

    for value in (0, -5):
        response = client.patch(
            URL.format(case.slug),
            data=[{"op": "replace", "path": "/weight", "value": value}],
            format="json",
        )
        assert response.status_code == 200, response.data
        case.refresh_from_db()
        assert case.weight == value


@pytest.mark.django_db
def test_patch_weight_rejects_null_and_out_of_range():
    """The column is NOT NULL and 32-bit: both must 422 at the serializer rather
    than reach the DB (a bigo-sized value fits IntegerField's serializer only if
    the bounds are declared)."""
    user = create_user_with_role("bimala", "bimala@example.com", "Caseworker")
    case = _make_case(weight=7)
    client = _authed_client(user)

    for bad in (None, 2147483648):
        response = client.patch(
            URL.format(case.slug),
            data=[{"op": "replace", "path": "/weight", "value": bad}],
            format="json",
        )
        assert response.status_code == 422, response.data
        case.refresh_from_db()
        assert case.weight == 7


@pytest.mark.django_db
def test_patch_rejects_invalid_state_transition_in_multi_op_without_partial_write():
    # v3 authz: a Caseworker is authorized to publish, but publishing an
    # incomplete case (no accused entity / key allegation) fails validation.
    # The multi-op must not persist the title change either — no partial write.
    user = create_user_with_role("dipesh", "dipesh@example.com", "Caseworker")
    case = _make_case(title="Original title", key_allegations=[])

    patch_ops = [
        {"op": "replace", "path": "/title", "value": "Should not persist"},
        {"op": "replace", "path": "/state", "value": "PUBLISHED"},
    ]

    response = _authed_client(user).patch(
        URL.format(case.slug), data=patch_ops, format="json"
    )

    assert response.status_code == 422
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


@pytest.mark.django_db
def test_patch_explicit_null_outcome_resets_accused_verdict_to_charged():
    """An explicit ``outcome: null`` on an accused bind must reset the verdict
    to the default 'charged' — not silently preserve the prior verdict. The old
    ``item.get("outcome") or prior_outcomes.get(key)`` fell through on null."""
    user = create_user_with_role("gita", "gita@example.com", "Caseworker")
    case = _make_case()

    accused = "https://jawafdehi.org/entity/person/sushil-adhikari"
    # Seed a non-default prior verdict for the bind.
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id=accused,
        relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CONVICTED,
    )

    patch_ops = [
        {
            "op": "replace",
            "path": "/entities",
            "value": [
                {
                    "nes_id": accused,
                    "relationship_type": RelationshipType.ACCUSED,
                    # Explicit null: client asks to reset the verdict.
                    "outcome": None,
                }
            ],
        }
    ]

    response = _authed_client(user).patch(
        URL.format(case.slug), data=patch_ops, format="json"
    )

    assert response.status_code == 200, response.data
    rel = CaseEntityRelationship.objects.get(
        case=case, nes_id=accused, relationship_type=RelationshipType.ACCUSED
    )
    # Model save() normalizes a null accused verdict to CHARGED, NOT the prior
    # CONVICTED — the explicit null reset the verdict to the default.
    assert rel.outcome == RelationshipOutcome.CHARGED


@pytest.mark.django_db
def test_patch_omitted_outcome_preserves_accused_prior_verdict():
    """When the client OMITS ``outcome`` entirely, the accused bind's prior
    verdict is preserved across the whole-list delete/recreate (guards against
    an outcome-unaware client silently resetting verdicts)."""
    user = create_user_with_role("hari", "hari@example.com", "Caseworker")
    case = _make_case()

    accused = "https://jawafdehi.org/entity/person/sushil-adhikari"
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id=accused,
        relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CONVICTED,
    )

    patch_ops = [
        {
            "op": "replace",
            "path": "/entities",
            "value": [
                {
                    "nes_id": accused,
                    "relationship_type": RelationshipType.ACCUSED,
                    # No "outcome" key at all.
                }
            ],
        }
    ]

    response = _authed_client(user).patch(
        URL.format(case.slug), data=patch_ops, format="json"
    )

    assert response.status_code == 200, response.data
    rel = CaseEntityRelationship.objects.get(
        case=case, nes_id=accused, relationship_type=RelationshipType.ACCUSED
    )
    assert rel.outcome == RelationshipOutcome.CONVICTED


@pytest.mark.django_db
def test_patch_duplicate_entity_bind_returns_422_not_500():
    """Two payload entries with the same (nes_id, relationship_type) violate the
    unique_case_entity_relationship_type DB constraint at .create(). The loop
    detects the dup and returns a field-keyed 422 rather than letting the
    IntegrityError surface as a 500."""
    user = create_user_with_role("nabin", "nabin@example.com", "Caseworker")
    case = _make_case()

    accused = "https://jawafdehi.org/entity/person/sushil-adhikari"
    patch_ops = [
        {
            "op": "replace",
            "path": "/entities",
            "value": [
                {
                    "nes_id": accused,
                    "relationship_type": RelationshipType.ACCUSED,
                },
                {
                    # Same bind identity -> duplicate.
                    "nes_id": accused,
                    "relationship_type": RelationshipType.ACCUSED,
                },
            ],
        }
    ]

    response = _authed_client(user).patch(
        URL.format(case.slug), data=patch_ops, format="json"
    )

    assert response.status_code == 422, response.data
    assert "entities" in response.data
    # No partial write: the prior (empty) relationship set is intact.
    assert not CaseEntityRelationship.objects.filter(case=case).exists()
