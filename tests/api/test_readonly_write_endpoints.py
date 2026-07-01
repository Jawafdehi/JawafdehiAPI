"""Integration tests: the org-wide ReadOnly role cannot write via the case API
endpoints.

These endpoints gate writes on ``DjangoModelPermissions`` (POST -> add_*,
PATCH/PUT -> change_*) on top of authentication. A ReadOnly user is
authenticated and holds only ``view_*`` perms, so every write must be rejected
with 403 — the permission check fires before serializer validation, so the
payload shape is irrelevant for the denial cases.

NOTE: there is no ``/api/entities/`` endpoint anymore. Entities are owned by
the Nepal Entity Service (NES); the JawafEntity-backed endpoint was removed with
the model, so there are no entity write paths to gate here.
"""

import pytest
from django.contrib.auth.models import Permission
from django.core.cache import cache
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseType,
)
from tests.conftest import create_user_with_role


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the process-global cache around each test so cached querysets
    never read a stale set."""
    cache.clear()
    yield
    cache.clear()


def _authed_client(user):
    # OIDC-only migration: DRF token auth was removed. force_authenticate sets
    # request.user directly (bypassing the auth class), which is the
    # auth-scheme-agnostic way to exercise the permission/authorization logic
    # under test.
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _grant(user, codename):
    """Grant a single model permission directly to the user.

    The ReadOnly/Contributor *groups* get their perms from `create_groups`
    (an ops step that the test DB does not run), so writer tests grant the
    specific perm they exercise to keep the test self-contained and prove the
    DjangoModelPermissions gate ALLOWS when the perm is present.
    """
    user.user_permissions.add(Permission.objects.get(codename=codename))


# ---------------------------------------------------------------------------
# Case writes (POST /api/cases/)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_readonly_cannot_create_case():
    """ReadOnly holds only view_case, so POST /api/cases/ is rejected by
    DjangoModelPermissions (needs add_case) before any case is written."""
    readonly = create_user_with_role("ro_case", "ro_case@example.com", "ReadOnly")
    response = _authed_client(readonly).post(
        "/api/cases/",
        data={"title": "RO Should Not Create", "case_type": CaseType.CORRUPTION},
        format="json",
    )
    assert response.status_code == 403
    assert not Case.objects.filter(title="RO Should Not Create").exists()


@pytest.mark.django_db
def test_writer_with_perm_can_create_case():
    """A user holding add_case passes the DjangoModelPermissions gate (allow-path)."""
    contributor = create_user_with_role(
        "contrib_case", "contrib_case@example.com", "Caseworker"
    )
    _grant(contributor, "add_case")
    response = _authed_client(contributor).post(
        "/api/cases/",
        data={"title": "Contributor Case", "case_type": CaseType.CORRUPTION},
        format="json",
    )
    assert response.status_code == 201
    assert Case.objects.filter(title="Contributor Case").exists()
