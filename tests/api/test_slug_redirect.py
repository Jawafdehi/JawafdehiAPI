"""BB-38: retired case slugs 301-redirect to the case's canonical URL.

Changing a case's slug (DRAFT re-slug, operational re-slug) used to orphan the
old URL as a hard 404. These tests cover (a) that a slug change is recorded in
CaseSlugHistory, (b) that a retired slug 301-redirects to the canonical URL,
(c) that a genuinely unknown slug stays a 404, (d) that a reused slug resolves
to its live owner (not a redirect), (e) that a retired slug never leaks a
non-public case's existence, (f) that ANY bulk ``update(slug=…)`` — the path a
PUBLISHED case is re-slugged through, which bypasses ``save()`` — records
history, and (g) that ``backfill_slug_history`` reconstructs retired slugs from
CaseReview snapshots.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from cases.models import Case, CaseSlugHistory, CaseState, CaseType
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"


def _make_case(slug, state=CaseState.PUBLISHED, title="A case") -> Case:
    return Case.objects.create(
        title=title,
        case_type=CaseType.CORRUPTION,
        state=state,
        slug=slug,
        description="Some description",
        short_description="Short",
    )


def _caseworker_client() -> APIClient:
    user = create_user_with_role("cw", "cw@example.com", "Caseworker")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# (a) A slug change records the OLD slug in CaseSlugHistory.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_draft_slug_patch_records_old_slug():
    """The API's DRAFT re-slug PATCH (bulk UPDATE path) records history."""
    case = _make_case("old-slug", state=CaseState.DRAFT)
    resp = _caseworker_client().patch(
        URL.format("old-slug"),
        data=[{"op": "replace", "path": "/slug", "value": "new-slug"}],
        format="json",
    )
    assert resp.status_code == 200
    case.refresh_from_db()
    assert case.slug == "new-slug"
    assert CaseSlugHistory.objects.filter(slug="old-slug", case=case).exists()
    # The current (live) slug is never recorded as its own predecessor.
    assert not CaseSlugHistory.objects.filter(slug="new-slug").exists()


@pytest.mark.django_db
def test_model_save_slug_change_records_old_slug():
    """A slug change persisted through Case.save() records history."""
    case = _make_case("first-slug", state=CaseState.DRAFT)
    case.slug = "second-slug"
    case.save()
    assert CaseSlugHistory.objects.filter(slug="first-slug", case=case).exists()


@pytest.mark.django_db
def test_creating_a_case_records_no_history():
    """A brand-new case (no prior slug) must not create a spurious history row."""
    _make_case("brand-new", state=CaseState.DRAFT)
    assert not CaseSlugHistory.objects.filter(slug="brand-new").exists()


# ---------------------------------------------------------------------------
# (b) A retired slug 301-redirects to the canonical URL (query string kept).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_retired_slug_301_redirects_for_public():
    case = _make_case("canonical-slug", state=CaseState.PUBLISHED)
    CaseSlugHistory.objects.create(slug="old-shared", case=case)

    resp = APIClient().get(URL.format("old-shared"))

    assert resp.status_code == 301
    assert resp["Location"].endswith("/api/cases/canonical-slug/")


@pytest.mark.django_db
def test_redirect_preserves_query_string():
    case = _make_case("canon2", state=CaseState.PUBLISHED)
    CaseSlugHistory.objects.create(slug="old2", case=case)

    resp = APIClient().get(URL.format("old2"), {"utm": "newsletter", "ref": "x"})

    assert resp.status_code == 301
    location = resp["Location"]
    assert "/api/cases/canon2/?" in location
    assert "utm=newsletter" in location
    assert "ref=x" in location


@pytest.mark.django_db
def test_end_to_end_reslug_then_old_url_redirects():
    """Full path: publish a case, re-slug it (via history), old URL redirects."""
    case = _make_case("draft-title", state=CaseState.DRAFT)
    # Re-slug while DRAFT (allowed), which records history, then mark published.
    case.slug = "final-slug"
    case.save()
    Case.objects.filter(pk=case.pk).update(state=CaseState.PUBLISHED)

    resp = APIClient().get(URL.format("draft-title"))

    assert resp.status_code == 301
    assert resp["Location"].endswith("/api/cases/final-slug/")


# ---------------------------------------------------------------------------
# (c) A genuinely unknown slug stays a 404.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unknown_slug_is_404():
    resp = APIClient().get(URL.format("no-such-case-anywhere"))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# (d) A reused slug resolves to its LIVE owner (200), never a redirect.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reused_slug_resolves_to_live_case_not_redirect():
    old_owner = _make_case("canon-old", state=CaseState.PUBLISHED, title="Old owner")
    # "recycled" is a retired slug of old_owner...
    CaseSlugHistory.objects.create(slug="recycled", case=old_owner)
    # ...but a DIFFERENT live case now owns "recycled".
    _make_case("recycled", state=CaseState.PUBLISHED, title="Live owner")

    resp = APIClient().get(URL.format("recycled"))

    assert resp.status_code == 200
    assert resp.data["slug"] == "recycled"
    assert resp.data["title"] == "Live owner"


