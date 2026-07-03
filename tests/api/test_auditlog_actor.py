"""
Audit-trail coverage for the cases write API.

Regression tests for two defects on ``CaseViewSet.partial_update``:

1. Scalar content edits are persisted with ``Case.objects.update()``, which bypasses
   ``post_save`` — so auditlog never logged content changes (only workflow/state saves,
   which go through ``Case.save()``). See ``log_bulk_update``.
2. ``AuditlogMiddleware`` reads ``request.user`` before DRF authentication runs, so every
   API-driven ``LogEntry`` was written with ``actor=NULL``. See ``AuditlogActorMixin``.
"""

import pytest
from auditlog.models import LogEntry
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


def _make_case(**kwargs) -> Case:
    defaults = dict(
        title="Test case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="Some description",
        short_description="Short",
        timeline=[{"date": "2024-01-01", "title": "Event one"}],
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _contributor(name="rishi") -> User:
    return create_user_with_role(name, f"{name}@example.com", "Caseworker")


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _case_updates(case):
    """UPDATE log entries for a given Case, newest first."""
    return LogEntry.objects.get_for_object(case).filter(action=LogEntry.Action.UPDATE)


# ---------------------------------------------------------------------------
# Defect 1 — content PATCHes must be audit-logged
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_content_patch_creates_audit_entry():
    """A scalar content edit (persisted via QuerySet.update) is logged."""
    user = _contributor("hari")
    case = _make_case(description="Original description")
    case.contributors.add(user)

    before = _case_updates(case).count()
    response = _authed_client(user).patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/description", "value": "Amended description"}],
        format="json",
    )
    assert response.status_code == 200

    entries = _case_updates(case)
    assert entries.count() == before + 1
    changes = entries.first().changes
    # auditlog stores changes as {field: [old, new]}; only the changed field appears.
    assert "description" in changes
    assert changes["description"][0] == "Original description"
    assert changes["description"][1] == "Amended description"


@pytest.mark.django_db
def test_content_patch_logs_only_changed_fields():
    """The diff is scoped to the fields the patch actually touched."""
    user = _contributor("gita")
    case = _make_case(title="Keep me", description="Change me")
    case.contributors.add(user)

    response = _authed_client(user).patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/description", "value": "Changed"}],
        format="json",
    )
    assert response.status_code == 200

    changes = _case_updates(case).first().changes
    # The write rewrites every scalar column, but the diff only carries genuinely
    # changed fields: description is in, the untouched title is not.
    assert "description" in changes
    assert "title" not in changes


@pytest.mark.django_db
def test_scalar_only_patch_does_not_log_when_unchanged():
    """A no-op replace (same value) must not manufacture a spurious log entry."""
    user = _contributor("mina")
    # court_cases defaults to NULL but the endpoint normalizes it to [] on write,
    # which is itself a real (loggable) column change. Pre-set it so the patch is a
    # genuine no-op and we can assert the "no diff -> no entry" guarantee.
    case = _make_case(description="Same", court_cases=[])
    case.contributors.add(user)

    before = _case_updates(case).count()
    response = _authed_client(user).patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/description", "value": "Same"}],
        format="json",
    )
    assert response.status_code == 200
    assert _case_updates(case).count() == before  # no diff -> no entry


# ---------------------------------------------------------------------------
# Defect 2 — the actor must be attributed (not NULL)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_content_patch_entry_attributes_actor():
    user = _contributor("bikash")
    case = _make_case(description="Original")
    case.contributors.add(user)

    response = _authed_client(user).patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/description", "value": "Edited"}],
        format="json",
    )
    assert response.status_code == 200

    entry = _case_updates(case).first()
    assert entry.actor_id == user.id


@pytest.mark.django_db
def test_state_transition_entry_attributes_actor():
    """The workflow-save path (Case.save) must also capture the actor."""
    user = _contributor("puja")
    # DRAFT->IN_REVIEW enforces the publish gate (accused entity + allegations +
    # description), so build a case that satisfies it.
    case = _make_case(description="Ready for review", key_allegations=["Primary allegation"])
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id="https://jawafdehi.org/entity/person/ram-prasad-gautam",
        relationship_type=RelationshipType.ACCUSED,
    )
    case.contributors.add(user)

    response = _authed_client(user).patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": CaseState.IN_REVIEW}],
        format="json",
    )
    assert response.status_code == 200, response.data

    state_entries = [e for e in _case_updates(case) if "state" in (e.changes or {})]
    assert state_entries, "expected a LogEntry recording the state change"
    assert all(e.actor_id == user.id for e in state_entries)
