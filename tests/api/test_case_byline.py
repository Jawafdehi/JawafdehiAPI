"""The structured public byline: authors, publish date, and edit history.

Replaces the free-text ``Case.public_notes`` attribution. Covers the publish
gate, the author join's replace semantics and ordering, the auto-created
AuthorProfile (slug, per-person description), the casework-only ``user_id``
boundary, the author-picker endpoint, and the public profile page.
"""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from rest_framework.test import APIClient

from cases.models import (
    AuthorProfile,
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
    assert not case.author_ids and case.case_publish_date is None

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
# The CaseAuthor join + AuthorProfile
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_crediting_someone_creates_their_profile_and_slug():
    """"Every case author has a slug" holds by construction, not by backfill."""
    user = get_user_model().objects.create_user(
        username="rujit", first_name="Rujit", last_name="Kafle"
    )
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=user)

    profile = AuthorProfile.objects.get(user=user)
    assert profile.slug == "rujit-kafle"
    assert profile.display_name == "Rujit Kafle"
    # Empty until someone fills it in, so it is not published yet.
    assert profile.has_public_page is False


@pytest.mark.django_db
def test_profile_slug_falls_back_to_username_without_a_full_name():
    user = get_user_model().objects.create_user(username="mononym")
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=user)

    assert AuthorProfile.objects.get(user=user).slug == "mononym"


@pytest.mark.django_db
def test_profile_slugs_do_not_collide_for_identical_names():
    """Suffixed -2, not a random tail: a slug is a person's public handle."""
    case = _make_case()
    for username in ("a", "b"):
        user = get_user_model().objects.create_user(
            username=username, first_name="Ram", last_name="Gautam"
        )
        CaseAuthor.objects.create(case=_make_case() if username == "b" else case, user=user)

    assert sorted(AuthorProfile.objects.values_list("slug", flat=True)) == [
        "ram-gautam",
        "ram-gautam-2",
    ]


@pytest.mark.django_db
def test_a_slug_is_generated_even_for_a_name_with_no_latin_letters():
    """validate_slug requires a leading letter; a Devanagari name has none."""
    user = get_user_model().objects.create_user(
        username="\u0938\u0941\u092c\u094b\u0927", first_name="\u0938\u0941\u092c\u094b\u0927", last_name="\u0915\u0901\u0921\u0947\u0932"
    )
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=user)

    profile = AuthorProfile.objects.get(user=user)
    assert profile.slug
    assert profile.slug[0].isalpha()


@pytest.mark.django_db
def test_renaming_an_account_does_not_change_the_profile_name():
    """The profile name is the canonical one; the account name only seeds it."""
    user = get_user_model().objects.create_user(
        username="sambhav", first_name="Sambhav", last_name="Koirala"
    )
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=user)

    user.last_name = "Koirala-Sharma"
    user.save()

    assert AuthorProfile.objects.get(user=user).display_name == "Sambhav Koirala"


@pytest.mark.django_db
def test_a_title_is_per_person_so_it_shows_on_every_case_they_wrote():
    """The point of moving it off the join: one fact, stored once."""
    user = get_user_model().objects.create_user(
        username="sambhav2", first_name="Sambhav", last_name="Koirala"
    )
    first = _make_case(state=CaseState.PUBLISHED)
    second = _make_case(state=CaseState.PUBLISHED)
    CaseAuthor.objects.create(case=first, user=user)
    CaseAuthor.objects.create(case=second, user=user)

    profile = AuthorProfile.objects.get(user=user)
    profile.title = "BALLB 4th Year Student"
    profile.save()

    for case in (first, second):
        response = APIClient().get(URL.format(case.slug))
        assert response.data["authors"][0]["title"] == "BALLB 4th Year Student"


@pytest.mark.django_db
def test_nepali_name_falls_back_to_english_when_unset():
    user = get_user_model().objects.create_user(
        username="niroj", first_name="Niroj", last_name="Aryal"
    )
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=user)
    profile = AuthorProfile.objects.get(user=user)

    assert profile.name_for_language("ne") == "Niroj Aryal"
    profile.name_ne = "\u0928\u093f\u0930\u094b\u091c \u0905\u0930\u094d\u092f\u093e\u0932"
    profile.save()
    assert profile.name_for_language("ne") == "\u0928\u093f\u0930\u094b\u091c \u0905\u0930\u094d\u092f\u093e\u0932"
    assert profile.name_for_language("en") == "Niroj Aryal"


@pytest.mark.django_db
def test_deleting_a_credited_account_is_refused():
    """PROTECT, not SET_NULL: losing the row would silently strip a byline."""
    user = get_user_model().objects.create_user(username="subodh")
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=user)

    with pytest.raises(ProtectedError):
        user.delete()


