"""BB-38 edge cases for the CaseQuerySet.update() slug-history hook.

Exhaustive corners of the bulk-update recording hook: chained re-slugs, slug
reclaim/recycle, slug swaps, multi-row + integrity failures, empty/None slugs,
expression-valued updates (``bulk_update()`` / ``F()`` fall through), the cheap
non-slug path, manager wiring, per-state recording, and — via a REAL HTTP
server (pytest-django ``live_server``) — 301 status, Location, query-string
preservation, single-hop chains, HEAD, and the non-public no-leak boundary.
"""

import pytest
import requests
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from cases.models import Case, CaseQuerySet, CaseSlugHistory, CaseState, CaseType
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"


def _make_case(slug, state=CaseState.PUBLISHED, title="A case") -> Case:
    return Case.objects.create(
        title=title,
        case_type=CaseType.CORRUPTION,
        state=state,
        slug=slug,
        description="x" * 5000,  # large text columns — .only() must skip these
        short_description="Short",
    )


def _cw_client() -> APIClient:
    user = create_user_with_role("cw", "cw@example.com", "Caseworker")
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _history_slugs(case):
    return set(case.slug_history.values_list("slug", flat=True))


# ===========================================================================
# Manager wiring
# ===========================================================================


@pytest.mark.django_db
def test_manager_returns_case_queryset():
    assert isinstance(Case.objects.all(), CaseQuerySet)
    assert isinstance(Case.objects.filter(pk__gt=0), CaseQuerySet)


# ===========================================================================
# Chained re-slugs
# ===========================================================================


@pytest.mark.django_db
def test_chained_reslug_all_predecessors_point_to_case():
    case = _make_case("slug-a", state=CaseState.PUBLISHED)
    Case.objects.filter(pk=case.pk).update(slug="slug-b")
    Case.objects.filter(pk=case.pk).update(slug="slug-c")

    case.refresh_from_db()
    assert case.slug == "slug-c"
    # Both retired slugs recorded, both pointing at the same (single) case.
    assert _history_slugs(case) == {"slug-a", "slug-b"}
    assert CaseSlugHistory.objects.filter(slug="slug-a", case=case).exists()
    assert CaseSlugHistory.objects.filter(slug="slug-b", case=case).exists()


@pytest.mark.django_db
def test_chained_reslug_oldest_url_single_hops_to_current():
    """A→B→C: GET A must 301 straight to C, not chain through B."""
    case = _make_case("chain-a", state=CaseState.PUBLISHED)
    Case.objects.filter(pk=case.pk).update(slug="chain-b")
    Case.objects.filter(pk=case.pk).update(slug="chain-c")

    resp = APIClient().get(URL.format("chain-a"))
    assert resp.status_code == 301
    assert resp["Location"].endswith("/api/cases/chain-c/")  # not chain-b

    resp_b = APIClient().get(URL.format("chain-b"))
    assert resp_b.status_code == 301
    assert resp_b["Location"].endswith("/api/cases/chain-c/")


# ===========================================================================
# Reclaim / recycle / swap
# ===========================================================================


@pytest.mark.django_db
def test_reclaim_own_old_slug_via_update_drops_shadow_and_reverses():
    case = _make_case("home-1", state=CaseState.PUBLISHED)
    Case.objects.filter(pk=case.pk).update(slug="home-2")
    assert _history_slugs(case) == {"home-1"}

    # Case moves back to its original slug — the shadow row for the now-live
    # slug must be purged, and the vacated one now redirects.
    Case.objects.filter(pk=case.pk).update(slug="home-1")
    case.refresh_from_db()
    assert case.slug == "home-1"
    assert not CaseSlugHistory.objects.filter(slug="home-1").exists()
    assert CaseSlugHistory.objects.filter(slug="home-2", case=case).exists()


@pytest.mark.django_db
def test_recycled_slug_live_owner_wins_over_history():
    """case1 vacates 's1'; case2 then claims 's1' via update — live wins."""
    case1 = _make_case("s1", state=CaseState.PUBLISHED, title="One")
    Case.objects.filter(pk=case1.pk).update(slug="s1-new")
    # history: s1 -> case1
    assert CaseSlugHistory.objects.filter(slug="s1", case=case1).exists()

    case2 = _make_case("s2", state=CaseState.PUBLISHED, title="Two")
    Case.objects.filter(pk=case2.pk).update(slug="s1")  # reclaim s1 for case2

    # The stale s1 -> case1 shadow is gone; s1 is live for case2.
    assert not CaseSlugHistory.objects.filter(slug="s1", case=case1).exists()
    resp = APIClient().get(URL.format("s1"))
    assert resp.status_code == 200
    assert resp.data["title"] == "Two"
    # case2's own vacated slug redirects to it (now at s1).
    resp2 = APIClient().get(URL.format("s2"))
    assert resp2.status_code == 301
    assert resp2["Location"].endswith("/api/cases/s1/")


