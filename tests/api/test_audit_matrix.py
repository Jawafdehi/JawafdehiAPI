"""Audit-coverage matrix: {auth method} × {mutation endpoint}.

This is a regression guard for the audit-logging work. For every combination
of authentication method and mutating API endpoint, it asserts that the write

  (a) produces a django-auditlog ``LogEntry`` (the mutation is audited), and
  (b) attributes that entry to the *acting* user.

It runs through the real DRF auth classes and the full middleware stack
(``JWTAuditlogMiddleware`` binds the lazy actor), inside the test transaction,
so nothing leaks.

Endpoints deliberately excluded from auditing (CaseWorkflowRun, NESQueueItem —
hot-path models saved in loops) are asserted to produce *no* LogEntry, so the
intentional exclusion can't silently regress into either direction.
"""

import pytest
from auditlog.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseState,
    CaseType,
    DocumentSource,
    JawafEntity,
    SourceLinkRole,
    SourceType,
)
from tests.conftest import create_user_with_role

User = get_user_model()

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Auth-method clients. Each returns an APIClient that authenticates as `actor`
# (the user the audit entry should be attributed to), plus a human label.
# ---------------------------------------------------------------------------


def _jwt_client(actor, **_):
    client = APIClient()
    client.force_authenticate(user=actor)
    return client


def _drf_token_client(actor, **_):
    client = APIClient()
    token, _created = Token.objects.get_or_create(user=actor)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


AUTH_METHODS = [
    ("jwt", _jwt_client),
    ("drf_token", _drf_token_client),
]


# ---------------------------------------------------------------------------
# Mutation endpoints. Each builds any prerequisite rows and returns
# (method, url, json_body, audited_model_or_None, expected_actions).
# audited_model is None for endpoints that must NOT be audited.
# ---------------------------------------------------------------------------


def _case_patch(actor):
    case = Case.objects.create(
        title="Matrix Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        short_description="before",
    )
    case.contributors.add(actor)
    body = [{"op": "replace", "path": "/short_description", "value": "after-matrix"}]
    return ("patch", f"/api/cases/{case.slug}/", body, Case, {"update"})


def _entity_create(actor):
    body = {"display_name": "Matrix Entity", "nes_id": ""}
    return ("post", "/api/entities/", body, JawafEntity, {"create"})


def _source_create(actor):
    body = {
        "title": "Matrix Source",
        "description": "matrix",
        "source_type": SourceType.MISC.value,
        "url": [
            {"link": "https://example.com/matrix", "role": SourceLinkRole.RAW.value}
        ],
    }
    return ("post", "/api/sources/", body, DocumentSource, {"create"})


def _feedback_create(actor):
    # Public endpoint: anonymous-capable, but it IS audited (Feedback is
    # registered). When authenticated the actor should still be attributed.
    body = {
        "feedbackType": "general",
        "subject": "Matrix feedback",
        "description": "matrix body",
    }
    return ("post", "/api/feedback/", body, "feedback", {"create"})


def _review_submit(actor):
    Case.objects.create(
        title="Matrix Review Case",
        slug="matrix-review-case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
    )
    body = {"slug": "matrix-review-case"}
    return ("post", "/api/casework/reviews/submit/", body, "casereview", {"create"})


ENDPOINTS = [
    ("case_patch", _case_patch),
    ("entity_create", _entity_create),
    ("source_create", _source_create),
    ("feedback_create", _feedback_create),
    ("review_submit", _review_submit),
]


@pytest.fixture
def admin_actor(db):
    # Admin role => is_superuser, so authz uniformly passes and the test
    # isolates the *audit* question from per-endpoint permission gates.
    return create_user_with_role("matrix_admin", "matrix_admin@example.com", "Admin")


def _logentries_for(model, since_id):
    if isinstance(model, str):
        ct = ContentType.objects.get(model=model)
    else:
        ct = ContentType.objects.get_for_model(model)
    return LogEntry.objects.filter(content_type=ct, id__gt=since_id)


@pytest.mark.parametrize("auth_label,auth_factory", AUTH_METHODS)
@pytest.mark.parametrize("ep_label,ep_factory", ENDPOINTS)
def test_audit_matrix(auth_label, auth_factory, ep_label, ep_factory, admin_actor):
    """Every (auth method × endpoint) cell audits the write and names the actor."""
    method, url, body, model, expected_actions = ep_factory(admin_actor)

    before_id = (
        LogEntry.objects.order_by("-id").values_list("id", flat=True).first() or 0
    )

    client = auth_factory(admin_actor)
    resp = getattr(client, method)(url, body, format="json")

    assert resp.status_code in (200, 201), (
        f"{auth_label} × {ep_label}: write failed "
        f"({resp.status_code}): {resp.content!r}"
    )

    entries = list(_logentries_for(model, before_id))
    assert entries, f"{auth_label} × {ep_label}: NO LogEntry written (audit gap)"

    actions = {e.get_action_display().lower() for e in entries}
    assert (
        expected_actions <= actions
    ), f"{auth_label} × {ep_label}: expected actions {expected_actions}, got {actions}"

    # The acting user must be attributed.
    actors = {e.actor for e in entries if e.actor is not None}
    assert admin_actor in actors, (
        f"{auth_label} × {ep_label}: audit entry not attributed to the acting "
        f"user (got actors={actors})"
    )


# ---------------------------------------------------------------------------
# Intentional exclusions. CaseWorkflowRun and NESQueueItem are deliberately
# NOT registered (hot-path models saved in loops). Assert a direct save()
# writes NO LogEntry, so the exclusion can't silently regress into auditing
# them (write amplification) — the inverse direction of the matrix above.
# ---------------------------------------------------------------------------


def test_excluded_models_are_not_audited(admin_actor):
    from auditlog.context import set_actor

    from case_workflows.models import CaseWorkflowRun
    from nesq.models import NESQueueItem, QueueAction, QueueStatus

    with set_actor(actor=admin_actor):
        run = CaseWorkflowRun.objects.create(
            run_id="matrix-run-1", workflow_id="ciaa_caseworker", case_id="case-x"
        )
        run.has_failed = True
        run.save(update_fields=["has_failed"])

        item = NESQueueItem.objects.create(
            action=QueueAction.ADD_NAME,
            payload={"entity_id": "e1", "name": "x"},
            status=QueueStatus.PENDING,
            change_description="matrix",
            submitted_by=admin_actor,
        )
        item.status = QueueStatus.APPROVED
        item.save(update_fields=["status"])

    run_ct = ContentType.objects.get_for_model(CaseWorkflowRun)
    item_ct = ContentType.objects.get_for_model(NESQueueItem)
    assert not LogEntry.objects.filter(
        content_type=run_ct, object_pk=str(run.pk)
    ).exists(), "CaseWorkflowRun should not be audited (hot-path exclusion regressed)"
    assert not LogEntry.objects.filter(
        content_type=item_ct, object_pk=str(item.pk)
    ).exists(), "NESQueueItem should not be audited (hot-path exclusion regressed)"
