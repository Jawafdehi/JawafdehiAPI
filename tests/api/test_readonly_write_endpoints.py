"""Integration tests: the org-wide ReadOnly role cannot write via the case /
source / entity API endpoints.

These endpoints gate writes on ``DjangoModelPermissions`` (POST -> add_*,
PATCH/PUT -> change_*) on top of authentication. A ReadOnly user is
authenticated and holds only ``view_*`` perms, so every write must be rejected
with 403 — the permission check fires before serializer validation, so the
payload shape is irrelevant for the denial cases.
"""

import pytest
from django.contrib.auth.models import Permission
from django.core.cache import cache
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    DocumentSource,
    JawafEntity,
    RelationshipType,
)
from tests.conftest import create_user_with_role


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the process-global cache (e.g. public_entities_list) around each
    test so the entity list/get_object queryset never reads a stale set."""
    cache.clear()
    yield
    cache.clear()


def _authed_client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
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
# DocumentSource writes
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_readonly_cannot_create_source():
    readonly = create_user_with_role("ro_src", "ro_src@example.com", "ReadOnly")
    response = _authed_client(readonly).post(
        "/api/sources/",
        data={"title": "RO Should Fail", "source_type": "MISC"},
        format="json",
    )
    assert response.status_code == 403
    assert not DocumentSource.objects.filter(title="RO Should Fail").exists()


@pytest.mark.django_db
def test_readonly_cannot_update_source():
    readonly = create_user_with_role("ro_src2", "ro_src2@example.com", "ReadOnly")
    source = DocumentSource.objects.create(
        title="RO Existing Source", source_type="MISC"
    )
    response = _authed_client(readonly).patch(
        f"/api/sources/{source.id}/",
        data={"source_type": "COURT_ORDER"},
        format="json",
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_writer_with_perm_can_create_source():
    """A user holding add_documentsource passes the DjangoModelPermissions gate."""
    contributor = create_user_with_role(
        "contrib_src", "contrib_src@example.com", "Contributor"
    )
    _grant(contributor, "add_documentsource")
    response = _authed_client(contributor).post(
        "/api/sources/",
        data={"title": "Contributor Source", "source_type": "MISC"},
        format="json",
    )
    assert response.status_code == 201
    assert DocumentSource.objects.filter(title="Contributor Source").exists()


@pytest.mark.django_db
def test_writer_with_perm_can_update_source():
    """A user holding change_documentsource passes the PATCH gate (allow-path)."""
    writer = create_user_with_role(
        "writer_src", "writer_src@example.com", "Contributor"
    )
    _grant(writer, "change_documentsource")
    source = DocumentSource.objects.create(title="Editable Source", source_type="MISC")
    # The non-ReadOnly source queryset only exposes sources referenced by a
    # published/in-review case, so attach it to one via evidence.
    Case.objects.create(
        title="Pub Case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        evidence=[{"source_id": source.source_id, "description": "ref"}],
    )
    response = _authed_client(writer).patch(
        f"/api/sources/{source.id}/",
        data={"title": "Renamed Source"},
        format="json",
    )
    assert response.status_code == 200
    source.refresh_from_db()
    assert source.title == "Renamed Source"


# ---------------------------------------------------------------------------
# JawafEntity writes
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_readonly_cannot_create_entity():
    readonly = create_user_with_role("ro_ent", "ro_ent@example.com", "ReadOnly")
    response = _authed_client(readonly).post(
        "/api/entities/",
        data={"display_name": "RO Should Fail"},
        format="json",
    )
    assert response.status_code == 403
    assert not JawafEntity.objects.filter(display_name="RO Should Fail").exists()


@pytest.mark.django_db
def test_readonly_cannot_update_entity():
    readonly = create_user_with_role("ro_ent2", "ro_ent2@example.com", "ReadOnly")
    entity = JawafEntity.objects.create(display_name="RO Existing Entity")
    response = _authed_client(readonly).patch(
        f"/api/entities/{entity.id}/",
        data={"display_name": "RO Renamed"},
        format="json",
    )
    assert response.status_code == 403
    entity.refresh_from_db()
    assert entity.display_name == "RO Existing Entity"


@pytest.mark.django_db
def test_writer_with_perm_can_create_entity():
    """A user holding add_jawafentity passes the DjangoModelPermissions gate."""
    contributor = create_user_with_role(
        "contrib_ent", "contrib_ent@example.com", "Contributor"
    )
    _grant(contributor, "add_jawafentity")
    response = _authed_client(contributor).post(
        "/api/entities/",
        data={"display_name": "Contributor Entity"},
        format="json",
    )
    assert response.status_code == 201
    assert JawafEntity.objects.filter(display_name="Contributor Entity").exists()


@pytest.mark.django_db
def test_writer_with_perm_can_update_entity():
    """A user holding change_jawafentity passes the PATCH gate (allow-path)."""
    writer = create_user_with_role(
        "writer_ent", "writer_ent@example.com", "Contributor"
    )
    _grant(writer, "change_jawafentity")
    entity = JawafEntity.objects.create(display_name="Editable Entity")
    # The non-ReadOnly entity queryset (for partial_update) only exposes entities
    # appearing in a published case, so attach it to one.
    case = Case.objects.create(
        title="Pub Case Ent", case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED
    )
    CaseEntityRelationship.objects.create(
        case=case, entity=entity, relationship_type=RelationshipType.ACCUSED
    )
    response = _authed_client(writer).patch(
        f"/api/entities/{entity.id}/",
        data={"display_name": "Renamed Entity"},
        format="json",
    )
    assert response.status_code == 200
    entity.refresh_from_db()
    assert entity.display_name == "Renamed Entity"


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
        "contrib_case", "contrib_case@example.com", "Contributor"
    )
    _grant(contributor, "add_case")
    response = _authed_client(contributor).post(
        "/api/cases/",
        data={"title": "Contributor Case", "case_type": CaseType.CORRUPTION},
        format="json",
    )
    assert response.status_code == 201
    assert Case.objects.filter(title="Contributor Case").exists()