@pytest.mark.django_db
def test_author_ids_order_by_ordinal_not_insertion():
    first = get_user_model().objects.create_user(username="second-added")
    second = get_user_model().objects.create_user(username="first-added")
    case = _make_case()
    CaseAuthor.objects.create(case=case, user=first, ordinal=1)
    CaseAuthor.objects.create(case=case, user=second, ordinal=0)

    case.refresh_from_db()
    assert case.author_ids == [second.id, first.id]


@pytest.mark.django_db
def test_assigning_authors_deduplicates_on_user():
    """A double-click in the picker must not 500 on the unique constraint."""
    user = get_user_model().objects.create_user(username="dupe")
    case = _make_case()
    case.author_ids = [user.id, user.id]
    case.save()

    case.refresh_from_db()
    assert case.author_ids == [user.id]


@pytest.mark.django_db
def test_author_ids_on_an_unsaved_case_is_empty_rather_than_raising():
    """``validate()`` reads the property; an unsaved instance has no reverse rows."""
    assert Case(title="Unsaved").author_ids == []


@pytest.mark.django_db
def test_the_m2m_reverse_accessor_lists_the_cases_someone_wrote():
    """``user.authored_cases`` is what the public author page queries."""
    user = get_user_model().objects.create_user(username="prolific")
    first, second = _make_case(), _make_case()
    CaseAuthor.objects.create(case=first, user=user)
    CaseAuthor.objects.create(case=second, user=user)

    assert set(user.authored_cases.values_list("pk", flat=True)) == {
        first.pk,
        second.pk,
    }


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
            {"op": "replace", "path": "/authors", "value": [user.id]},
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
    assert case.author_ids == [user.id]
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
    before = case.author_ids

    response = _patch(
        _client(user), case, [{"op": "replace", "path": "/title", "value": "Retitled"}]
    )

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.author_ids == before


@pytest.mark.django_db
def test_patch_with_an_unknown_user_id_is_a_422_not_a_500():
    user = _caseworker("bad-id")
    case = _make_case()
    response = _patch(
        _client(user),
        case,
        [{"op": "replace", "path": "/authors", "value": [9_999_999]}],
    )

    assert response.status_code == 422, response.data
    assert "authors" in response.data


@pytest.mark.django_db
def test_patch_preserves_the_order_the_ids_were_sent_in():
    """List order IS byline order — the only per-case fact about an author."""
    user = _caseworker("order-setter")
    other = get_user_model().objects.create_user(username="second-author")
    case = _make_case()

    response = _patch(
        _client(user),
        case,
        [{"op": "replace", "path": "/authors", "value": [other.id, user.id]}],
    )

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.author_ids == [other.id, user.id]


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
    credit_author(case, title="BALLB 4th Year Student")

    response = APIClient().get(URL.format(case.slug))

    assert response.status_code == 200
    author = response.data["authors"][0]
    assert author["display_name"] == "Byline Author"
    assert author["title"] == "BALLB 4th Year Student"
    assert author["slug"] == "byline-author"
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


# ---------------------------------------------------------------------------
# The public author profile page
# ---------------------------------------------------------------------------


def _published_profile(username="published-author", **fields):
    """A credited author whose profile has been filled in and published."""
    user = get_user_model().objects.create_user(
        username=username, first_name="Subodh", last_name="Kandel"
    )
    case = _make_case(state=CaseState.PUBLISHED)
    CaseAuthor.objects.create(case=case, user=user)
    profile = AuthorProfile.objects.get(user=user)
    profile.has_public_page = True
    for key, value in fields.items():
        setattr(profile, key, value)
    profile.save()
    return user, profile, case


@pytest.mark.django_db
def test_author_page_is_public_and_returns_the_profile():
    _user, profile, _case = _published_profile(
        title="Caseworker",
        bio="Documents CIAA procurement cases. **Law student** at TU.",
        photo_url="https://s3.jawafdehi.org/team/subodh.jpeg",
        links=[{"type": "instagram", "value": "https://instagram.com/subodh"}],
    )

    response = APIClient().get(f"/api/authors/{profile.slug}/")

    assert response.status_code == 200
    assert response.data["display_name"] == "Subodh Kandel"
    assert response.data["title"] == "Caseworker"
    assert response.data["bio"].startswith("Documents CIAA procurement cases.")
    assert response.data["photo_url"] == "https://s3.jawafdehi.org/team/subodh.jpeg"
    assert response.data["links"][0]["type"] == "instagram"


@pytest.mark.django_db
def test_author_page_returns_a_null_email_when_none_is_set():
    """Opt-in: the key is always present, and null means "no address published".

    Null rather than an empty string so the frontend cannot confuse "not set"
    with "not loaded"; the key is present so the shape is stable. Contrast
    ``bio``, which the byline payload omits entirely — see
    ``test_the_byline_card_payload_carries_the_title_but_not_the_bio``.
    """
    _user, profile, _case = _published_profile()

    response = APIClient().get(f"/api/authors/{profile.slug}/")

    assert "email" in response.data
    assert response.data["email"] is None


