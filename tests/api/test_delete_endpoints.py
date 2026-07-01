"""Tests for the SOFT-DELETE plane on cases + document sources.

- ``DELETE /api/cases/{slug}/`` transitions the case to CLOSED (the platform's
  existing soft-delete pattern — ``Case.delete()`` is overridden; the ViewSet
  already hides CLOSED cases from every read) and returns 204. Auth: the
  cases.delete_case model permission (DjangoModelPermissions) plus the
  ``can_change_case`` authorization gate (admin/moderator or assigned
  contributor).
- ``DELETE /api/sources/{id}/`` flips ``is_deleted=True`` (never hard-deletes;
  the field + admin action already exist) and returns 204. Auth: the
  cases.delete_documentsource model permission.

force_authenticate is auth-scheme-agnostic; it exercises the permission /
authorization logic under test.
"""

import pytest
from django.contrib.auth.models import Permission
from django.core.cache import cache
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType, DocumentSource
from tests.conftest import create_user_with_role


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def _authed_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))


def _make_case(**kwargs) -> Case:
    defaults = dict(
        title="Deletable case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="Some description",
        short_description="Short",
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


# ---------------------------------------------------------------------------
# Case soft-delete (DELETE /api/cases/{slug}/)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_case_transitions_to_closed_204():
    # Admin satisfies both delete_case (model perm) and can_change_case (authz).
    admin = create_user_with_role("del_admin", "del_admin@example.com", "Admin")
    case = _make_case()
    resp = _authed_client(admin).delete(f"/api/cases/{case.slug}/")
    assert resp.status_code == 204
    case.refresh_from_db()
    # Soft-deleted: row survives, transitioned to CLOSED (never hard-removed).
    assert case.state == CaseState.CLOSED
    assert Case.objects.filter(pk=case.pk).exists()


@pytest.mark.django_db
def test_deleted_case_hidden_from_detail_and_list():
    admin = create_user_with_role("del_admin2", "del_admin2@example.com", "Admin")
    case = _make_case(state=CaseState.PUBLISHED, description="d", short_description="s")
    _authed_client(admin).delete(f"/api/cases/{case.slug}/")

    public = APIClient()
    # CLOSED cases are never exposed via the read plane.
    detail = public.get(f"/api/cases/{case.slug}/")
    assert detail.status_code == 404
    listing = public.get("/api/cases/")
    slugs = [c["slug"] for c in listing.data["results"]]
    assert case.slug not in slugs


@pytest.mark.django_db
def test_delete_case_requires_authentication():
    case = _make_case()
    resp = APIClient().delete(f"/api/cases/{case.slug}/")
    assert resp.status_code == 401
    case.refresh_from_db()
    assert case.state != CaseState.CLOSED


@pytest.mark.django_db
def test_delete_case_without_delete_perm_is_403():
    # ReadOnly holds only view_* perms -> DjangoModelPermissions denies DELETE.
    readonly = create_user_with_role("ro_del", "ro_del@example.com", "ReadOnly")
    case = _make_case()
    resp = _authed_client(readonly).delete(f"/api/cases/{case.slug}/")
    assert resp.status_code == 403
    case.refresh_from_db()
    assert case.state != CaseState.CLOSED


@pytest.mark.django_db
def test_delete_case_unassigned_caseworker_is_403():
    # Caseworker holds delete_case but is NOT a contributor -> can_change_case
    # denies (403), proving the authorization gate fires after the model-perm gate.
    worker = create_user_with_role("cw_del", "cw_del@example.com", "Caseworker")
    case = _make_case()
    resp = _authed_client(worker).delete(f"/api/cases/{case.slug}/")
    assert resp.status_code == 403
    case.refresh_from_db()
    assert case.state != CaseState.CLOSED


@pytest.mark.django_db
def test_delete_case_assigned_caseworker_ok():
    worker = create_user_with_role("cw_del2", "cw_del2@example.com", "Caseworker")
    case = _make_case()
    case.contributors.add(worker)
    resp = _authed_client(worker).delete(f"/api/cases/{case.slug}/")
    assert resp.status_code == 204
    case.refresh_from_db()
    assert case.state == CaseState.CLOSED


# ---------------------------------------------------------------------------
# DocumentSource soft-delete (DELETE /api/sources/{id}/)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_source_sets_is_deleted_204():
    writer = create_user_with_role("del_src", "del_src@example.com", "Caseworker")
    _grant(writer, "delete_documentsource")
    source = DocumentSource.objects.create(title="Deletable Source", source_type="MISC")
    resp = _authed_client(writer).delete(f"/api/sources/{source.id}/")
    assert resp.status_code == 204
    source.refresh_from_db()
    # Never hard-deleted; flagged soft-deleted.
    assert source.is_deleted is True
    assert DocumentSource.objects.filter(pk=source.id).exists()


@pytest.mark.django_db
def test_deleted_source_hidden_from_reads():
    writer = create_user_with_role("del_src2", "del_src2@example.com", "Caseworker")
    _grant(writer, "delete_documentsource")
    source = DocumentSource.objects.create(title="Vanishing Source", source_type="MISC")
    Case.objects.create(
        title="Ref Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        evidence=[{"source_id": source.source_id, "description": "ref"}],
    )
    client = _authed_client(writer)
    # Visible before delete (referenced by a published case).
    assert client.get(f"/api/sources/{source.id}/").status_code == 200
    assert client.delete(f"/api/sources/{source.id}/").status_code == 204
    # Gone from detail after soft-delete.
    assert client.get(f"/api/sources/{source.id}/").status_code == 404


@pytest.mark.django_db
def test_delete_source_requires_authentication():
    source = DocumentSource.objects.create(title="Auth Source", source_type="MISC")
    resp = APIClient().delete(f"/api/sources/{source.id}/")
    assert resp.status_code == 401
    source.refresh_from_db()
    assert source.is_deleted is False


@pytest.mark.django_db
def test_delete_source_without_delete_perm_is_403():
    readonly = create_user_with_role("ro_src_del", "ro_src_del@example.com", "ReadOnly")
    source = DocumentSource.objects.create(title="RO Source", source_type="MISC")
    resp = _authed_client(readonly).delete(f"/api/sources/{source.id}/")
    assert resp.status_code == 403
    source.refresh_from_db()
    assert source.is_deleted is False
