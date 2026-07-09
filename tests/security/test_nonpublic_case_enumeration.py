"""Adversarial: a non-public case must not leak through ANY read surface.

Threat: an anonymous visitor (or the accused party) tries to discover a case that
has not cleared the verification queue — DRAFT / IN_REVIEW / CLOSED — by hitting
every surface that enumerates or resolves cases, not just /api/cases/.

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
        (CaseState.IN_REVIEW, "secret-in-review"),
        (CaseState.CLOSED, "secret-closed"),
    ],
)
def test_nonpublic_case_404s_on_detail_for_anon(state, slug):
    _make_case(state, slug)
    resp = APIClient().get(f"/api/cases/{slug}/")
    assert resp.status_code == 404, resp.content


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


def test_nonpublic_case_not_in_statistics_titles():
    # Statistics aggregates must not carry a non-public case's identifying text.
    _make_case(CaseState.IN_REVIEW, "secret-in-review")
    resp = APIClient().get("/api/statistics/")
    assert resp.status_code == 200
    assert SECRET_TITLE not in resp.content.decode()


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


def test_guessing_a_valid_nonpublic_slug_still_404s_for_anon():
    # Horizontal IDOR: knowing the exact slug of a non-public case must not help.
    _make_case(CaseState.IN_REVIEW, "known-slug-abc123")
    resp = APIClient().get("/api/cases/known-slug-abc123/")
    assert resp.status_code == 404
