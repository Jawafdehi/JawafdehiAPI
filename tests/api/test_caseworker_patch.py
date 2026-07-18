"""
Tests for PATCH /api/cases/{id}/ (RFC 6902 JSON Patch endpoint).
"""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from cases.api_views import CaseViewSet
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _authed_client(user) -> APIClient:
    # OIDC-only migration: DRF token auth was removed. force_authenticate sets
    # request.user directly (auth-scheme-agnostic) so the authorization logic
    # under test is still exercised.
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _contributor(name="rishi") -> User:
    return create_user_with_role(name, f"{name}@example.com", "Caseworker")


# ---------------------------------------------------------------------------
# Auth / permission tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_requires_authentication():
    case = _make_case()
    client = APIClient()
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "New"}],
        format="json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_patch_allowed_for_any_caseworker_without_assignment():
    # v3 authz: object-level case assignment is retired — any Caseworker can
    # patch any case without being assigned to it.
    case = _make_case()
    user = _contributor("sunita")
    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Edited"}],
        format="json",
    )
    assert response.status_code == 200
    case.refresh_from_db()
    assert case.title == "Edited"


# ---------------------------------------------------------------------------
# Successful patch operations
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_replace_scalar_field():
    user = _contributor("hari")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Updated title"}],
        format="json",
    )
    assert response.status_code == 200
    assert response.data["title"] == "Updated title"
    case.refresh_from_db()
    assert case.title == "Updated title"


@pytest.mark.django_db
def test_patch_replace_notes_persists_on_case_without_notes():
    # BB-28: the editor sends `replace /notes`, but the patch surface omitted
    # `notes` (no snapshot key, no serializer field, no scalar write), so the
    # patch failed with 400 "can't replace a non-existent object 'notes'".
    # Same class as BB-11 (a writable field missing from the patch surface).
    user = _contributor("bikash")
    case = _make_case()  # notes defaults to ""
    assert case.notes == ""

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/notes", "value": "internal note"}],
        format="json",
    )
    assert response.status_code == 200, response.data
    # The patched note is persisted to the Case row.
    case.refresh_from_db()
    assert case.notes == "internal note"
    # And it survives the editor's reload path — the casework read serializer
    # returns notes to casework viewers (BB-04). (The PATCH response body itself
    # blanks notes because CaseSerializer is built there without request context;
    # that's pre-existing and orthogonal to this persistence fix.)
    reload = client.get(URL.format(case.slug))
    assert reload.status_code == 200
    assert reload.data["notes"] == "internal note"


@pytest.mark.django_db
def test_patch_scalar_only_leaves_existing_notes_untouched():
    # A PATCH that does NOT touch /notes must not clobber an existing note: the
    # snapshot carries the current notes value, so a scalar-only edit round-trips
    # it back unchanged.
    user = _contributor("chandra")
    case = _make_case(notes="pre-existing internal note")

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Retitled"}],
        format="json",
    )
    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.title == "Retitled"
    assert case.notes == "pre-existing internal note"


@pytest.mark.django_db
def test_build_snapshot_coerces_null_notes_to_empty_string():
    # Regression (Gemini HIGH): a legacy/raw row whose notes reads back NULL must
    # not put ``None`` into the snapshot — CasePatchSerializer.notes is allow_blank
    # but NOT allow_null, so a None would 422 EVERY patch to that case. The column
    # is NOT NULL (default=""), so we exercise the coercion directly on the
    # snapshot builder with an in-memory None (which the ORM can't persist).
    case = _make_case()
    case.notes = None  # simulate a NULL read-back
    snapshot = CaseViewSet()._build_snapshot(case)
    assert snapshot["notes"] == ""


@pytest.mark.django_db
def test_patch_unrelated_field_survives_null_notes_row():
    # Regression (Gemini HIGH), end-to-end: with a row whose notes reads back NULL,
    # patching an UNRELATED field must return 200 (not 422). The NOT NULL column
    # can't hold NULL via the ORM, so we force the fetched instance's notes to None
    # and let the real _build_snapshot coercion run through the endpoint.
    user = _contributor("null-notes")
    case = _make_case(title="Original title")
    case.notes = None

    client = _authed_client(user)
    # get_object() is the single source of the case the view reads/writes; return
    # our NULL-notes instance so _build_snapshot sees None and must coerce it.
    with mock.patch.object(CaseViewSet, "get_object", return_value=case):
        response = client.patch(
            URL.format(case.slug),
            data=[{"op": "replace", "path": "/title", "value": "Renamed"}],
            format="json",
        )
    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.title == "Renamed"
    # The coerced-empty note landed as "" (never NULL), consistent with the column.
    assert case.notes == ""