@pytest.mark.django_db
def test_author_page_shows_an_email_that_was_set():
    _user, profile, _case = _published_profile(email="kandel@example.org")

    response = APIClient().get(f"/api/authors/{profile.slug}/")

    assert response.data["email"] == "kandel@example.org"


@pytest.mark.django_db
def test_unpublished_profile_404s():
    """A profile is auto-created empty on first credit; that is not a page."""
    user = get_user_model().objects.create_user(username="not-published")
    CaseAuthor.objects.create(case=_make_case(), user=user)
    profile = AuthorProfile.objects.get(user=user)
    assert profile.has_public_page is False

    assert APIClient().get(f"/api/authors/{profile.slug}/").status_code == 404


@pytest.mark.django_db
def test_author_page_lists_cases_newest_published_first():
    user, profile, first = _published_profile()
    Case.objects.filter(pk=first.pk).update(case_publish_date=date(2025, 7, 1))

    middle = _make_case(state=CaseState.PUBLISHED, case_publish_date=date(2026, 8, 14))
    newest = _make_case(state=CaseState.PUBLISHED, case_publish_date=date(2026, 8, 20))
    for case in (middle, newest):
        CaseAuthor.objects.create(case=case, user=user)

    response = APIClient().get(f"/api/authors/{profile.slug}/")

    assert [c["slug"] for c in response.data["cases"]] == [
        newest.slug,
        middle.slug,
        first.slug,
    ]


@pytest.mark.django_db
def test_author_page_sorts_undated_cases_last_not_first():
    """A NULL publish date must not float to the top of the list."""
    user, profile, undated = _published_profile()
    Case.objects.filter(pk=undated.pk).update(case_publish_date=None)
    dated = _make_case(state=CaseState.PUBLISHED, case_publish_date=date(2026, 8, 20))
    CaseAuthor.objects.create(case=dated, user=user)

    response = APIClient().get(f"/api/authors/{profile.slug}/")

    assert [c["slug"] for c in response.data["cases"]] == [dated.slug, undated.slug]


@pytest.mark.django_db
def test_author_page_never_lists_a_draft():
    """An author page must not become the one place a draft's existence leaks."""
    user, profile, published = _published_profile()
    draft = _make_case(state=CaseState.DRAFT, title="Secret draft")
    CaseAuthor.objects.create(case=draft, user=user)

    response = APIClient().get(f"/api/authors/{profile.slug}/")

    slugs = [c["slug"] for c in response.data["cases"]]
    assert slugs == [published.slug]
    assert "Secret draft" not in str(response.data)


@pytest.mark.django_db
def test_case_byline_reports_whether_the_author_has_a_public_page():
    """The card links only when there is a page to link to."""
    _user, _profile, case = _published_profile()
    response = APIClient().get(URL.format(case.slug))

    assert response.data["authors"][0]["has_public_page"] is True
    assert response.data["authors"][0]["slug"]


@pytest.mark.django_db
def test_author_page_returns_an_empty_bio_when_none_is_written():
    """The About section is simply not rendered; it is not an error."""
    _user, profile, _case = _published_profile()

    response = APIClient().get(f"/api/authors/{profile.slug}/")

    assert response.data["bio"] == ""


@pytest.mark.django_db
def test_the_byline_card_payload_carries_the_title_but_not_the_bio():
    """A paragraph per author on every case read would be dead weight."""
    _user, _profile, case = _published_profile(title="Caseworker", bio="A long bio.")

    response = APIClient().get(URL.format(case.slug))

    author = response.data["authors"][0]
    assert author["title"] == "Caseworker"
    assert "bio" not in author


# ---------------------------------------------------------------------------
# Review hardening (PR #456)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_rejects_a_float_author_id_instead_of_truncating_it():
    """`child` is a JSONField, so 3.7 would otherwise int() down to account 3."""
    user = _caseworker("float-id")
    case = _make_case()

    response = _patch(
        _client(user), case, [{"op": "replace", "path": "/authors", "value": [3.7]}]
    )

    assert response.status_code == 422, response.data
    assert "authors" in response.data


@pytest.mark.django_db
def test_patch_rejects_a_boolean_author_id():
    """`bool` is an int subclass, so True would otherwise credit account 1."""
    user = _caseworker("bool-id")
    case = _make_case()

    response = _patch(
        _client(user), case, [{"op": "replace", "path": "/authors", "value": [True]}]
    )

    assert response.status_code == 422, response.data


