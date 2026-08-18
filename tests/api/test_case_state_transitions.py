"""Tests for A2 — full case state transitions via PATCH /api/cases/{slug}/.

The PATCH endpoint dispatches every state target to the model method that
already implements + validates the transition:

    ->IN_REVIEW  = Case.submit()
    ->PUBLISHED  = Case.publish()
    ->CLOSED     = Case.delete()   (soft-delete)
    ->DRAFT      = revert (un-submit / un-publish)

Each is gated by can_transition_case_state (Admin/Moderator may go anywhere;
Caseworkers are confined to DRAFT<->IN_REVIEW by the predicate). Model
ValidationError surfaces as 422 with field-keyed messages.
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
from tests.byline import credit_author
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _publishable_case(state=CaseState.DRAFT, **kwargs) -> Case:
    """A case that satisfies the IN_REVIEW/PUBLISHED gates (accused entity +
    allegations + description)."""
    defaults = dict(
        title="Publishable case",
        case_type=CaseType.CORRUPTION,
        state=state,
        description="Detailed allegation description",
        short_description="Short",
        key_allegations=["Primary allegation"],
    )
    defaults.update(kwargs)
    case = Case.objects.create(**defaults)
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id="https://jawafdehi.org/entity/person/ram-prasad-gautam",
        relationship_type=RelationshipType.ACCUSED,
    )
    # The byline half of the same gate: >=1 credited author + a publish date.
    credit_author(case)
    return case


def _patch_state(client, case, target):
    return client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": target}],
        format="json",
    )


# ---------------------------------------------------------------------------
# Allowed transitions per role (success)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Admin", "Moderator"])
def test_in_review_to_published_allowed_for_admin_moderator(role):
    user = create_user_with_role(f"pub-{role}", f"pub-{role}@example.com", role)
    case = _publishable_case(state=CaseState.IN_REVIEW)

    response = _patch_state(_authed_client(user), case, CaseState.PUBLISHED)

    assert response.status_code == 200
    assert response.data["state"] == CaseState.PUBLISHED
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Admin", "Moderator"])
def test_draft_to_published_allowed_for_admin_moderator(role):
    user = create_user_with_role(f"dpub-{role}", f"dpub-{role}@example.com", role)
    case = _publishable_case(state=CaseState.DRAFT)

    response = _patch_state(_authed_client(user), case, CaseState.PUBLISHED)

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Admin", "Moderator"])
def test_to_closed_soft_delete_allowed_for_admin_moderator(role):
    user = create_user_with_role(f"cls-{role}", f"cls-{role}@example.com", role)
    case = _publishable_case(state=CaseState.PUBLISHED)

    response = _patch_state(_authed_client(user), case, CaseState.CLOSED)

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.CLOSED
    # Soft-delete: the row is preserved with a deletion audit entry.
    assert case.versionInfo.get("action") == "deleted"


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Admin", "Moderator"])
def test_published_to_draft_revert_allowed_for_admin_moderator(role):
    user = create_user_with_role(f"rev-{role}", f"rev-{role}@example.com", role)
    case = _publishable_case(state=CaseState.PUBLISHED)

    response = _patch_state(_authed_client(user), case, CaseState.DRAFT)

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.DRAFT
    assert case.versionInfo.get("action") == "reverted_to_draft"


@pytest.mark.django_db
def test_in_review_to_draft_revert_allowed_for_caseworker():
    """Caseworkers may revert IN_REVIEW -> DRAFT."""
    user = create_user_with_role("cw-revert", "cw-revert@example.com", "Caseworker")
    case = _publishable_case(state=CaseState.IN_REVIEW)

    response = _patch_state(_authed_client(user), case, CaseState.DRAFT)

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.DRAFT


# ---------------------------------------------------------------------------
# v3 authz model: the single content-staff role (Caseworker) can transition to
# ANY state, including PUBLISHED and CLOSED. The old caseworker-confined-to-
# {DRAFT, IN_REVIEW} boundary is retired with the Caseworker/Moderator collapse.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_caseworker_can_publish():
    user = create_user_with_role("cw-pub", "cw-pub@example.com", "Caseworker")
    case = _publishable_case(state=CaseState.IN_REVIEW)

    response = _patch_state(_authed_client(user), case, CaseState.PUBLISHED)

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED


@pytest.mark.django_db
def test_caseworker_can_close():
    user = create_user_with_role("cw-close", "cw-close@example.com", "Caseworker")
    case = _publishable_case(state=CaseState.IN_REVIEW)

    response = _patch_state(_authed_client(user), case, CaseState.CLOSED)

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.CLOSED


# ---------------------------------------------------------------------------
# Publish gate failures (422) — reuse the model gates via publish()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_publish_rejected_when_missing_allegations_and_accused():
    user = create_user_with_role("mod-gate", "mod-gate@example.com", "Moderator")
    # DRAFT with no allegations, no accused entity, no description.
    case = Case.objects.create(
        title="Incomplete case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        key_allegations=[],
    )

    response = _patch_state(_authed_client(user), case, CaseState.PUBLISHED)

    assert response.status_code == 422
    assert "entities" in response.data
    assert "key_allegations" in response.data
    assert "description" in response.data
    case.refresh_from_db()
    assert case.state == CaseState.DRAFT


@pytest.mark.django_db
def test_publish_rejected_when_missing_accused_only():
    user = create_user_with_role("mod-gate2", "mod-gate2@example.com", "Moderator")
    case = Case.objects.create(
        title="Missing accused",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="Full description",
        key_allegations=["An allegation"],
    )

    response = _patch_state(_authed_client(user), case, CaseState.PUBLISHED)

    assert response.status_code == 422
    assert "entities" in response.data
    case.refresh_from_db()
    assert case.state == CaseState.DRAFT
