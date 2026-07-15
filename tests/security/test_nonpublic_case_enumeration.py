"""Adversarial: a non-public case must not leak through any ENUMERATION surface.

Threat model by state:
- DRAFT / CLOSED are fully hidden: no read surface may confirm they exist, even
  to a caller who guesses the exact slug (horizontal IDOR).
- IN_REVIEW is "unlisted": intentionally retrievable by a caller who already has
  the exact slug, but must NOT be discoverable — absent from the list endpoint,
  search, the statistics payload (aggregate-only), and the sitemap/discovery
  corpus. Knowing a slug is allowed; enumerating slugs is not.

Run with: ``uv run pytest -m security``.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType

pytestmark = [pytest.mark.security, pytest.mark.django_db]

SECRET_TITLE = "SECRET un-verified allegation against a sitting official"


def _make_case(state, slug, title=SECRET_TITLE):
    case = Case.objects.create(
        case_type=CaseType.CORRUPTION,
        title=title,
        slug=slug,
        description="d",
        state=CaseState.DRAFT,
    )
    if state != CaseState.DRAFT:
        # Bypass submit()/publish() content validation — we only need the row in
        # the target state to test visibility, not a valid transition.
        Case.objects.filter(pk=case.pk).update(state=state)
        case.refresh_from_db()
    return case


@pytest.mark.parametrize(
    "state,slug",
    [
        (CaseState.DRAFT, "secret-draft"),
        (CaseState.CLOSED, "secret-closed"),
    ],
)
def test_hidden_case_404s_on_detail_for_anon(state, slug):
    # DRAFT and CLOSED are fully hidden — the detail endpoint must not confirm
    # they exist. (IN_REVIEW is deliberately slug-retrievable; see below.)
    _make_case(state, slug)
    resp = APIClient().get(f"/api/cases/{slug}/")
    assert resp.status_code == 404, resp.content


def test_in_review_is_unlisted_but_slug_retrievable_for_anon():
    # IN_REVIEW is "unlisted": an anon caller WITH the exact slug may retrieve it
    # (200), but it must stay out of the public list endpoint.
    case = _make_case(CaseState.IN_REVIEW, "secret-in-review")
    client = APIClient()

    detail = client.get(f"/api/cases/{case.slug}/")
    assert detail.status_code == 200, detail.content
    assert detail.json()["slug"] == case.slug

    listing = client.get("/api/cases/")
    slugs = {c.get("slug") for c in listing.json().get("results", [])}
    assert case.slug not in slugs, "unlisted IN_REVIEW case leaked into the list"


@pytest.mark.parametrize(
    "state,slug",
    [
        (CaseState.DRAFT, "secret-draft"),
        (CaseState.IN_REVIEW, "secret-in-review"),
        (CaseState.CLOSED, "secret-closed"),
    ],
)
def test_nonpublic_case_absent_from_list_for_anon(state, slug):
    _make_case(state, slug)
    # Also seed a published case (DISTINCT title) so the list is non-empty.
    pub = _make_case(CaseState.PUBLISHED, "public-one", title="A public case")
    resp = APIClient().get("/api/cases/")
    assert resp.status_code == 200
    body = resp.json()
    results = body.get("results", body if isinstance(body, list) else [])
    slugs = {c.get("slug") for c in results}
    titles = {c.get("title") for c in results}
    assert slug not in slugs, f"{state} case leaked into the public list"
    assert pub.slug in slugs
    # The secret title must never appear in a public listing.
    assert SECRET_TITLE not in titles, f"{state} case title leaked into the list"


def test_statistics_is_aggregate_only_and_never_names_a_case():
    # The statistics endpoint intentionally COUNTS non-public cases (the public
    # "under investigation" tally = DRAFT + IN_REVIEW), so the leak risk isn't the
    # count — it's the shape ever growing a per-case field (a "recent cases" list,
    # a sample, etc.) that carries identifying text. Lock that contract: seed a
    # non-public case AND a distinctly-titled PUBLISHED case, then assert NEITHER
    # title nor slug appears anywhere in the payload (proving it stays aggregate),
    # while the non-public case IS reflected in the under-investigation count.
    # This fails the moment statistics starts emitting any case's identifying text
    # — which would leak a draft/in-review title to anon.
    _make_case(CaseState.IN_REVIEW, "secret-in-review")
    pub = _make_case(CaseState.PUBLISHED, "public-stats-one", title="A public case")

    resp = APIClient().get("/api/statistics/")
    assert resp.status_code == 200
    body = resp.json()
    blob = resp.content.decode()

    # Aggregate-only: no case title or slug (public OR non-public) is emitted.
    for needle in (SECRET_TITLE, "secret-in-review", "A public case", pub.slug):
        assert needle not in blob, f"statistics leaked case identifier: {needle!r}"

    # The non-public case is still counted (so the guard above isn't vacuously true
    # because statistics ignores non-public cases entirely).
    assert body.get("cases_under_investigation", 0) >= 1


def test_nonpublic_case_absent_from_public_corpus():
    # The discovery corpus (sitemap / ResourceSync source) is PUBLISHED-only.
    from discovery import corpus

    _make_case(CaseState.DRAFT, "secret-draft")
    _make_case(CaseState.IN_REVIEW, "secret-in-review")
    _make_case(CaseState.CLOSED, "secret-closed")
    pub = _make_case(CaseState.PUBLISHED, "public-one")

    iris = {r.iri for r in corpus.iter_resources((corpus.TYPE_CASE,))}
    assert any(pub.slug in iri for iri in iris)
    for leaked in ("secret-draft", "secret-in-review", "secret-closed"):
        assert not any(leaked in iri for iri in iris), f"{leaked} in public corpus"


def test_guessing_a_hidden_slug_still_404s_for_anon():
    # Horizontal IDOR: knowing the exact slug of a fully-hidden case (DRAFT /
    # CLOSED) must not help. (IN_REVIEW is intentionally slug-reachable — it is
    # unlisted, not hidden — so it is excluded here.)
    _make_case(CaseState.DRAFT, "known-slug-abc123")
    resp = APIClient().get("/api/cases/known-slug-abc123/")
    assert resp.status_code == 404