@pytest.mark.django_db
def test_record_drops_history_row_colliding_with_new_live_slug():
    """Reclaiming a slug that is in history purges the now-shadowing row."""
    case = _make_case("home", state=CaseState.DRAFT)
    CaseSlugHistory.objects.create(slug="reclaim", case=case)
    # The case now reclaims "reclaim" as its live slug.
    case.slug = "reclaim"
    case.save()
    # The history row that would have shadowed the now-live slug is gone.
    assert not CaseSlugHistory.objects.filter(slug="reclaim").exists()
    # And the vacated "home" slug now redirects.
    assert CaseSlugHistory.objects.filter(slug="home", case=case).exists()


# ---------------------------------------------------------------------------
# (e) A retired slug never leaks a non-public case's existence.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_retired_slug_of_draft_case_does_not_leak_to_public():
    case = _make_case("draft-canon", state=CaseState.DRAFT, title="Secret draft")
    CaseSlugHistory.objects.create(slug="draft-old", case=case)

    resp = APIClient().get(URL.format("draft-old"))

    # Anonymous callers must get a plain 404 — no 301 that confirms the draft.
    assert resp.status_code == 404


@pytest.mark.django_db
def test_retired_slug_of_draft_case_redirects_for_caseworker():
    case = _make_case("draft-canon2", state=CaseState.DRAFT, title="Draft")
    CaseSlugHistory.objects.create(slug="draft-old2", case=case)

    resp = _caseworker_client().get(URL.format("draft-old2"))

    assert resp.status_code == 301
    assert resp["Location"].endswith("/api/cases/draft-canon2/")


@pytest.mark.django_db
def test_retired_slug_of_closed_case_is_404_even_for_caseworker():
    case = _make_case("closed-canon", state=CaseState.CLOSED, title="Closed")
    CaseSlugHistory.objects.create(slug="closed-old", case=case)

    resp = _caseworker_client().get(URL.format("closed-old"))

    # CLOSED cases are never exposed via this API, even to casework roles.
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# (f) ANY bulk update(slug=…) records history — the durable choke point.
#     A PUBLISHED case's slug is immutable through save(), so it is re-slugged
#     operationally via QuerySet.update(), which bypasses the save() hook.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bulk_update_slug_records_old_slug_for_published_case():
    case = _make_case("published-old", state=CaseState.PUBLISHED)

    Case.objects.filter(pk=case.pk).update(slug="published-new")

    case.refresh_from_db()
    assert case.slug == "published-new"
    assert CaseSlugHistory.objects.filter(slug="published-old", case=case).exists()
    # The new (now-live) slug is never recorded as its own predecessor.
    assert not CaseSlugHistory.objects.filter(slug="published-new").exists()


@pytest.mark.django_db
def test_published_reslug_via_update_then_old_url_redirects():
    """End-to-end: a published case re-slugged via update() 301-redirects."""
    case = _make_case("pub-canon-old", state=CaseState.PUBLISHED)

    Case.objects.filter(pk=case.pk).update(slug="pub-canon-new")

    resp = APIClient().get(URL.format("pub-canon-old"))
    assert resp.status_code == 301
    assert resp["Location"].endswith("/api/cases/pub-canon-new/")


@pytest.mark.django_db
def test_bulk_update_without_slug_records_nothing():
    """A non-slug bulk update must not touch CaseSlugHistory."""
    case = _make_case("state-change", state=CaseState.DRAFT)

    Case.objects.filter(pk=case.pk).update(state=CaseState.PUBLISHED)

    assert not CaseSlugHistory.objects.exists()


@pytest.mark.django_db
def test_bulk_update_to_same_slug_records_nothing():
    """update(slug=<current>) is a no-op — nothing retires."""
    case = _make_case("same", state=CaseState.PUBLISHED)

    Case.objects.filter(pk=case.pk).update(slug="same")

    assert not CaseSlugHistory.objects.filter(slug="same").exists()


# ---------------------------------------------------------------------------
# (g) backfill_slug_history rebuilds retired slugs from CaseReview snapshots.
# ---------------------------------------------------------------------------


def _run_backfill(*args) -> str:
    out = StringIO()
    call_command("backfill_slug_history", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_backfill_records_outdated_review_slug():
    from review.models import CaseReview

    case = _make_case("current-slug", state=CaseState.PUBLISHED, title="Distinct Title")
    # A review captured the case under a slug it no longer carries; resolves by
    # the exact snapshot title (tier 3).
    CaseReview.objects.create(slug="retired-slug", case_title="Distinct Title")

    # Dry-run writes nothing.
    _run_backfill()
    assert not CaseSlugHistory.objects.exists()

    out = _run_backfill("--apply")
    assert "retired-slug -> current-slug" in out
    assert CaseSlugHistory.objects.filter(slug="retired-slug", case=case).exists()


@pytest.mark.django_db
def test_backfill_skips_slug_that_is_still_live():
    from review.models import CaseReview

    _make_case("still-live", state=CaseState.PUBLISHED, title="Live One")
    CaseReview.objects.create(slug="still-live", case_title="Live One")

    _run_backfill("--apply")

    # The review's slug still addresses a live case — not outdated, not recorded.
    assert not CaseSlugHistory.objects.filter(slug="still-live").exists()


@pytest.mark.django_db
def test_backfill_is_idempotent():
    from review.models import CaseReview

    case = _make_case("live-canon", state=CaseState.PUBLISHED, title="Idem Title")
    CaseReview.objects.create(slug="idem-old", case_title="Idem Title")

    _run_backfill("--apply")
    _run_backfill("--apply")

    assert CaseSlugHistory.objects.filter(slug="idem-old", case=case).count() == 1
