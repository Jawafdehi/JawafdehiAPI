"""The structured public byline: authors, publish date, and edit history.

Replaces the free-text ``Case.public_notes`` attribution. Covers the publish
gate, the author join's replace semantics, the display-name snapshot, the
casework-only ``user_id`` boundary, and the author-picker endpoint.
"""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseAuthor,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from tests.byline import credit_author
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"
PICKER_URL = "/api/case-authors/"


def _make_case(**kwargs) -> Case:
    """A case complete except for the byline — one author short of publishable."""
    defaults = dict(
        title="Byline case",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
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
    return case


def _caseworker(name="byline-worker"):
    return create_user_with_role(name, f"{name}@example.com", "Caseworker")


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _patch(client, case, ops, **kwargs):
    return client.patch(URL.format(case.slug), data=ops, format="json", **kwargs)


# ---------------------------------------------------------------------------
# The publish gate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_publish_is_blocked_without_authors_or_publish_date():
    case = _make_case()
    case.state = CaseState.PUBLISHED

    with pytest.raises(ValidationError) as exc:
        case.validate()

    errors = exc.value.message_dict
    assert "authors" in errors
    assert "case_publish_date" in errors


@pytest.mark.django_db
def test_publish_is_blocked_with_authors_but_no_publish_date():
    """The two halves of the byline gate are independent."""
    case = _make_case()
    credit_author(case, publish_date=None)
    case.state = CaseState.PUBLISHED

    with pytest.raises(ValidationError) as exc:
        case.validate()

    errors = exc.value.message_dict
    assert "case_publish_date" in errors
    assert "authors" not in errors


@pytest.mark.django_db
def test_publish_is_blocked_with_publish_date_but_no_authors():
    case = _make_case(case_publish_date=date(2026, 8, 1))
    case.state = CaseState.PUBLISHED

    with pytest.raises(ValidationError) as exc:
        case.validate()

    errors = exc.value.message_dict
    assert "authors" in errors
    assert "case_publish_date" not in errors


@pytest.mark.django_db
def test_in_review_is_gated_too_not_just_published():
    """The gate is the shared IN_REVIEW/PUBLISHED block, not publish-only."""
    case = _make_case()
    case.state = CaseState.IN_REVIEW

    with pytest.raises(ValidationError) as exc:
        case.validate()

    assert "authors" in exc.value.message_dict


@pytest.mark.django_db
def test_draft_needs_no_byline():
    """A DRAFT is exempt — a case is researched before it is attributed."""
    case = _make_case()
    case.validate()  # must not raise
    assert case.state == CaseState.DRAFT


@pytest.mark.django_db
def test_a_complete_byline_publishes():
    case = _make_case()
    credit_author(case)
    case.publish()

    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED


@pytest.mark.django_db
def test_existing_published_case_without_a_byline_still_saves():
    """The ~72 legacy cases must not start rejecting ordinary content edits.

    ``Case.save()`` deliberately does not call ``validate()`` — only transitions
    do — so a backfill that lands after the rule cannot break editing in the
    meantime. This pins that, because making save() validate would be an easy and
    catastrophic "cleanup".
    """
    case = _make_case(state=CaseState.PUBLISHED)
    assert not case.authors and case.case_publish_date is None

    case.title = "Edited without a byline"
    case.save()

    case.refresh_from_db()
    assert case.title == "Edited without a byline"


@pytest.mark.django_db
def test_republishing_a_legacy_case_does_require_a_byline():
    """The other side of the coin: a transition re-runs the gate."""
    case = _make_case(state=CaseState.PUBLISHED)
    case.state = CaseState.DRAFT
    case.save()

    with pytest.raises(ValidationError) as exc:
        case.publish()

    assert "authors" in exc.value.message_dict


# ---------------------------------------------------------------------------
# The CaseAuthor join
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_display_name_is_snapshotted_from_the_account():
    user = get_user_model().objects.create_user(
        username="rujit", first_name="Rujit", last_name="Kafle"
    )
    case = _make_case()
    credit = CaseAuthor.objects.create(case=case, user=user)

    assert credit.display_name == "Rujit Kafle"


@pytest.mark.django_db
def test_display_name_falls_back_to_username_when_there_is_no_full_name():
    user = get_user_model().objects.create_user(username="mononym")
    case = _make_case()
    credit = CaseAuthor.objects.create(case=case, user=user)

    assert credit.display_name == "mononym"


@pytest.mark.django_db
def test_renaming_an_account_does_not_rewrite_a_published_byline():
    """The whole point of snapshotting rather than reading through."""
    user = get_user_model().objects.create_user(
        username="sambhav", first_name="Sambhav", last_name="Koirala"
    )
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=user)

    user.last_name = "Koirala-Sharma"
    user.save()

    case.refresh_from_db()
    assert case.authors[0]["display_name"] == "Sambhav Koirala"


