"""Integration tests: `internal_notes` is staff/readonly-only.

internal_notes carries internal annotations (e.g. the NO_BIGO: justification
marker). It must NEVER appear in the public API payload — neither the case
detail (GET retrieve) nor the case list — for anonymous/unprivileged callers.
It IS exposed to authenticated staff (Admin/Moderator/Contributor) and the
org-wide ReadOnly role.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities, create_user_with_role

MARKER = "NO_BIGO: record_offence — आरोपपत्रमा बिगो रकम उल्लेख छैन"
LIST_URL = "/api/cases/"
DETAIL_URL = "/api/cases/{}/"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def _published_case():
    case = create_case_with_entities(
        title="Internal notes visibility case",
        alleged_entities=["entity:person/test-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="A published case used to assert internal_notes gating.",
        notes="Public notes shown on the site.",
        internal_notes=MARKER,
    )
    case.state = CaseState.PUBLISHED
    case.save()
    return case


def _client(role=None):
    client = APIClient()
    if role:
        user = create_user_with_role(f"u_{role}", f"u_{role}@example.com", role)
        client.force_authenticate(user=user)
    return client


def _list_row(response, case_id):
    data = response.json()
    rows = data["results"] if isinstance(data, dict) and "results" in data else data
    return next((r for r in rows if r["id"] == case_id), None)


# --- hidden from anonymous / public --------------------------------------


@pytest.mark.django_db
def test_internal_notes_absent_from_anonymous_detail():
    case = _published_case()
    resp = _client().get(DETAIL_URL.format(case.slug))
    assert resp.status_code == 200
    body = resp.json()
    assert "internal_notes" not in body
    # public `notes` is unaffected — regression guard.
    assert body.get("notes") == "Public notes shown on the site."


@pytest.mark.django_db
def test_internal_notes_absent_from_anonymous_list():
    case = _published_case()
    resp = _client().get(LIST_URL)
    assert resp.status_code == 200
    row = _list_row(resp, case.id)
    assert row is not None
    assert "internal_notes" not in row


# --- visible to staff (Admin/Moderator/Contributor) ----------------------


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Admin", "Moderator", "Contributor"])
def test_internal_notes_visible_to_staff_detail(role):
    case = _published_case()
    resp = _client(role).get(DETAIL_URL.format(case.slug))
    assert resp.status_code == 200
    assert resp.json().get("internal_notes") == MARKER


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["Admin", "Moderator", "Contributor"])
def test_internal_notes_visible_to_staff_list(role):
    case = _published_case()
    resp = _client(role).get(LIST_URL)
    assert resp.status_code == 200
    row = _list_row(resp, case.id)
    assert row is not None
    assert row.get("internal_notes") == MARKER


# --- visible to org-wide ReadOnly ----------------------------------------


@pytest.mark.django_db
def test_internal_notes_visible_to_readonly_detail():
    case = _published_case()
    resp = _client("ReadOnly").get(DETAIL_URL.format(case.slug))
    assert resp.status_code == 200
    assert resp.json().get("internal_notes") == MARKER


@pytest.mark.django_db
def test_internal_notes_visible_to_readonly_list():
    case = _published_case()
    resp = _client("ReadOnly").get(LIST_URL)
    assert resp.status_code == 200
    row = _list_row(resp, case.id)
    assert row is not None
    assert row.get("internal_notes") == MARKER