@pytest.mark.django_db
def test_two_cases_swap_slugs_via_update_three_step():
    a = _make_case("alpha", state=CaseState.PUBLISHED, title="A")
    b = _make_case("beta", state=CaseState.PUBLISHED, title="B")

    # Swap alpha<->beta through a temporary to dodge the unique collision.
    Case.objects.filter(pk=a.pk).update(slug="alpha-tmp")
    Case.objects.filter(pk=b.pk).update(slug="alpha")
    Case.objects.filter(pk=a.pk).update(slug="beta")

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.slug == "beta" and b.slug == "alpha"
    # "alpha" is live for b now; the alpha->a shadow was dropped when b claimed it.
    resp_alpha = APIClient().get(URL.format("alpha"))
    assert resp_alpha.status_code == 200 and resp_alpha.data["title"] == "B"
    resp_beta = APIClient().get(URL.format("beta"))
    assert resp_beta.status_code == 200 and resp_beta.data["title"] == "A"
    # The throwaway temp slug redirects to a (now at beta).
    resp_tmp = APIClient().get(URL.format("alpha-tmp"))
    assert resp_tmp.status_code == 301
    assert resp_tmp["Location"].endswith("/api/cases/beta/")


# ===========================================================================
# Multi-row and integrity failures
# ===========================================================================


@pytest.mark.django_db
def test_bulk_update_two_rows_same_slug_raises_and_records_nothing():
    a = _make_case("m1", state=CaseState.PUBLISHED, title="M1")
    b = _make_case("m2", state=CaseState.PUBLISHED, title="M2")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Case.objects.filter(pk__in=[a.pk, b.pk]).update(slug="collide")
    assert not CaseSlugHistory.objects.exists()


@pytest.mark.django_db
def test_update_slug_to_none_raises_and_records_nothing():
    case = _make_case("notnull", state=CaseState.PUBLISHED)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Case.objects.filter(pk=case.pk).update(slug=None)
    assert not CaseSlugHistory.objects.exists()


@pytest.mark.django_db
def test_update_slug_to_empty_string_records_old_slug():
    """Documents behavior: an empty-string slug still retires the old one."""
    case = _make_case("was-full", state=CaseState.PUBLISHED)
    Case.objects.filter(pk=case.pk).update(slug="")
    case.refresh_from_db()
    assert case.slug == ""
    assert CaseSlugHistory.objects.filter(slug="was-full", case=case).exists()


# ===========================================================================
# Cheap path (no slug) and row counts
# ===========================================================================


@pytest.mark.django_db
def test_bulk_non_slug_update_over_many_rows_records_nothing():
    for i in range(5):
        _make_case(f"draft-{i}", state=CaseState.DRAFT, title=f"D{i}")
    updated = Case.objects.filter(state=CaseState.DRAFT).update(
        state=CaseState.PUBLISHED
    )
    assert updated == 5
    assert not CaseSlugHistory.objects.exists()


@pytest.mark.django_db
def test_update_returns_rowcount_like_base():
    case = _make_case("rc-old", state=CaseState.PUBLISHED)
    n = Case.objects.filter(pk=case.pk).update(slug="rc-new")
    assert n == 1
    # A filter matching nothing returns 0 and records nothing.
    z = Case.objects.filter(pk=-1).update(slug="rc-nope")
    assert z == 0
    assert not CaseSlugHistory.objects.filter(slug="rc-nope").exists()


@pytest.mark.django_db
def test_update_slug_to_same_value_records_nothing():
    case = _make_case("idem", state=CaseState.PUBLISHED)
    n = Case.objects.filter(pk=case.pk).update(slug="idem")
    assert n == 1  # row is "updated" (rowcount) even though slug is unchanged
    assert not CaseSlugHistory.objects.exists()


# ===========================================================================
# State independence: record on update regardless of state; gate at retrieve
# ===========================================================================


@pytest.mark.django_db
def test_update_records_for_every_state():
    for state in (CaseState.DRAFT, CaseState.IN_REVIEW, CaseState.PUBLISHED, CaseState.CLOSED):
        c = _make_case(f"st-{state}-old", state=state, title=f"T-{state}")
        Case.objects.filter(pk=c.pk).update(slug=f"st-{state}-new")
        assert CaseSlugHistory.objects.filter(slug=f"st-{state}-old", case=c).exists()


@pytest.mark.django_db
def test_closed_case_records_history_but_retrieve_404s():
    case = _make_case("closed-old2", state=CaseState.CLOSED, title="Closed")
    Case.objects.filter(pk=case.pk).update(slug="closed-new2")
    assert CaseSlugHistory.objects.filter(slug="closed-old2", case=case).exists()
    # CLOSED is never exposed, even to a caseworker — no redirect leak.
    assert _cw_client().get(URL.format("closed-old2")).status_code == 404


