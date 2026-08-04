"""Wave-2 admin-UX backend support for the moderation/casework surface:

  1. ``?page_size=`` on the case list (CasePagination) — the moderation queue
     and dashboard need to size/count more than the default 20 rows.
  2. Optimistic concurrency on PATCH via ``If-Match`` / ETag — a stale editor
     gets 412 instead of silently clobbering a concurrent edit.
  3. The append-only ``CaseStateChange`` history (actor + reason from the
     ``X-Transition-Reason`` header) and ``GET /api/cases/{slug}/history/``.

All three are additive and backward compatible: no ``page_size`` → default 20;
no ``If-Match`` → last-write-wins as before; the history endpoint is new.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseStateChange,
    CaseType,
    RelationshipType,
)
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"
LIST_URL = "/api/cases/"


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _publishable_case(state=CaseState.DRAFT, **kwargs) -> Case:
    defaults = dict(
        title="Publishable case",
        case_type=CaseType.CORRUPTION,
        state=state,
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


def _patch_state(client, case, target, **headers):
    return client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/state", "value": target}],
        format="json",
        **headers,
    )


# ---------------------------------------------------------------------------
# 1. page_size pagination
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_page_size_query_param_honoured():
    user = create_user_with_role("mod-ps", "mod-ps@example.com", "Moderator")
    for i in range(25):
        Case.objects.create(
            title=f"Case {i}",
            case_type=CaseType.CORRUPTION,
            state=CaseState.IN_REVIEW,
        )

    resp = _authed_client(user).get(LIST_URL, {"state": "IN_REVIEW", "page_size": 100})

    assert resp.status_code == 200
    # count is preserved (page-number pagination, not cursor) and all 25 fit.
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 25


@pytest.mark.django_db
def test_default_page_size_unchanged_at_20():
    user = create_user_with_role("mod-ps2", "mod-ps2@example.com", "Moderator")
    for i in range(25):
        Case.objects.create(
            title=f"Case {i}",
            case_type=CaseType.CORRUPTION,
            state=CaseState.IN_REVIEW,
        )

    resp = _authed_client(user).get(LIST_URL, {"state": "IN_REVIEW"})

    assert resp.status_code == 200
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20  # default page unchanged


@pytest.mark.django_db
def test_page_size_capped_at_max():
    user = create_user_with_role("mod-ps3", "mod-ps3@example.com", "Moderator")
    for i in range(5):
        Case.objects.create(
            title=f"Case {i}",
            case_type=CaseType.CORRUPTION,
            state=CaseState.IN_REVIEW,
        )
    # Asking for more than max_page_size (200) must not error; it clamps.
    resp = _authed_client(user).get(LIST_URL, {"state": "IN_REVIEW", "page_size": 9999})
    assert resp.status_code == 200
    assert len(resp.data["results"]) == 5


# ---------------------------------------------------------------------------
# 2. optimistic concurrency (If-Match / ETag)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_retrieve_returns_etag():
    user = create_user_with_role("mod-et", "mod-et@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)

    resp = _authed_client(user).get(URL.format(case.slug))

    assert resp.status_code == 200
    assert resp.headers.get("ETag")


@pytest.mark.django_db
def test_patch_without_if_match_still_succeeds():
    """Backward compatible: no precondition header → last-write-wins as before."""
    user = create_user_with_role("mod-nm", "mod-nm@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)

    resp = _authed_client(user).patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "New title"}],
        format="json",
    )

    assert resp.status_code == 200
    case.refresh_from_db()
    assert case.title == "New title"


@pytest.mark.django_db
def test_patch_with_matching_if_match_succeeds():
    user = create_user_with_role("mod-mm", "mod-mm@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)
    client = _authed_client(user)

    token = client.get(URL.format(case.slug)).headers["ETag"]
    resp = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Retitled"}],
        format="json",
        HTTP_IF_MATCH=token,
    )

    assert resp.status_code == 200
    # A fresh token is returned so the editor can keep editing in place.
    assert resp.headers.get("ETag")
    assert resp.headers["ETag"] != token  # updated_at moved → token changed


@pytest.mark.django_db
def test_patch_with_stale_if_match_rejected_412():
    user = create_user_with_role("mod-st", "mod-st@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)
    client = _authed_client(user)

    stale = client.get(URL.format(case.slug)).headers["ETag"]
    # Someone else edits the case, moving updated_at (and the token).
    client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Edited elsewhere"}],
        format="json",
    )

    resp = client.patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "My conflicting edit"}],
        format="json",
        HTTP_IF_MATCH=stale,
    )

    assert resp.status_code == 412
    assert resp.headers.get("ETag")  # carries the current token for reconcile
    case.refresh_from_db()
    assert case.title == "Edited elsewhere"  # my stale write did NOT land


@pytest.mark.django_db
def test_if_match_star_matches_any_existing():
    user = create_user_with_role("mod-str", "mod-str@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)

    resp = _authed_client(user).patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Wildcard"}],
        format="json",
        HTTP_IF_MATCH="*",
    )

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. state-change history + reason
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_transition_records_history_row_with_actor():
    user = create_user_with_role("mod-h1", "mod-h1@example.com", "Moderator")
    case = _publishable_case(state=CaseState.IN_REVIEW)

    resp = _patch_state(_authed_client(user), case, CaseState.PUBLISHED)

    assert resp.status_code == 200
    changes = list(CaseStateChange.objects.filter(case=case))
    assert len(changes) == 1
    assert changes[0].from_state == CaseState.IN_REVIEW
    assert changes[0].to_state == CaseState.PUBLISHED
    assert changes[0].actor == user
    assert changes[0].reason == ""


@pytest.mark.django_db
def test_transition_captures_reason_header():
    user = create_user_with_role("mod-h2", "mod-h2@example.com", "Moderator")
    case = _publishable_case(state=CaseState.IN_REVIEW)

    resp = _patch_state(
        _authed_client(user),
        case,
        CaseState.DRAFT,
        HTTP_X_TRANSITION_REASON="Needs a second source for the CIAA claim.",
    )

    assert resp.status_code == 200
    change = CaseStateChange.objects.get(case=case)
    assert change.to_state == CaseState.DRAFT
    assert change.reason == "Needs a second source for the CIAA claim."


@pytest.mark.django_db
def test_no_history_row_when_state_unchanged():
    """A scalar-only PATCH (no /state op) writes no history row."""
    user = create_user_with_role("mod-h3", "mod-h3@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)

    _authed_client(user).patch(
        URL.format(case.slug),
        data=[{"op": "replace", "path": "/title", "value": "Just a title edit"}],
        format="json",
    )

    assert CaseStateChange.objects.filter(case=case).count() == 0


@pytest.mark.django_db
def test_history_endpoint_returns_changes_newest_first():
    user = create_user_with_role("mod-h4", "mod-h4@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)
    client = _authed_client(user)

    _patch_state(client, case, CaseState.IN_REVIEW)
    _patch_state(client, case, CaseState.PUBLISHED)

    resp = client.get(URL.format(case.slug) + "history/")

    assert resp.status_code == 200
    rows = resp.data["results"] if "results" in resp.data else resp.data
    assert len(rows) == 2
    # Newest first (Meta.ordering).
    assert rows[0]["to_state"] == CaseState.PUBLISHED
    assert rows[1]["to_state"] == CaseState.IN_REVIEW
    assert rows[0]["actor_name"]  # actor label present


@pytest.mark.django_db
def test_history_endpoint_hidden_for_public_on_draft():
    """A draft's history must not leak to an unauthenticated caller (404)."""
    case = _publishable_case(state=CaseState.DRAFT)

    resp = APIClient().get(URL.format(case.slug) + "history/")

    assert resp.status_code == 404


