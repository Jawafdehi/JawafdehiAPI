"""Tests for the reviewer overhaul: case_id identity, claim payload, reviewer
provenance, and the grouped (full-history) review list."""

import pytest
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from review.models import CaseReview
from tests.conftest import create_user_with_role

SUBMIT_URL = "/api/casework/reviews/submit/"
CLAIM_URL = "/api/casework/jobs/claim/"
GROUPED_URL = "/api/casework/reviews/grouped/"


def _result_url(pk):
    return f"/api/casework/jobs/{pk}/result/"


@pytest.fixture
def client(db):
    user = create_user_with_role("rev_user", "rev_user@example.com", "Contributor")
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _make_case(slug, title, case_id, court_cases=None):
    return Case.objects.create(
        title=title,
        slug=slug,
        case_id=case_id,
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        court_cases=court_cases,
    )


# ── identity at submit ───────────────────────────────────────────────────────


def test_submit_stores_stable_case_id_from_registry(client):
    _make_case("alpha-case", "Alpha", "case-alpha123")
    resp = client.post(SUBMIT_URL, {"slug": "alpha-case"}, format="json")
    assert resp.status_code == 201, resp.content
    review = CaseReview.objects.get(id=resp.data["id"])
    assert review.case_id == "case-alpha123"
    assert review.slug == "alpha-case"
    assert review.case_state == CaseState.DRAFT
    assert review.case_type == CaseType.CORRUPTION


def test_active_dedupe_is_by_case_id_across_slug_and_court_number(client):
    # Same case reached two ways resolves to one case_id -> one active review.
    _make_case("beta-case", "Beta", "case-beta456", ["special:081-CR-0079"])
    first = client.post(SUBMIT_URL, {"slug": "beta-case"}, format="json")
    assert first.status_code == 201
    dup = client.post(
        SUBMIT_URL, {"court_case_number": "special:081-CR-0079"}, format="json"
    )
    assert dup.status_code == 409
    assert dup.data["review_id"] == first.data["id"]


# ── claim returns basic details, not content ─────────────────────────────────


def test_claim_returns_basic_details_and_runs(client):
    review = CaseReview.objects.create(
        case_id="case-gamma",
        slug="gamma-case",
        case_title="Gamma",
        case_state="DRAFT",
        case_type="CORRUPTION",
        status=CaseReview.STATUS_PENDING,
    )
    resp = client.post(CLAIM_URL, {}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.data["review_id"] == review.id
    assert resp.data["case_id"] == "case-gamma"
    assert resp.data["slug"] == "gamma-case"
    # Only the basics the API identified at submit — no evidence/content here.
    assert resp.data["case"]["title"] == "Gamma"
    assert resp.data["case"]["state"] == "DRAFT"
    assert "evidence" not in resp.data["case"]
    assert set(resp.data["config"]) == {
        "pass_threshold",
        "revise_threshold",
        "llm_samples",
    }
    review.refresh_from_db()
    assert review.status == CaseReview.STATUS_RUNNING


def test_claim_empty_queue_is_204(client):
    assert client.post(CLAIM_URL, {}, format="json").status_code == 204


# ── reviewer provenance on result submit ─────────────────────────────────────


def test_submit_result_records_reviewers_from_usage(client):
    review = CaseReview.objects.create(
        case_id="case-delta", slug="delta-case", status=CaseReview.STATUS_RUNNING
    )
    payload = {
        "status": "done",
        "case_title": "Delta",
        "case_state": "DRAFT",
        "case_type": "CORRUPTION",
        "source_count": 2,
        "sources_converted": 2,
        "duration_seconds": 1.0,
        "result": {
            "overall_score": 88,
            "disposition": "PASS",
            "token_usage": {
                "by_provider": [
                    {
                        "tier": "premium",
                        "provider": "claude_cli",
                        "model": "opus",
                        "calls": 7,
                    },
                    {
                        "tier": "cheap",
                        "provider": "codex_cli",
                        "model": "gpt-5-codex",
                        "calls": 40,
                    },
                ]
            },
        },
    }
    resp = client.post(_result_url(review.id), payload, format="json")
    assert resp.status_code == 200, resp.content
    review.refresh_from_db()
    assert review.status == CaseReview.STATUS_DONE
    assert review.reviewers == [
        {"tier": "premium", "provider": "claude_cli", "model": "opus", "calls": 7},
        {"tier": "cheap", "provider": "codex_cli", "model": "gpt-5-codex", "calls": 40},
    ]
    assert resp.data["reviewers"][0]["provider"] == "claude_cli"


# ── grouped list: all executions per case, across page boundaries ────────────


def test_grouped_returns_all_executions_per_case(client):
    # Two cases; the "ncell" case has executions that, on the flat list, would
    # straddle a page boundary. Grouped, they must appear together.
    for i in range(3):
        CaseReview.objects.create(
            case_id="case-ncell",
            slug="ncell",
            case_title="Ncell",
            status=CaseReview.STATUS_DONE,
        )
    CaseReview.objects.create(
        case_id="case-other",
        slug="other",
        case_title="Other",
        status=CaseReview.STATUS_DONE,
    )

    resp = client.get(GROUPED_URL)
    assert resp.status_code == 200, resp.content
    assert resp.data["count"] == 2  # two cases, not four reviews
    groups = {g["slug"]: g for g in resp.data["results"]}
    assert set(groups) == {"ncell", "other"}
    assert len(groups["ncell"]["executions"]) == 3
    assert groups["ncell"]["case_title"] == "Ncell"
    # latest is the most recent execution (highest id) of the case.
    ncell_ids = [e["id"] for e in groups["ncell"]["executions"]]
    assert ncell_ids == sorted(ncell_ids, reverse=True)
    assert groups["ncell"]["latest"]["id"] == ncell_ids[0]
    # case_id is the internal grouping key and must not be exposed.
    assert "case_id" not in groups["ncell"]


def test_grouped_paginates_by_case(client):
    for n in range(25):
        CaseReview.objects.create(
            case_id=f"case-{n:03d}", slug=f"c-{n:03d}", status=CaseReview.STATUS_DONE
        )
    resp = client.get(GROUPED_URL, {"page_size": 10})
    assert resp.status_code == 200
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 10
    assert resp.data["next"] is not None