@pytest.mark.django_db
def test_patch_replace_public_notes_persists_on_case_without_public_notes():
    # #4: the editor sends `replace /public_notes` (the public attribution +
    # human-written edit-dates block). Like /notes it must resolve against an
    # existing snapshot key, validate, and persist via the scalar write.
    user = _contributor("prakash")
    case = _make_case()  # public_notes defaults to ""
    assert case.public_notes == ""

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/public_notes",
                "value": "Documented by the Jawafdehi team. First published Shrawan 2082.",
            }
        ],
        format="json",
    )
    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.public_notes == (
        "Documented by the Jawafdehi team. First published Shrawan 2082."
    )


@pytest.mark.django_db
def test_public_notes_returned_to_anonymous_but_notes_blanked():
    # #4 core contract: unlike the internal ``notes`` (BB-04-gated to casework
    # viewers), ``public_notes`` is returned to EVERYONE. A published case read
    # anonymously carries the public byline verbatim while notes stays blank.
    case = _make_case(
        state=CaseState.PUBLISHED,
        public_notes="Documented by the Jawafdehi research team.",
        notes="internal-only reviewer note",
    )

    anon = APIClient()
    response = anon.get(URL.format(case.slug))
    assert response.status_code == 200, response.data
    assert response.data["public_notes"] == (
        "Documented by the Jawafdehi research team."
    )
    # The internal note is never leaked to the public reader.
    assert response.data["notes"] == ""


@pytest.mark.django_db
def test_patch_scalar_only_leaves_existing_public_notes_untouched():
    # A PATCH that does NOT touch /public_notes must round-trip it unchanged —
    # the snapshot carries the current value (mirrors the /notes guarantee).
    user = _contributor("gita")
    case = _make_case(public_notes="Documented by the field team.")

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Retitled"}],
        format="json",
    )
    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.title == "Retitled"
    assert case.public_notes == "Documented by the field team."


@pytest.mark.django_db
def test_build_snapshot_coerces_null_public_notes_to_empty_string():
    # Same NULL-read-back guard as notes: the column is NOT NULL (default=""),
    # but a legacy/raw row reading back NULL must become "" in the snapshot, else
    # CasePatchSerializer.public_notes (allow_blank, NOT allow_null) would 422
    # every subsequent patch to that case.
    case = _make_case()
    case.public_notes = None  # simulate a NULL read-back
    snapshot = CaseViewSet()._build_snapshot(case)
    assert snapshot["public_notes"] == ""


@pytest.mark.django_db
def test_patch_replace_timeline_item_title():
    user = _contributor("sita")
    case = _make_case(
        timeline=[
            {"date": "2024-01-01", "title": "First event"},
            {"date": "2024-02-01", "title": "Second event"},
        ]
    )

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/timeline/0/title", "value": "Renamed"}],
        format="json",
    )
    assert response.status_code == 200
    timeline = response.data["timeline"]
    assert timeline[0]["title"] == "Renamed"
    assert timeline[1]["title"] == "Second event"


@pytest.mark.django_db
def test_patch_timeline_preserves_date_bs_and_span_fields():
    """PATCH must not strip the optional date_bs/end_date/end_date_bs fields."""
    user = _contributor("kamala")
    case = _make_case()

    new_timeline = [
        {
            "date": "1989-07-14",
            "date_bs": "2046-03-30",
            "end_date": "2020-07-15",
            "end_date_bs": "2077-03-31",
            "title": "जाँच अवधि",
            "description": "Investigation period span.",
        },
        {
            "date": "2025-02-09",
            "date_bs": "2081-10-27",
            "title": "मुद्दा दर्ता",
        },
    ]
    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/timeline", "value": new_timeline}],
        format="json",
    )
    assert response.status_code == 200
    timeline = response.data["timeline"]
    assert timeline[0]["date_bs"] == "2046-03-30"
    assert timeline[0]["end_date"] == "2020-07-15"
    assert timeline[0]["end_date_bs"] == "2077-03-31"
    assert timeline[1]["date_bs"] == "2081-10-27"

    case.refresh_from_db()
    assert case.timeline[0]["end_date"] == "2020-07-15"
    assert case.timeline[0]["end_date_bs"] == "2077-03-31"
    assert case.timeline[1]["date_bs"] == "2081-10-27"


@pytest.mark.django_db
def test_patch_timeline_rejects_malformed_date_bs():
    """A malformed date_bs in a PATCHed timeline is rejected (422)."""
    user = _contributor("nabin")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/timeline",
                "value": [
                    {"date": "2025-02-09", "date_bs": "2081/10/27", "title": "X"}
                ],
            }
        ],
        format="json",
    )
    assert response.status_code == 422


