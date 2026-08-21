"""Tests for the published-article API (`/api/cms/v2/pages/`).

Focused on the case cross-links: the `related_cases` inline (a ParentalKey
through model, edited via the case chooser) and the inline `case` StreamField
block. Both serialize a minimal case payload; the Case model has no `case_id`
column (the slug is the public identifier), so these also guard against
reintroducing dropped columns into the payload.

Also guards the two thumbnail renditions. The card and the article hero consume
the same image at very different sizes, and the split only holds because the
listing serializer returns just the fields the caller asks for — the frontend's
list query names `thumbnail` and so must not be charged for the hero rendition.
"""

import datetime

import pytest
from django.core.cache import caches
from wagtail.images import get_image_model
from wagtail.images.tests.utils import get_test_image_file

from cases.models import Case, CaseState, CaseType
from content.models import (
    ArticleCategory,
    ArticleIndexPage,
    ArticlePage,
    ArticlePageRelatedCase,
)

PAGES_URL = "/api/cms/v2/pages/"
JSON = {"HTTP_ACCEPT": "application/json"}

# Exactly what `src/services/cms-api.ts` sends for the /updates grid. Kept
# verbatim so a rename on either side shows up as a failure here.
LIST_FIELDS = "title,category,date,excerpt,thumbnail"


def _isolate_rendition_cache():
    """Drop Wagtail's rendition cache between image fixtures.

    Wagtail caches renditions keyed on image pk + filter spec, NOT on the file.
    Under pytest each test rolls back, so every fixture image is created as pk=1
    — meaning a second test's image inherits the FIRST one's cached rendition and
    reports its dimensions. That looked exactly like `fill-` upscaling a small
    source to 800x450. It isn't: in production images have distinct pks.
    """
    for cache in caches.all():
        cache.clear()


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


@pytest.fixture
def article_with_thumbnail(db, settings, tmp_path):
    # Renditions are written through the default storage, which is a plain
    # FileSystemStorage under the test settings (no AWS creds). Point it at tmp
    # so generating them doesn't litter the checkout's MEDIA_ROOT.
    settings.MEDIA_ROOT = str(tmp_path)
    _isolate_rendition_cache()

    image = get_image_model().objects.create(
        title="जलहरी मुद्दाको तस्बिर",
        # Wider than the 1600px hero rendition, so `fill-` downscales rather
        # than upscaling — the shape a real article thumbnail should have.
        file=get_test_image_file(filename="jalahari.png", size=(1800, 1013)),
    )

    index = ArticleIndexPage.objects.first()
    assert index is not None, "ArticleIndexPage missing — content.0002 not applied"

    article = ArticlePage(
        title="Update with a thumbnail",
        slug="update-with-a-thumbnail",
        category=ArticleCategory.UPDATE,
        date=datetime.date(2026, 8, 19),
        excerpt="Has an image",
        thumbnail=image,
    )
    index.add_child(instance=article)
    article.save_revision().publish()
    return article


@pytest.mark.django_db
def test_detail_serializes_both_thumbnail_renditions(client, article_with_thumbnail):
    """The hero needs ~1600px; the card rendition is only 800px wide."""
    resp = client.get(
        PAGES_URL,
        {
            "type": "content.ArticlePage",
            "slug": article_with_thumbnail.slug,
            "fields": "*",
        },
        **JSON,
    )

    assert resp.status_code == 200
    data = resp.json()["items"][0]

    assert (data["thumbnail"]["width"], data["thumbnail"]["height"]) == (800, 450)
    assert (data["thumbnail_large"]["width"], data["thumbnail_large"]["height"]) == (
        1600,
        900,
    )
    # Distinct renditions, not the same file relabelled.
    assert data["thumbnail"]["url"] != data["thumbnail_large"]["url"]


@pytest.mark.django_db
def test_both_renditions_are_webp(client, article_with_thumbnail):
    """A PNG hero at 1600x900 costs megabytes; the source upload is a PNG."""
    resp = client.get(
        PAGES_URL,
        {
            "type": "content.ArticlePage",
            "slug": article_with_thumbnail.slug,
            "fields": "*",
        },
        **JSON,
    )

    data = resp.json()["items"][0]
    assert data["thumbnail"]["url"].endswith(".webp")
    assert data["thumbnail_large"]["url"].endswith(".webp")


@pytest.mark.django_db
def test_og_image_is_jpeg_at_social_aspect_ratio(client, article_with_thumbnail):
    """Link unfurlers want 1200x630 and are unreliable with WebP, so the social
    rendition must not drift onto the display renditions' format or ratio."""
    resp = client.get(
        PAGES_URL,
        {
            "type": "content.ArticlePage",
            "slug": article_with_thumbnail.slug,
            "fields": "*",
        },
        **JSON,
    )

    og = resp.json()["items"][0]["og_image"]
    assert (og["width"], og["height"]) == (1200, 630)
    assert og["url"].endswith(".jpg")


@pytest.fixture
def article_with_a_small_thumbnail(db, settings, tmp_path):
    """A thumbnail narrower than the 1600px hero target — the case that revealed
    `fill-` does not upscale."""
    settings.MEDIA_ROOT = str(tmp_path)
    _isolate_rendition_cache()

    image = get_image_model().objects.create(
        title="सानो तस्बिर",
        file=get_test_image_file(filename="sano.png", size=(750, 400)),
    )
    index = ArticleIndexPage.objects.first()
    article = ArticlePage(
        title="Update with a small thumbnail",
        slug="update-with-a-small-thumbnail",
        category=ArticleCategory.UPDATE,
        date=datetime.date(2026, 8, 19),
        excerpt="Small image",
        thumbnail=image,
    )
    index.add_child(instance=article)
    article.save_revision().publish()
    return article


@pytest.mark.django_db
def test_a_small_source_downgrades_rather_than_upscaling(
    client, article_with_a_small_thumbnail
):
    """`fill-` crops to the ratio then resizes only when `scale < 1.0`, so a
    source below the target yields a SMALLER rendition and both specs collapse to
    the same size. Pinned because the payload's width/height are what the client
    reserves space with — if this ever started upscaling, callers would silently
    get a blurry, much heavier hero instead of an honest small one."""
    resp = client.get(
        PAGES_URL,
        {
            "type": "content.ArticlePage",
            "slug": article_with_a_small_thumbnail.slug,
            "fields": "*",
        },
        **JSON,
    )

    data = resp.json()["items"][0]
    small = (data["thumbnail"]["width"], data["thumbnail"]["height"])
    large = (data["thumbnail_large"]["width"], data["thumbnail_large"]["height"])

    assert small == (712, 400)
    assert large == small
    # Never larger than the source could honestly support.
    assert data["og_image"]["width"] <= 750


@pytest.mark.django_db
def test_listing_omits_the_hero_rendition(client, article_with_thumbnail):
    """The /updates grid asks for `thumbnail` only, so it must not pay for the
    1600px hero — generating it costs a rendition write per article, and
    shipping its URL invites a client into loading it in a card."""
    resp = client.get(
        PAGES_URL,
        {"type": "content.ArticlePage", "fields": LIST_FIELDS},
        **JSON,
    )

    assert resp.status_code == 200
    data = next(
        item
        for item in resp.json()["items"]
        if item["meta"]["slug"] == article_with_thumbnail.slug
    )

    assert data["thumbnail"]["width"] == 800
    assert "thumbnail_large" not in data
    assert "og_image" not in data