@pytest.mark.django_db
def test_deleting_a_credited_account_is_refused():
    """PROTECT, not SET_NULL: losing the row would silently strip a byline."""
    user = get_user_model().objects.create_user(username="subodh")
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=user)

    with pytest.raises(ProtectedError):
        user.delete()


@pytest.mark.django_db
def test_authors_property_orders_by_ordinal_not_insertion():
    first = get_user_model().objects.create_user(username="second-added")
    second = get_user_model().objects.create_user(username="first-added")
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=first, ordinal=1)
    CaseAuthor.objects.create(case=case, user=second, ordinal=0)

    case.refresh_from_db()
    assert [a["display_name"] for a in case.authors] == [
        "first-added",
        "second-added",
    ]


@pytest.mark.django_db
def test_assigning_authors_deduplicates_on_user():
    """A double-click in the picker must not 500 on the unique constraint."""
    user = get_user_model().objects.create_user(username="dupe")
    case = _make_case()
    case.authors = [{"user_id": user.id}, {"user_id": user.id}]
    case.save()

    case.refresh_from_db()
    assert len(case.authors) == 1


@pytest.mark.django_db
def test_authors_on_an_unsaved_case_is_empty_rather_than_raising():
    """``validate()`` reads the property; an unsaved instance has no reverse rows."""
    assert Case(title="Unsaved").authors == []


@pytest.mark.django_db
def test_rewriting_the_same_authors_preserves_the_snapshotted_name():
    """A no-op re-send must not re-derive names from since-renamed accounts."""
    user = get_user_model().objects.create_user(
        username="niroj", first_name="Niroj", last_name="Aryal"
    )
    case = _make_case()
    case.authors = [{"user_id": user.id}]
    case.save()

    user.first_name = "N."
    user.save()

    # Same list, but with a credit_note — forces a real rewrite, not a no-op.
    case.authors = [{"user_id": user.id, "credit_note": "BALLB 5th Year Student"}]
    case.save()

    case.refresh_from_db()
    assert case.authors[0]["display_name"] == "Niroj Aryal"
    assert case.authors[0]["credit_note"] == "BALLB 5th Year Student"


# ---------------------------------------------------------------------------
# PATCH round-trip
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_sets_authors_publish_date_and_edit_history():
    user = _caseworker()
    case = _make_case()
    response = _patch(
        _client(user),
        case,
        [
            {
                "op": "replace",
                "path": "/authors",
                "value": [{"user_id": user.id, "credit_note": "Research lead"}],
            },
            {"op": "replace", "path": "/case_publish_date", "value": "2026-08-14"},
            {
                "op": "replace",
                "path": "/public_edit_history",
                "value": [{"date": "2026-08-15", "remarks": "Corrected the bigo."}],
            },
        ],
    )

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert [a["credit_note"] for a in case.authors] == ["Research lead"]
    assert case.case_publish_date == date(2026, 8, 14)
    assert case.public_edit_history == [
        {"date": "2026-08-15", "remarks": "Corrected the bigo."}
    ]


@pytest.mark.django_db
def test_patch_can_set_the_byline_and_publish_in_one_request():
    """Joins are written before the transition, so one PATCH can do both."""
    user = _caseworker("byline-and-publish")
    case = _make_case()
    response = _patch(
        _client(user),
        case,
        [
            {"op": "replace", "path": "/authors", "value": [{"user_id": user.id}]},
            {"op": "replace", "path": "/case_publish_date", "value": "2026-08-14"},
            {"op": "replace", "path": "/state", "value": CaseState.PUBLISHED},
        ],
    )

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED


@pytest.mark.django_db
def test_scalar_only_patch_leaves_the_author_list_untouched():
    """Joins are rewritten only when an op targets their path."""
    user = _caseworker("untouched")
    case = _make_case()
    credit_author(case)
    before = case.authors

    response = _patch(
        _client(user), case, [{"op": "replace", "path": "/title", "value": "Retitled"}]
    )

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.authors == before


@pytest.mark.django_db
def test_patch_with_an_unknown_user_id_is_a_422_not_a_500():
    user = _caseworker("bad-id")
    case = _make_case()
    response = _patch(
        _client(user),
        case,
        [{"op": "replace", "path": "/authors", "value": [{"user_id": 9_999_999}]}],
    )

    assert response.status_code == 422, response.data
    assert "authors" in response.data