@pytest.mark.django_db
def test_patch_timeline_rejects_malformed_iso_date():
    """A3: a non-ISO `date` in a PATCHed timeline item is rejected (422)."""
    user = _contributor("nabin-2")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/timeline",
                "value": [{"date": "09/02/2025", "title": "X"}],
            }
        ],
        format="json",
    )
    assert response.status_code == 422


@pytest.mark.django_db
def test_patch_rejects_invalid_court_cases():
    """A3: court_cases entries are validated (unknown court identifier -> 422)."""
    user = _contributor("nabin-3")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/court_cases",
                "value": ["not-a-real-court:123"],
            }
        ],
        format="json",
    )
    assert response.status_code == 422


@pytest.mark.django_db
def test_patch_court_cases_stores_iris():
    """court_cases takes canonical @id IRIs; stored on the reference join."""
    user = _contributor("nabin-court-1")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/court_cases",
                "value": [
                    "https://jawafdehi.org/courtcase/special/080-cr-0111",
                    "https://jawafdehi.org/courtcase/supreme/078-wc-0123",
                ],
            }
        ],
        format="json",
    )
    assert response.status_code == 200
    assert response.data["court_cases"] == [
        "https://jawafdehi.org/courtcase/special/080-cr-0111",
        "https://jawafdehi.org/courtcase/supreme/078-wc-0123",
    ]
    case.refresh_from_db()
    assert list(case.courtcase_references.values_list("courtcase_iri", flat=True)) == [
        "https://jawafdehi.org/courtcase/special/080-cr-0111",
        "https://jawafdehi.org/courtcase/supreme/078-wc-0123",
    ]


@pytest.mark.django_db
def test_patch_rejects_short_form_court_cases():
    """The legacy <court>:<number> short form is not accepted — IRIs only."""
    user = _contributor("nabin-court-3")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/court_cases",
                "value": ["special:080-CR-0111"],
            }
        ],
        format="json",
    )
    assert response.status_code == 422


@pytest.mark.django_db
def test_patch_scalar_field_does_not_wipe_court_cases():
    """A scalar-only PATCH must not rewrite the court-case reference join."""
    user = _contributor("nabin-court-2")
    case = _make_case(
        court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0111"]
    )

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "New title"}],
        format="json",
    )
    assert response.status_code == 200
    assert response.data["court_cases"] == [
        "https://jawafdehi.org/courtcase/special/080-cr-0111"
    ]
    assert case.courtcase_references.count() == 1


@pytest.mark.django_db
def test_patch_bigo_accepts_integer():
    """A3: bigo (embezzled amount) is a writable integer field."""
    user = _contributor("nabin-4")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/bigo", "value": 1250000}],
        format="json",
    )
    assert response.status_code == 200
    case.refresh_from_db()
    assert case.bigo == 1250000


@pytest.mark.django_db
def test_patch_add_appends_timeline_item():
    user = _contributor("ram")
    case = _make_case(timeline=[{"date": "2024-01-01", "title": "First"}])

    new_item = {"date": "2025-03-15", "title": "New event", "description": "Details"}
    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "add", "path": "/timeline/-", "value": new_item}],
        format="json",
    )
    assert response.status_code == 200
    assert len(response.data["timeline"]) == 2
    assert response.data["timeline"][-1]["title"] == "New event"


@pytest.mark.django_db
def test_patch_remove_timeline_item():
    user = _contributor("gita")
    case = _make_case(
        timeline=[
            {"date": "2024-01-01", "title": "Keep"},
            {"date": "2024-02-01", "title": "Remove me"},
        ]
    )

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "remove", "path": "/timeline/1"}],
        format="json",
    )
    assert response.status_code == 200
    timeline = response.data["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["title"] == "Keep"


@pytest.mark.django_db
def test_patch_add_entity_with_relationship_type():
    user = _contributor("kiran")
    case = _make_case()
    entity = "https://jawafdehi.org/entity/person/prachanda"

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {
                    "nes_id": entity,
                    "relationship_type": RelationshipType.ACCUSED,
                },
            }
        ],
        format="json",
    )
    assert response.status_code == 200
    entity_types = [e["type"] for e in response.data["entities"]]
    assert RelationshipType.ACCUSED in entity_types
    assert CaseEntityRelationship.objects.filter(
        case=case, nes_id=entity, relationship_type=RelationshipType.ACCUSED
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("wire_value", ["ACCUSED", "Accused", "aCcUsEd"])
def test_patch_add_entity_accepts_uppercase_relationship_type(wire_value):
    # The frontend sends UPPERCASE relationship_type values; the backend must
    # accept them case-insensitively and STORE/RETURN them lowercase.
    user = _contributor("kiran")
    case = _make_case()
    entity = "https://jawafdehi.org/entity/person/prachanda"

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {
                    "nes_id": entity,
                    "relationship_type": wire_value,
                },
            }
        ],
        format="json",
    )
    assert response.status_code == 200, response.data
    # Stored + returned as canonical lowercase ("accused").
    rel = CaseEntityRelationship.objects.get(case=case, nes_id=entity)
    assert rel.relationship_type == RelationshipType.ACCUSED == "accused"
    assert RelationshipType.ACCUSED in [e["type"] for e in response.data["entities"]]


