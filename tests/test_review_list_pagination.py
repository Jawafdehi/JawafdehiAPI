"""Tests for paginated (lazy-loadable) listing on the review list endpoint."""

import pytest
from rest_framework.test import APIClient

from review.models import CaseReview
from tests.conftest import create_user_with_role

LIST_URL = "/api/casework/reviews/"


@pytest.fixture
def client(db):
    user = create_user_with_role("rev_reader", "rev_reader@example.com", "Contributor")
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _make_reviews(n):
    # Distinct case_id per row: the active-review uniqueness constraint is keyed
    # on case_id, so same-status rows must not share one.
    CaseReview.objects.bulk_create(
        [CaseReview(case_id=f"cid-{i:03d}", slug=f"case-{i:03d}") for i in range(n)]
    )


def test_list_is_paginated_with_default_page_size(client):
    _make_reviews(25)
    resp = client.get(LIST_URL)
    assert resp.status_code == 200
    # Paginated envelope, default page size (settings PAGE_SIZE = 20).
    assert resp.data["count"] == 25
    assert len(resp.data["results"]) == 20
    assert resp.data["next"] is not None
    assert resp.data["previous"] is None


def test_client_can_set_page_size_for_lazy_loading(client):
    _make_reviews(25)
    resp = client.get(LIST_URL, {"page_size": 5})
    assert resp.status_code == 200
    assert len(resp.data["results"]) == 5
    assert resp.data["next"] is not None


def test_paging_walks_all_rows_without_overlap(client):
    _make_reviews(12)
    seen = []
    page = 1
    while True:
        resp = client.get(LIST_URL, {"page_size": 5, "page": page})
        assert resp.status_code == 200
        seen.extend(r["id"] for r in resp.data["results"])
        if resp.data["next"] is None:
            break
        page += 1
    assert len(seen) == 12
    assert len(set(seen)) == 12  # no duplicates across pages


def test_page_size_is_capped(client):
    _make_reviews(30)
    resp = client.get(LIST_URL, {"page_size": 1000})
    assert resp.status_code == 200
    # max_page_size caps the client-requested size.
    assert len(resp.data["results"]) == 30
    assert len(resp.data["results"]) <= 100
