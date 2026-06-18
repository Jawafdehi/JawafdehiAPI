from django.db import models
from modelcluster.fields import ParentalManyToManyField
from wagtail.admin.panels import FieldPanel, MultiFieldPanel
from wagtail.api import APIField
from wagtail.fields import StreamField
from wagtail.images.api.fields import ImageRenditionField
from wagtail.models import Page
from wagtail.search import index

from .blocks import ArticleStreamBlock
from .serializers import RelatedCaseSerializer


class ArticleCategory(models.TextChoices):
    UPDATE = "UPDATE", "Update"
    NEWS = "NEWS", "News"


class ArticleIndexPage(Page):
    """Single container page that all articles live under."""

    intro = models.TextField(blank=True)

    subpage_types = ["content.ArticlePage"]
    max_count = 1

    content_panels = Page.content_panels + [FieldPanel("intro")]
    api_fields = [APIField("intro")]


class ArticlePage(Page):
    """An update or news article, delivered headless via the API v2."""

    category = models.CharField(
        max_length=20,
        choices=ArticleCategory.choices,
        default=ArticleCategory.UPDATE,
        db_index=True,
    )
    date = models.DateField("Publication date")
    excerpt = models.TextField(
        blank=True, help_text="Short summary shown in listings and cards"
    )
    thumbnail = models.ForeignKey(
        "wagtailimages.Image",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    body = StreamField(ArticleStreamBlock(), blank=True)
    related_cases = ParentalManyToManyField(
        "cases.Case", blank=True, related_name="related_articles"
    )

    parent_page_types = ["content.ArticleIndexPage"]
    subpage_types = []

    content_panels = Page.content_panels + [
        MultiFieldPanel(
            [
                FieldPanel("category"),
                FieldPanel("date"),
                FieldPanel("thumbnail"),
                FieldPanel("excerpt"),
            ],
            heading="Article details",
        ),
        FieldPanel("body"),
        FieldPanel("related_cases"),
    ]

    search_fields = Page.search_fields + [
        index.SearchField("excerpt"),
        index.SearchField("body"),
        index.FilterField("category"),
        index.FilterField("date"),
    ]

    api_fields = [
        APIField("category"),
        APIField("date"),
        APIField("excerpt"),
        APIField("thumbnail", serializer=ImageRenditionField("fill-800x450")),
        APIField("body"),
        APIField("related_cases", serializer=RelatedCaseSerializer(many=True)),
    ]
