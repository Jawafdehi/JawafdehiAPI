"""End-to-end tests for NGM court-case existence enforcement on case writes.

`ngm.services.court_case_exists` is monkeypatched (the test environment has no NGM
database), so these assert the *wiring and scoping* of the check, not NGM itself.
"""

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from tests.conftest import create_user_with_role

PATCH_URL = "/api/cases/{}/"
POST_URL = "/api/cases/"


def _authed_client(user) -> APIClient:
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _contributor(name="rishi"):
    return create_user_with_role(name, f"{name}@example.com", "Contributor")


def _patch_exists(monkeypatch, fn):
    import ngm.services as ngm_services

    monkeypatch.setattr(ngm_services, "court_case_exists", fn)


# ---------------------------------------------------------------------------
# PATCH: existence enforced only when /court_cases is touched
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_court_cases_accepts_value_present_in_ngm(monkeypatch):
    _patch_exists(monkeypatch, lambda ci, cn: True)
    user = _contributor("hari")
    case = Case.objects.create(
        title="T", case_type=CaseType.CORRUPTION, state=CaseState.DRAFT
    )
    case.contributors.add(user)

    resp = _authed_client(user).patch(
        PATCH_URL.format(case.slug),
        data=[
            {"op": "replace", "path": "/court_cases", "value": ["special:081-CR-0095"]}
        ],
        format="json",
    )

    assert resp.status_code == 200
    case.refresh_from_db()
    assert case.court_cases == ["special:081-CR-0095"]


@pytest.mark.django_db
def test_patch_court_cases_rejects_value_missing_from_ngm(monkeypatch):
    _patch_exists(monkeypatch, lambda ci, cn: False)
    user = _contributor("sita")
    case = Case.objects.create(
        title="T", case_type=CaseType.CORRUPTION, state=CaseState.DRAFT
    )
    case.contributors.add(user)

    resp = _authed_client(user).patch(
        PATCH_URL.format(case.slug),
        data=[
            {"op": "replace", "path": "/court_cases", "value": ["special:O81-CR-0095"]}
        ],
        format="json",
    )

    assert resp.status_code == 422
    assert "not found in NGM" in resp.data["detail"]
    case.refresh_from_db()
    assert not case.court_cases  # rejected value was not persisted


@pytest.mark.django_db
def test_patch_unrelated_field_skips_ngm_check_for_legacy_court_cases(monkeypatch):
    """A scalar-only PATCH must NOT re-validate the case's existing court_cases,
    so legacy references that predate NGM coverage stay editable."""
    calls = []

    def spy(ci, cn):
        calls.append((ci, cn))
        return False  # even if called, would reject — but it must not be called

    _patch_exists(monkeypatch, spy)
    user = _contributor("gita")
    # Seed a legacy reference NGM does not have (created via ORM, bypassing checks).
    case = Case.objects.create(
        title="T",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        court_cases=["supreme:081-CR-2026"],
    )
    case.contributors.add(user)

    resp = _authed_client(user).patch(
        PATCH_URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Renamed"}],
        format="json",
    )

    assert resp.status_code == 200
    assert calls == []  # NGM existence check never ran for an untouched field
    case.refresh_from_db()
    assert case.title == "Renamed"
    assert case.court_cases == ["supreme:081-CR-2026"]


# ---------------------------------------------------------------------------
# POST: existence enforced on create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_post_create_rejects_court_case_missing_from_ngm(monkeypatch):
    _patch_exists(monkeypatch, lambda ci, cn: False)
    user = _contributor("ram")

    resp = _authed_client(user).post(
        POST_URL,
        data={
            "title": "New case",
            "case_type": CaseType.CORRUPTION,
            "court_cases": ["special:081-CR-9999"],
        },
        format="json",
    )

    assert resp.status_code == 422
    assert "court_cases" in resp.data


@pytest.mark.django_db
def test_post_create_accepts_court_case_present_in_ngm(monkeypatch):
    _patch_exists(monkeypatch, lambda ci, cn: True)
    user = _contributor("shyam")

    resp = _authed_client(user).post(
        POST_URL,
        data={
            "title": "New case",
            "case_type": CaseType.CORRUPTION,
            "court_cases": ["special:081-CR-0095"],
        },
        format="json",
    )

    assert resp.status_code == 201
    case = Case.objects.get(pk=resp.data["id"])
    assert case.court_cases == ["special:081-CR-0095"]