@pytest.mark.django_db
def test_patch_rejects_an_out_of_range_author_id():
    """Without a bound this reaches the pk__in query and 500s on PostgreSQL."""
    user = _caseworker("huge-id")
    case = _make_case()

    response = _patch(
        _client(user), case, [{"op": "replace", "path": "/authors", "value": [10**30]}]
    )

    assert response.status_code == 422, response.data


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    [
        "2026-08-14T10:30:00",  # a full timestamp
        "20260814",  # ISO basic format
        "2026-W33-5",  # an ISO week date
    ],
)
def test_patch_rejects_edit_history_dates_that_are_not_plain_iso_dates(value):
    """None of these is a plain YYYY-MM-DD date, and all would be rendered
    verbatim on the public case page.

    Worth being precise about why the shape check exists: on 3.11+ BOTH parsers
    accept the basic form ("20260814") and the week form ("2026-W33-5").
    ``date.fromisoformat`` only narrows ``datetime.fromisoformat`` by rejecting
    timestamps and offsets — so swapping parsers is not on its own enough, and
    ``parse_edit_history_date`` shape-checks with a regex before parsing."""
    user = _caseworker(f"date-{abs(hash(value)) % 10000}")
    case = _make_case()

    response = _patch(
        _client(user),
        case,
        [
            {
                "op": "replace",
                "path": "/public_edit_history",
                "value": [{"date": value, "remarks": "Something"}],
            }
        ],
    )

    assert response.status_code == 422, response.data


@pytest.mark.django_db
def test_the_model_field_rejects_the_same_loose_dates_as_the_serializer():
    """One rule, both layers — a raw ORM write must not slip a timestamp in."""
    case = _make_case()
    case.public_edit_history = [{"date": "2026-08-14T10:30:00", "remarks": "x"}]

    with pytest.raises(ValidationError):
        case.full_clean()


@pytest.mark.django_db
def test_ensure_for_is_idempotent_under_a_repeated_call():
    """The check-then-create window is retried, not raised, on a lost race."""
    user = get_user_model().objects.create_user(username="racer")

    first = AuthorProfile.ensure_for(user)
    second = AuthorProfile.ensure_for(user)

    assert first.pk == second.pk
    assert AuthorProfile.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_picker_orders_by_the_name_the_byline_will_show():
    """Sorting the raw account fields would disagree with the displayed name."""
    staff = _caseworker("picker-order")
    # Account name sorts last ("Zed"), profile name sorts first ("Aaron").
    renamed = get_user_model().objects.create_user(
        username="zzz", first_name="Zed", last_name="Zimmer"
    )
    CaseAuthor.objects.create(case=_make_case(), user=renamed)
    profile = AuthorProfile.objects.get(user=renamed)
    profile.name_en = "Aaron Adhikari"
    profile.save()

    rows = _client(staff).get(PICKER_URL).data
    names = [row["display_name"] for row in rows]

    assert names == sorted(names)
    assert names[0] == "Aaron Adhikari"


@pytest.mark.django_db
def test_the_author_inline_is_view_only_in_the_django_admin():
    """It does not inherit CaseAdmin's read-only gate — it falls back to
    model-level caseauthor perms, so the byline would be editable there."""
    from django.contrib.admin.sites import AdminSite

    from cases.admin import CaseAuthorInline

    inline = CaseAuthorInline(Case, AdminSite())
    assert inline.has_add_permission(None, None) is False
    assert inline.has_change_permission(None, None) is False
    assert inline.has_delete_permission(None, None) is False


@pytest.mark.django_db
def test_patch_rejects_an_impossible_edit_history_date():
    """The shape check alone would pass 2026-02-31; the parser catches it."""
    user = _caseworker("impossible-date")
    case = _make_case()

    response = _patch(
        _client(user),
        case,
        [
            {
                "op": "replace",
                "path": "/public_edit_history",
                "value": [{"date": "2026-02-31", "remarks": "Something"}],
            }
        ],
    )

    assert response.status_code == 422, response.data


@pytest.mark.django_db
def test_both_layers_report_the_same_message_for_a_bad_edit_history_date():
    """One rule, one error format — API and direct ORM write must agree."""
    from django.core.exceptions import ValidationError as DjangoValidationError

    from cases.caseworker_serializers import EditHistoryItemSerializer

    bad = "2026-W33-5"

    serializer = EditHistoryItemSerializer(data={"date": bad, "remarks": "x"})
    assert not serializer.is_valid()
    api_message = str(serializer.errors["date"][0])

    case = _make_case()
    case.public_edit_history = [{"date": bad, "remarks": "x"}]
    with pytest.raises(DjangoValidationError) as exc:
        case.full_clean()
    model_message = exc.value.message_dict["public_edit_history"][0]

    assert api_message == model_message