@pytest.mark.django_db
def test_patch_ignores_a_client_supplied_display_name():
    """A client must not be able to publish an arbitrary name against an id."""
    user = _caseworker("no-spoofing")
    user.first_name, user.last_name = "Real", "Name"
    user.save()
    case = _make_case()

    response = _patch(
        _client(user),
        case,
        [
            {
                "op": "replace",
                "path": "/authors",
                "value": [{"user_id": user.id, "display_name": "Someone Else"}],
            }
        ],
    )

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.authors[0]["display_name"] == "Real Name"


@pytest.mark.django_db
def test_patch_rejects_an_edit_history_entry_with_a_bad_date():
    user = _caseworker("bad-date")
    case = _make_case()
    response = _patch(
        _client(user),
        case,
        [
            {
                "op": "replace",
                "path": "/public_edit_history",
                "value": [{"date": "14 Bhadra", "remarks": "Something"}],
            }
        ],
    )

    assert response.status_code == 422, response.data


@pytest.mark.django_db
def test_patch_rejects_an_edit_history_entry_with_empty_remarks():
    user = _caseworker("empty-remarks")
    case = _make_case()
    response = _patch(
        _client(user),
        case,
        [
            {
                "op": "replace",
                "path": "/public_edit_history",
                "value": [{"date": "2026-08-15", "remarks": "   "}],
            }
        ],
    )

    assert response.status_code == 422, response.data


@pytest.mark.django_db
def test_publish_date_can_be_cleared_back_to_null_on_a_draft():
    """The column is nullable; only the transition gate requires a value."""
    user = _caseworker("clear-date")
    case = _make_case(case_publish_date=date(2026, 8, 1))

    response = _patch(
        _client(user),
        case,
        [{"op": "replace", "path": "/case_publish_date", "value": None}],
    )

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.case_publish_date is None


# ---------------------------------------------------------------------------
# Read boundary
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_anonymous_reader_gets_the_byline_but_not_account_ids():
    case = _make_case(state=CaseState.PUBLISHED)
    credit_author(case, credit_note="BALLB 4th Year Student")

    response = APIClient().get(URL.format(case.slug))

    assert response.status_code == 200
    author = response.data["authors"][0]
    assert author["display_name"] == "Byline Author"
    assert author["credit_note"] == "BALLB 4th Year Student"
    assert "user_id" not in author
    assert response.data["case_publish_date"] == "2026-08-01"


@pytest.mark.django_db
def test_casework_reader_gets_user_id_so_the_editor_can_round_trip():
    case = _make_case(state=CaseState.PUBLISHED)
    credit_author(case)

    response = _client(_caseworker("reader")).get(URL.format(case.slug))

    assert response.status_code == 200
    assert "user_id" in response.data["authors"][0]


# ---------------------------------------------------------------------------
# The author picker
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_picker_lists_active_accounts_for_casework():
    user = _caseworker("picker")
    response = _client(user).get(PICKER_URL)

    assert response.status_code == 200
    assert any(row["id"] == user.id for row in response.data)


@pytest.mark.django_db
def test_picker_is_closed_to_anonymous_callers():
    assert APIClient().get(PICKER_URL).status_code in (401, 403)


@pytest.mark.django_db
def test_picker_is_closed_to_authenticated_non_staff():
    outsider = get_user_model().objects.create_user(username="outsider")
    assert _client(outsider).get(PICKER_URL).status_code == 403


@pytest.mark.django_db
def test_picker_omits_deactivated_accounts():
    user = _caseworker("picker-active")
    gone = get_user_model().objects.create_user(username="departed", is_active=False)

    response = _client(user).get(PICKER_URL)

    assert response.status_code == 200
    assert all(row["id"] != gone.id for row in response.data)


@pytest.mark.django_db
def test_picker_search_matches_names_and_usernames():
    user = _caseworker("picker-search")
    target = get_user_model().objects.create_user(
        username="skandel", first_name="Subodh", last_name="Kandel"
    )

    by_name = _client(user).get(PICKER_URL, {"search": "Subodh"})
    by_username = _client(user).get(PICKER_URL, {"search": "skandel"})

    assert [row["id"] for row in by_name.data] == [target.id]
    assert [row["id"] for row in by_username.data] == [target.id]


@pytest.mark.django_db
def test_picker_is_not_truncated_by_the_default_page_size():
    """The default PAGE_SIZE is 20; a silently-truncated picker is the bug this
    endpoint's ``pagination_class = None`` exists to prevent."""
    user = _caseworker("picker-paging")
    get_user_model().objects.bulk_create(
        [get_user_model()(username=f"colleague-{i}") for i in range(30)]
    )

    response = _client(user).get(PICKER_URL)

    assert response.status_code == 200
    # A flat list, not a {count, results} page.
    assert isinstance(response.data, list)
    assert len(response.data) >= 31
