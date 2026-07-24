"""Tests for the published-article API (`/api/cms/v2/pages/`).

Focused on the case cross-links: the `related_cases` inline (a ParentalKey
through model, edited via the case chooser) and the inline `case` StreamField
block. Both serialize a minimal case payload; the Case model has no `case_id`
column (the slug is the public identifier), so these also guard against
reintroducing dropped columns into the payload.
"""

import datetime

import pytest

from cases.models import Case, CaseState, CaseType
from content.models import (
    ArticleCategory,
    ArticleIndexPage,
    ArticlePage,
    ArticlePageRelatedCase,
)

PAGES_URL = "/api/cms/v2/pages/"
JSON = {"HTTP_ACCEPT": "application/json"}


@pytest.fixture
def case(db):
    return Case.objects.create(
        title="एनसेल कर विवाद परीक्षण मुद्दा",
        slug="ncell-tax-test-case",
        state=CaseState.PUBLISHED,
        case_type=CaseType.CORRUPTION,
    )


@pytest.fixture
def article_with_case_links(case):
    index = ArticleIndexPage.objects.first()
    assert index is not None, "ArticleIndexPage missing — content.0002 not applied"

    article = ArticlePage(
        title="Update with case links",
        slug="update-with-case-links",
        category=ArticleCategory.UPDATE,
        date=datetime.date(2026, 7, 6),
        excerpt="Cross-linked update",
        body=[
            ("paragraph", "<p>Context</p>"),
            ("case", {"case": case, "note": "How this case relates"}),
        ],
    )
    index.add_child(instance=article)
    article.related_cases_through.add(ArticlePageRelatedCase(case=case))
    article.save_revision().publish()
    return article


@pytest.mark.django_db
def test_article_serializes_related_cases(client, case, article_with_case_links):
    resp = client.get(
        PAGES_URL,
        {"type": "content.ArticlePage", "slug": article_with_case_links.slug, "fields": "*"},
        **JSON,
    )

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    data = items[0]

    assert data["related_cases"] == [
        {"id": case.pk, "title": case.title, "slug": case.slug}
    ]

    case_blocks = [block for block in data["body"] if block["type"] == "case"]
    assert len(case_blocks) == 1
    assert case_blocks[0]["value"] == {
        "case": {"id": case.pk, "title": case.title, "slug": case.slug},
        "note": "How this case relates",
    }
