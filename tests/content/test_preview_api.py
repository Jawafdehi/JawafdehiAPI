"""Tests for the headless article preview endpoint.

`HeadlessPreviewMixin` (on `ArticlePage`) stores the in-progress edit as a
`PagePreview` keyed by a signed token and redirects the edit-screen iframe to
the SPA. The SPA then calls the `page_preview` *detail* route with that token
(the pk in the path is a placeholder); these tests cover that endpoint
reconstructing and serializing the *unsaved* draft.
"""

import datetime

import pytest

from content.models import ArticleCategory, ArticleIndexPage, ArticlePage

# Detail route; the pk is a placeholder — the draft is resolved from the token.
PREVIEW_URL = "/api/cms/v2/page_preview/0/"
JSON = {"HTTP_ACCEPT": "application/json"}


def _make_published_article() -> ArticlePage:
    # The single ArticleIndexPage is created by migration content.0002.
    index = ArticleIndexPage.objects.first()
    assert index is not None, "ArticleIndexPage missing — content.0002 not applied"

    article = ArticlePage(
        title="Published title",
        slug="preview-test-article",
        category=ArticleCategory.UPDATE,
        date=datetime.date(2026, 6, 24),
        excerpt="Published excerpt",
        body=[("heading", "Section"), ("paragraph", "<p>Body text</p>")],
    )
    index.add_child(instance=article)
    article.save_revision().publish()
    return article


@pytest.mark.django_db
def test_preview_returns_unsaved_draft(client):
    article = _make_published_article()

    # Edits made in the form but NOT yet saved to the live page.
    article.title = "Draft title — not yet saved"
    article.excerpt = "Draft excerpt"
    preview = article.create_page_preview()

    resp = client.get(
        PREVIEW_URL,
        {"content_type": "content.articlepage", "token": preview.token, "fields": "*"},
        **JSON,
    )

    assert resp.status_code == 200
    data = resp.json()
    # The draft values are returned, not the published ones.
    assert data["title"] == "Draft title — not yet saved"
    assert data["excerpt"] == "Draft excerpt"
    # The StreamField body serializes through the same api_fields as published
    # pages, so the SPA renders preview identically.
    body_types = [block["type"] for block in data["body"]]
    assert "heading" in body_types
    assert "paragraph" in body_types


@pytest.mark.django_db
def test_preview_of_never_saved_page(client):
    """Previewing a brand-new article before its first save (pk is None).

    Mirrors Wagtail's PreviewOnCreate, which populates treebeard path/depth from
    the parent so the unsaved page is tree-aware (get_parent resolves the real
    saved parent) even though it has no pk.
    """
    index = ArticleIndexPage.objects.first()

    page = ArticlePage(
        title="Brand new draft",
        slug="brand-new-draft",
        category=ArticleCategory.NEWS,
        date=datetime.date(2026, 6, 24),
        excerpt="New excerpt",
        body=[("paragraph", "<p>New body</p>")],
    )
    page.depth = index.depth + 1
    if index.is_leaf():
        page.path = page._get_path(index.path, page.depth, 1)
    else:
        page.path = index.get_last_child()._inc_path()
    assert page.pk is None

    preview = page.create_page_preview()

    resp = client.get(
        PREVIEW_URL,
        {"content_type": "content.articlepage", "token": preview.token, "fields": "*"},
        **JSON,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Brand new draft"
    assert data["excerpt"] == "New excerpt"


@pytest.mark.django_db
def test_preview_missing_params_returns_400(client):
    resp = client.get(PREVIEW_URL, **JSON)
    assert resp.status_code == 400


@pytest.mark.django_db
def test_preview_unknown_token_returns_404(client):
    resp = client.get(
        PREVIEW_URL,
        {"content_type": "content.articlepage", "token": "does-not-exist"},
        **JSON,
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_preview_listing_route_not_exposed(client):
    # The endpoint must not double as a second published-pages listing.
    resp = client.get("/api/cms/v2/page_preview/", **JSON)
    assert resp.status_code == 404