@pytest.mark.django_db
def test_history_endpoint_hidden_for_public_on_published():
    """History carries internal data (moderator names + return reasons), so it
    must be gated even for a PUBLISHED case — unlike retrieve(), which exposes a
    published case to the public. An anonymous caller gets 404."""
    user = create_user_with_role("mod-hp", "mod-hp@example.com", "Moderator")
    case = _publishable_case(state=CaseState.IN_REVIEW)
    _patch_state(_authed_client(user), case, CaseState.PUBLISHED)

    resp = APIClient().get(URL.format(case.slug) + "history/")

    assert resp.status_code == 404


@pytest.mark.django_db
def test_history_endpoint_visible_to_caseworker():
    """A Caseworker can read any case's history — v3 retires object-level
    assignment, so the content-staff role sees the whole feedback loop."""
    author = create_user_with_role("cw-author", "cw-author@example.com", "Caseworker")
    mod = create_user_with_role("mod-fb", "mod-fb@example.com", "Moderator")
    case = _publishable_case(state=CaseState.IN_REVIEW)
    # Moderator sends it back with a reason.
    _patch_state(
        _authed_client(mod),
        case,
        CaseState.DRAFT,
        HTTP_X_TRANSITION_REASON="Please add a second source.",
    )

    resp = _authed_client(author).get(URL.format(case.slug) + "history/")

    assert resp.status_code == 200
    rows = resp.data["results"] if "results" in resp.data else resp.data
    assert any(r["reason"] == "Please add a second source." for r in rows)


