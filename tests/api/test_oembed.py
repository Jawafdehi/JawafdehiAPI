"""Tests for the oEmbed endpoint (GET /api/oembed/)."""

import datetime

import pytest
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from content.models import ArticleCategory, ArticleIndexPage, ArticlePage
from entities.services.publication import PublicationService
from entities.write_validation import normalize_authoring_payload


@pytest.fixture
def published_case(db):
    return Case.objects.create(
        title="Test Published Case",
        slug="test-published-case",
        state=CaseState.PUBLISHED,
        case_type=CaseType.CORRUPTION,
    )


@pytest.fixture
def draft_case(db):
    return Case.objects.create(
        title="Test Draft Case",
        slug="test-draft-case",
        state=CaseState.DRAFT,
        case_type=CaseType.CORRUPTION,
    )


@pytest.fixture
def in_review_case(db):
    return Case.objects.create(
        title="Test In Review Case",
        slug="test-in-review-case",
        state=CaseState.IN_REVIEW,
        case_type=CaseType.CORRUPTION,
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def published_update(db):
    index = ArticleIndexPage.objects.first()
    assert index is not None, "ArticleIndexPage missing — content.0002 not applied"

    article = ArticlePage(
        title="Public Accountability Update",
        slug="public-accountability-update",
        category=ArticleCategory.UPDATE,
        date=datetime.date(2026, 7, 6),
        excerpt="A public update for oEmbed.",
        body=[("paragraph", "<p>Update context</p>")],
    )
    index.add_child(instance=article)
    article.save_revision().publish()
    return article


@pytest.fixture
def published_entity(db):
    return PublicationService().create_entity(
        doc=normalize_authoring_payload(
            {
                "prefix": "person",
                "slug": "ram-bahadur-oembed",
                "type": "Person",
                "name": {"en": "Ram Bahadur oEmbed"},
                "change_description": "seed oembed entity",
            }
        ),
        author_id="oidc:seed",
        change_description="seed oembed entity",
    )


class TestOEmbedHappyPath:
    def test_json_response_for_published_case(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/test-published-case"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "1.0"
        assert data["type"] == "rich"
        assert data["title"] == "Test Published Case"
        assert data["provider_name"] == "Jawafdehi"
        assert "html" in data
        assert "test-published-case" in data["html"]
        assert 'frameborder="0"' in data["html"]

    def test_default_dimensions(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/test-published-case"},
        )
        data = resp.json()
        assert data["width"] == 600
        assert data["height"] == 300

    def test_maxwidth_applied(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {
                "url": "https://jawafdehi.org/case/test-published-case",
                "maxwidth": "800",
            },
        )
        data = resp.json()
        assert data["width"] == 800
        assert 'width="800"' in data["html"]

    def test_maxheight_applied(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {
                "url": "https://jawafdehi.org/case/test-published-case",
                "maxheight": "400",
            },
        )
        data = resp.json()
        assert data["height"] == 400
        assert 'height="400"' in data["html"]

    def test_maxwidth_zero_uses_default(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {
                "url": "https://jawafdehi.org/case/test-published-case",
                "maxwidth": "0",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["width"] == 600

    def test_maxwidth_negative_uses_default(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {
                "url": "https://jawafdehi.org/case/test-published-case",
                "maxwidth": "-100",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["width"] == 600

    def test_xml_format(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {
                "url": "https://jawafdehi.org/case/test-published-case",
                "format": "xml",
            },
        )
        assert resp.status_code == 200
        assert resp["Content-Type"] == "text/xml"
        assert "Test Published Case" in resp.content.decode()
        assert "<oembed>" in resp.content.decode()

    def test_case_url_with_www_prefix(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://www.jawafdehi.org/case/test-published-case"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Test Published Case"

    def test_case_url_with_trailing_slash(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/test-published-case/"},
        )
        assert resp.status_code == 200

    def test_case_url_with_http(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "http://jawafdehi.org/case/test-published-case"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Test Published Case"

    def test_update_url(self, client, published_update):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/updates/public-accountability-update"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Public Accountability Update"
        assert "/embed/updates/public-accountability-update" in data["html"]

    @pytest.mark.django_db(databases="__all__")
    def test_entity_url(self, client, published_entity):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/entity/person/ram-bahadur-oembed"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Ram Bahadur oEmbed"
        assert "/embed/entity/person/ram-bahadur-oembed" in data["html"]


class TestOEmbedRenderableAttributes:
    def test_all_required_fields_present(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/test-published-case"},
        )
        data = resp.json()
        required = [
            "version",
            "type",
            "title",
            "author_name",
            "author_url",
            "provider_name",
            "provider_url",
            "cache_age",
            "width",
            "height",
            "html",
        ]
        for field in required:
            assert field in data, f"Missing oEmbed field: {field}"

    def test_thumbnail_url_defaults_to_empty_string(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/test-published-case"},
        )
        assert resp.json()["thumbnail_url"] == ""

    def test_thumbnail_url_from_case(self, client, db):
        _case = Case.objects.create(
            title="Case With Thumbnail",
            slug="case-with-thumbnail",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            thumbnail_url="https://example.com/thumb.png",
        )
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/case-with-thumbnail"},
        )
        assert resp.json()["thumbnail_url"] == "https://example.com/thumb.png"


class TestOEmbedNotFound:
    def test_unpublished_case_returns_404(self, client, draft_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/test-draft-case"},
        )
        assert resp.status_code == 404

    def test_in_review_case_returns_404(self, client, in_review_case):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/test-in-review-case"},
        )
        assert resp.status_code == 404

    def test_nonexistent_case_returns_404(self, client, db):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/case/nonexistent-slug"},
        )
        assert resp.status_code == 404


class TestOEmbedBadRequest:
    def test_missing_url_returns_400(self, client):
        resp = client.get("/api/oembed/")
        assert resp.status_code == 400

    def test_empty_url_returns_400(self, client):
        resp = client.get("/api/oembed/", {"url": ""})
        assert resp.status_code == 400

    def test_non_jawafdehi_url_returns_400(self, client):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://example.com/something"},
        )
        assert resp.status_code == 400

    def test_unrelated_jawafdehi_url_returns_400(self, client):
        resp = client.get(
            "/api/oembed/",
            {"url": "https://jawafdehi.org/about"},
        )
        assert resp.status_code == 400

    def test_unsupported_format_returns_501(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {
                "url": "https://jawafdehi.org/case/test-published-case",
                "format": "html",
            },
        )
        assert resp.status_code == 501

    def test_invalid_maxwidth_uses_default(self, client, published_case):
        resp = client.get(
            "/api/oembed/",
            {
                "url": "https://jawafdehi.org/case/test-published-case",
                "maxwidth": "not-a-number",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["width"] == 600
