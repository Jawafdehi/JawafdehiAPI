"""``Case.tags_source`` — the pre-canonicalisation snapshot of ``Case.tags``.

Written once by ``rebuild_case_tags`` before it rewrites ``tags`` into canonical
vocabulary ids. It exists to make that rewrite reversible and auditable, and it
carries values deliberately removed from the public tag field (people's names,
organisation names), so the contract is: writable only by the rebuild command,
never by a form, never in a public response.
"""

from __future__ import annotations

import pytest
from tests.conftest import create_user_with_role
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType


@pytest.fixture
def published_case(db) -> Case:
    return Case.objects.create(
        title="बागमती नगर जग्गा प्रकरण",
        slug="tags-source-fixture",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        tags=["Land Management", "Bagmati"],
    )


def test_defaults_to_null_not_empty_list(published_case: Case) -> None:
    """NULL means "never snapshotted", which is distinct from "snapshotted as
    empty". A default of ``[]`` would erase that difference and make a re-run of
    the rebuild unable to tell a fresh case from a migrated one."""
    published_case.refresh_from_db()
    assert published_case.tags_source is None


def test_round_trips_the_original_free_text(published_case: Case) -> None:
    """It must hold the raw strings verbatim — including the values that get
    dropped from ``tags`` — or it is not a rollback path."""
    published_case.tags_source = ["Land Management", "CIAA", "K.P. Sharma Oli"]
    published_case.save(update_fields=["tags_source"])
    published_case.refresh_from_db()
    assert published_case.tags_source == ["Land Management", "CIAA", "K.P. Sharma Oli"]
    # Independent of ``tags`` — canonicalising one must not touch the other.
    assert published_case.tags == ["Land Management", "Bagmati"]


def test_not_editable_so_no_modelform_can_write_it() -> None:
    """``CaseAdminForm`` is built with ``fields = "__all__"``. Without
    ``editable=False`` this snapshot would render in the admin as a writable JSON
    textarea, letting a caseworker silently overwrite the audit trail."""
    field = Case._meta.get_field("tags_source")
    assert field.editable is False

    from cases.admin import CaseAdminForm

    assert "tags_source" not in CaseAdminForm.base_fields


def test_visible_in_admin_as_readonly() -> None:
    """Not editable, but an audit trail nobody can see is useless — "what was this
    tagged before?" is the question it exists to answer."""
    from cases.admin import CaseAdmin

    assert "tags_source" in CaseAdmin.readonly_fields
    content_fields = next(
        opts["fields"] for name, opts in CaseAdmin.fieldsets if name == "Content"
    )
    assert "tags_source" in content_fields


@pytest.mark.django_db
def test_absent_from_the_public_case_api(published_case: Case) -> None:
    """It carries the person/organisation names removed from ``tags``; leaking them
    back through the case API would defeat the point of removing them."""
    published_case.tags_source = ["sashikanta jha", "K.P. Sharma Oli"]
    published_case.save(update_fields=["tags_source"])

    client = APIClient()
    detail = client.get(f"/api/cases/{published_case.slug}/")
    assert detail.status_code == 200
    assert "tags_source" not in detail.data
    assert "tags" in detail.data  # the public field is unaffected

    listing = client.get("/api/cases/")
    assert listing.status_code == 200
    body = listing.json()
    rows = body["results"] if isinstance(body, dict) else body
    assert all("tags_source" not in row for row in rows)


@pytest.mark.django_db
def test_absent_from_the_caseworker_api(published_case: Case) -> None:
    """The caseworker sees a richer serializer on the SAME ``/api/cases/`` route, so
    "it's hidden from the public" is not enough — check the privileged view too.
    The rebuild command owns this field; no API surface should offer it."""
    user = create_user_with_role("bimala", "bimala@example.com", "Caseworker")
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.get(f"/api/cases/{published_case.slug}/")
    assert resp.status_code == 200, resp.data
    assert "tags_source" not in resp.data
    # The caseworker view is genuinely the richer one — guard against this test
    # silently passing because it fell back to the public serializer.
    assert "notes" in resp.data


@pytest.mark.django_db
def test_patch_cannot_write_it(published_case: Case) -> None:
    """A JSON-Patch op naming ``/tags_source`` must not persist. It is not in
    ``_PATCH_SCALAR_FIELDS``, so it should be rejected or ignored — never applied."""
    user = create_user_with_role("gita", "gita@example.com", "Caseworker")
    client = APIClient()
    client.force_authenticate(user=user)

    client.patch(
        f"/api/cases/{published_case.slug}/",
        data=[{"op": "replace", "path": "/tags_source", "value": ["forged"]}],
        format="json",
    )
    published_case.refresh_from_db()
    assert published_case.tags_source is None