@pytest.mark.django_db
def test_relation_only_patch_bumps_etag():
    """A PATCH touching ONLY relations (no scalar, no state) must still move the
    optimistic-concurrency token, else a concurrent relation edit clobbers
    unseen."""
    user = create_user_with_role("mod-rel", "mod-rel@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)
    client = _authed_client(user)

    before = client.get(URL.format(case.slug)).headers["ETag"]
    resp = client.patch(
        URL.format(case.slug),
        data=[
            {
                "op": "replace",
                "path": "/entities",
                "value": [
                    {
                        "nes_id": "https://jawafdehi.org/entity/person/some-other-person",
                        "relationship_type": "RELATED",
                    }
                ],
            }
        ],
        format="json",
    )

    assert resp.status_code == 200
    assert resp.headers["ETag"] != before  # token moved on a relation-only edit


# ---------------------------------------------------------------------------
# 4. gate precedence: an unparseable body must not pre-empt 403 / 412
# ---------------------------------------------------------------------------
#
# DRF parses the request body lazily and raises ParseError (-> 400) on the first
# access. So WHERE ``request.data`` is first touched inside partial_update is
# load-bearing: touching it before the permission / If-Match gates converts an
# unauthorized or stale-token request into a 400. That both leaks "your body was
# unparseable" to a caller entitled only to 403/412, and drops the 412's ETag —
# the header the client needs to reconcile.
#
# This regressed once during a refactor that hoisted ``patch_ops = request.data``
# above the gates, and nothing failed: the suite had no test that sent a
# malformed body to an unauthorized caller. These two are that test.


@pytest.mark.django_db
def test_unparseable_body_still_403_for_unauthorized_caller():
    """403 outranks the body parse: no probing a case you cannot write."""
    outsider = create_user_with_role("gp-403", "gp-403@example.com", "ReadOnly")
    case = _publishable_case(state=CaseState.DRAFT)

    resp = _authed_client(outsider).patch(
        URL.format(case.slug),
        data="{not json",
        content_type="application/json",
    )

    assert resp.status_code == 403


@pytest.mark.django_db
def test_unparseable_body_still_412_with_stale_if_match():
    """412 (and its ETag) outrank the body parse, so the client can reconcile."""
    user = create_user_with_role("gp-412", "gp-412@example.com", "Moderator")
    case = _publishable_case(state=CaseState.DRAFT)
    client = _authed_client(user)

    resp = client.patch(
        URL.format(case.slug),
        data="{not json",
        content_type="application/json",
        HTTP_IF_MATCH='"definitely-stale"',
    )

    assert resp.status_code == 412
    assert resp.headers.get("ETag")