@pytest.mark.django_db
def test_patch_add_location_entity():
    user = _contributor("kiran-loc")
    case = _make_case()
    entity = "https://jawafdehi.org/entity/location/district/kathmandu"

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {
                    "nes_id": entity,
                    "relationship_type": RelationshipType.LOCATION,
                    "notes": "Primary location",
                },
            }
        ],
        format="json",
    )
    assert response.status_code == 200
    entity_types = [e["type"] for e in response.data["entities"]]
    assert RelationshipType.LOCATION in entity_types
    rel = CaseEntityRelationship.objects.get(case=case, nes_id=entity)
    assert rel.relationship_type == RelationshipType.LOCATION
    assert rel.notes == "Primary location"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_400_for_malformed_patch_body():
    user = _contributor("sabita")
    case = _make_case()

    client = _authed_client(user)
    # Send a dict instead of a list — invalid RFC 6902
    response = client.patch(
        URL.format(case.slug),
        data={"op": "replace", "path": "/title", "value": "x"},
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_patch_400_for_invalid_json_patch_operation():
    user = _contributor("manish")
    case = _make_case()

    client = _authed_client(user)
    # Reference a path index that doesn't exist
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "remove", "path": "/timeline/99"}],
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_patch_caseworker_can_publish_complete_case():
    # v3 authz: a Caseworker has full publish powers (the old Moderator role
    # folded into Caseworker), so a complete case transitions DRAFT -> PUBLISHED.
    user = _contributor("deepak")
    case = _make_case(
        description="Detailed allegation description",
        key_allegations=["Primary allegation"],
    )
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id="https://jawafdehi.org/entity/person/ram-prasad-gautam",
        relationship_type=RelationshipType.ACCUSED,
    )

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": "PUBLISHED"}],
        format="json",
    )
    assert response.status_code == 200
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED


@pytest.mark.django_db
def test_patch_200_for_draft_to_in_review_transition():
    user = _contributor("deepak-2")
    case = _make_case(
        description="Detailed allegation description",
        key_allegations=["Primary allegation"],
    )
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id="https://jawafdehi.org/entity/person/ram-prasad-gautam",
        relationship_type=RelationshipType.ACCUSED,
    )

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": "IN_REVIEW"}],
        format="json",
    )

    assert response.status_code == 200
    assert response.data["state"] == CaseState.IN_REVIEW
    case.refresh_from_db()
    assert case.state == CaseState.IN_REVIEW


@pytest.mark.django_db
def test_patch_422_for_draft_to_in_review_missing_required_fields():
    user = _contributor("deepak-3")
    case = _make_case(key_allegations=[])

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": "IN_REVIEW"}],
        format="json",
    )

    # A2: model ValidationError on a state transition -> 422 with field-keyed
    # messages (was 400 before the transitions were unified).
    assert response.status_code == 422
    assert "entities" in response.data
    assert "key_allegations" in response.data
    case.refresh_from_db()
    assert case.state == CaseState.DRAFT


@pytest.mark.django_db
def test_patch_rejects_removed_case_id_path():
    # ``case_id`` has been dropped from the Case model (the slug is now the
    # case identifier). A patch targeting the removed field is rejected: the
    # snapshot has no ``/case_id`` member, so the JSON Patch fails to apply.
    user = _contributor("priya")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/case_id", "value": "case-tampered"}],
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_patch_422_for_blocked_path_case_type():
    user = _contributor("nisha")
    case = _make_case(case_type=CaseType.CORRUPTION)

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/case_type", "value": "CORRUPTION"}],
        format="json",
    )
    assert response.status_code == 422
    case.refresh_from_db()
    assert case.case_type == CaseType.CORRUPTION


@pytest.mark.django_db
def test_patch_422_for_invalid_nes_id():
    user = _contributor("anjali")
    case = _make_case()

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {"nes_id": "not-a-valid-id", "relationship_type": "accused"},
            }
        ],
        format="json",
    )
    assert response.status_code == 422