# ===========================================================================
# Regression: the save() path still records (untouched by this change)
# ===========================================================================


@pytest.mark.django_db
def test_save_path_draft_reslug_still_records():
    case = _make_case("save-a", state=CaseState.DRAFT)
    case.slug = "save-b"
    case.save()
    assert CaseSlugHistory.objects.filter(slug="save-a", case=case).exists()


# ===========================================================================
# Expression-valued slug updates fall through (no Python value to record).
# bulk_update() builds a Case/When expression and routes through update();
# update(slug=F(...)) is the direct form. Both must be clean no-ops for history
# (the string ``isinstance`` guard), NOT records of an expression object.
# ===========================================================================


@pytest.mark.django_db
def test_bulk_update_method_does_not_record_expression():
    case = _make_case("bu-old", state=CaseState.PUBLISHED)
    case.slug = "bu-new"
    Case.objects.bulk_update([case], ["slug"])
    case.refresh_from_db()
    assert case.slug == "bu-new"
    # No history row is created, and crucially none carries a mangled/expression
    # slug value — bulk_update passes a Case() expression, which we skip.
    assert CaseSlugHistory.objects.count() == 0


@pytest.mark.django_db
def test_update_slug_with_f_expression_falls_through():
    from django.db.models import F

    case = _make_case("fx", state=CaseState.PUBLISHED, title="fx title")
    # Nonsensical for slug, but proves an F() value is not recorded.
    Case.objects.filter(pk=case.pk).update(slug=F("slug"))
    assert CaseSlugHistory.objects.count() == 0


# ===========================================================================
# REAL HTTP SERVER (pytest-django live_server) — full WSGI + middleware stack
# ===========================================================================


@pytest.mark.django_db(transaction=True)
def test_live_server_retired_slug_301_with_location(live_server):
    case = _make_case("live-old", state=CaseState.PUBLISHED)
    Case.objects.filter(pk=case.pk).update(slug="live-new")

    r = requests.get(f"{live_server.url}/api/cases/live-old/", allow_redirects=False)
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/api/cases/live-new/")


@pytest.mark.django_db(transaction=True)
def test_live_server_preserves_query_string(live_server):
    case = _make_case("live-q-old", state=CaseState.PUBLISHED)
    Case.objects.filter(pk=case.pk).update(slug="live-q-new")

    r = requests.get(
        f"{live_server.url}/api/cases/live-q-old/?utm=news&ref=x",
        allow_redirects=False,
    )
    assert r.status_code == 301
    loc = r.headers["Location"]
    assert "/api/cases/live-q-new/?" in loc
    assert "utm=news" in loc and "ref=x" in loc


@pytest.mark.django_db(transaction=True)
def test_live_server_follows_to_200(live_server):
    case = _make_case("live-f-old", state=CaseState.PUBLISHED, title="Followed")
    Case.objects.filter(pk=case.pk).update(slug="live-f-new")

    r = requests.get(f"{live_server.url}/api/cases/live-f-old/")  # follow redirects
    assert r.status_code == 200
    assert r.json()["slug"] == "live-f-new"
    assert len(r.history) == 1 and r.history[0].status_code == 301


@pytest.mark.django_db(transaction=True)
def test_live_server_chain_is_single_hop(live_server):
    case = _make_case("lc-a", state=CaseState.PUBLISHED)
    Case.objects.filter(pk=case.pk).update(slug="lc-b")
    Case.objects.filter(pk=case.pk).update(slug="lc-c")

    r = requests.get(f"{live_server.url}/api/cases/lc-a/")
    assert r.status_code == 200
    # Exactly one 301 hop straight to the current slug.
    assert len(r.history) == 1
    assert r.history[0].headers["Location"].endswith("/api/cases/lc-c/")


@pytest.mark.django_db(transaction=True)
def test_live_server_head_request_redirects(live_server):
    case = _make_case("live-h-old", state=CaseState.PUBLISHED)
    Case.objects.filter(pk=case.pk).update(slug="live-h-new")

    r = requests.head(f"{live_server.url}/api/cases/live-h-old/", allow_redirects=False)
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/api/cases/live-h-new/")


@pytest.mark.django_db(transaction=True)
def test_live_server_unknown_slug_404(live_server):
    r = requests.get(
        f"{live_server.url}/api/cases/nothing-here-xyz/", allow_redirects=False
    )
    assert r.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_live_server_draft_retired_slug_no_leak_to_anon(live_server):
    case = _make_case("live-d-old", state=CaseState.DRAFT, title="Secret")
    Case.objects.filter(pk=case.pk).update(slug="live-d-new")

    r = requests.get(f"{live_server.url}/api/cases/live-d-old/", allow_redirects=False)
    # Anonymous over real HTTP: a plain 404, never a 301 confirming the draft.
    assert r.status_code == 404