@pytest.mark.django_db
def test_patch_scalar_only_does_not_touch_entity_relationships():
    """Scalar-only PATCH must not delete/recreate entity relationships."""
    user = _contributor("binod")
    case = _make_case()

    entity = "https://jawafdehi.org/entity/person/bijaya-shumsher"
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id=entity,
        relationship_type=RelationshipType.ACCUSED,
    )
    rel_pk_before = CaseEntityRelationship.objects.get(case=case, nes_id=entity).pk

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Updated Title"}],
        format="json",
    )
    assert response.status_code == 200
    # The relationship row must be the exact same DB row (same pk).
    rel_pk_after = CaseEntityRelationship.objects.get(case=case, nes_id=entity).pk
    assert (
        rel_pk_before == rel_pk_after
    ), "Scalar-only PATCH must not delete and recreate entity relationships"


@pytest.mark.django_db
def test_patch_add_entity_with_outcome():
    """An entity bind can carry a verdict ``outcome`` (default is 'charged')."""
    user = _contributor("outcome-add")
    case = _make_case()
    entity = "https://jawafdehi.org/entity/person/test-accused-1"

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {
                    "nes_id": entity,
                    "relationship_type": RelationshipType.ACCUSED,
                    "outcome": "acquitted",
                },
            }
        ],
        format="json",
    )
    assert response.status_code == 200, response.data
    rel = CaseEntityRelationship.objects.get(case=case, nes_id=entity)
    assert rel.outcome == "acquitted"
    returned = [e for e in response.data["entities"] if e["nes_id"] == entity][0]
    assert returned["outcome"] == "acquitted"


@pytest.mark.django_db
@pytest.mark.parametrize("wire_value", ["ACQUITTED", "Acquitted"])
def test_patch_outcome_accepts_uppercase(wire_value):
    # The frontend sends UPPERCASE outcome values, mirroring relationship_type.
    user = _contributor("outcome-case")
    case = _make_case()
    entity = "https://jawafdehi.org/entity/person/test-accused-2"

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {
                    "nes_id": entity,
                    "relationship_type": "ACCUSED",
                    "outcome": wire_value,
                },
            }
        ],
        format="json",
    )
    assert response.status_code == 200, response.data
    assert CaseEntityRelationship.objects.get(case=case, nes_id=entity).outcome == (
        "acquitted"
    )


@pytest.mark.django_db
def test_patch_entities_preserves_untouched_outcome():
    """A /entities PATCH that touches one entity must not reset another entity's
    outcome to the default: the snapshot has to carry outcome through the
    delete/recreate round-trip."""
    user = _contributor("outcome-keep")
    case = _make_case()
    acquitted = "https://jawafdehi.org/entity/person/test-acquitted"
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id=acquitted,
        relationship_type=RelationshipType.ACCUSED,
        outcome="acquitted",
    )
    new_entity = "https://jawafdehi.org/entity/person/test-added"

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "add",
                "path": "/entities/-",
                "value": {
                    "nes_id": new_entity,
                    "relationship_type": RelationshipType.ACCUSED,
                },
            }
        ],
        format="json",
    )
    assert response.status_code == 200, response.data
    # The pre-existing acquitted bind keeps its outcome across the round-trip.
    assert (
        CaseEntityRelationship.objects.get(case=case, nes_id=acquitted).outcome
        == "acquitted"
    )
    # A newly added bind with no stated outcome defaults to 'charged'.
    assert (
        CaseEntityRelationship.objects.get(case=case, nes_id=new_entity).outcome
        == "charged"
    )


@pytest.mark.django_db
def test_patch_replace_entities_preserves_outcome_when_client_omits_it():
    """A whole-list /entities replace from an outcome-unaware client (one that
    doesn't echo outcome) must NOT reset an existing bind's outcome to 'charged'
    — the server preserves it by (nes_id, relationship_type)."""
    user = _contributor("outcome-preserve")
    case = _make_case()
    entity = "https://jawafdehi.org/entity/person/test-preserve"
    CaseEntityRelationship.objects.create(
        case=case,
        nes_id=entity,
        relationship_type=RelationshipType.ACCUSED,
        outcome="convicted",
    )

    client = _authed_client(user)
    response = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/entities",
                "value": [
                    # Re-sends the same bind but OMITS outcome.
                    {"nes_id": entity, "relationship_type": "ACCUSED", "notes": ""}
                ],
            }
        ],
        format="json",
    )
    assert response.status_code == 200, response.data
    assert (
        CaseEntityRelationship.objects.get(case=case, nes_id=entity).outcome
        == "convicted"
    )
