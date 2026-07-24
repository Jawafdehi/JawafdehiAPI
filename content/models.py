from functools import cached_property

from django.db import models
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel, InlinePanel, MultiFieldPanel
from wagtail.api import APIField
from wagtail.fields import StreamField
from wagtail.images.api.fields import ImageRenditionField
from wagtail.models import Page
from wagtail.search import index
from wagtail_headless_preview.models import HeadlessPreviewMixin

from .blocks import ArticleStreamBlock
from .serializers import RelatedCaseSerializer


class ArticleCategory(models.TextChoices):
    UPDATE = "UPDATE", "Update"
    NEWS = "NEWS", "News"


class ArticlePageRelatedCase(models.Model):
    """Through model linking an ``ArticlePage`` to a ``cases.Case``.

    Modelled as a ``ParentalKey`` child so the article edit form can use an
    ``InlinePanel`` backed by the case chooser modal — the picker lazy-loads a
    paginated, searchable list instead of rendering every case inline (the old
    ``ParentalManyToManyField`` multi-select did the latter, which is what made
    the edit page slow to load).
    """

    article_page = ParentalKey(
        "ArticlePage",
        on_delete=models.CASCADE,
        related_name="related_cases_through",
    )
    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="+",
    )

    class Meta:
        unique_together = ("article_page", "case")
        verbose_name = "Related case"
        verbose_name_plural = "Related cases"

    def __str__(self):
        return f"{self.article_page.title} → {self.case.slug}"


class ArticleIndexPage(Page):
    """Single container page that all articles live under."""

    intro = models.TextField(blank=True)

    subpage_types = ["content.ArticlePage"]
    max_count = 1

    content_panels = Page.content_panels + [FieldPanel("intro")]
    api_fields = [APIField("intro")]


class ArticlePage(HeadlessPreviewMixin, Page):
    """An update or news article, delivered headless via the API v2.

    ``HeadlessPreviewMixin`` overrides ``serve_preview`` so the edit-screen
    preview iframe redirects to the SPA (which renders the real article) rather
    than rendering a server-side template — these pages have none.
    """

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
        InlinePanel("related_cases_through", label="Related cases"),
    ]

    search_fields = Page.search_fields + [
        index.SearchField("excerpt"),
        index.SearchField("body"),
        index.FilterField("category"),
        index.FilterField("date"),
    ]

    @cached_property
    def related_cases(self):
        """The linked cases, for API serialization and template use.

        Reads through the ``related_cases_through`` child rows. Callers
        serializing lists of articles should
        ``prefetch_related("related_cases_through__case")`` to avoid N+1
        queries; cached per-instance so a single article only queries once.
        """
        return [rel.case for rel in self.related_cases_through.all()]

    api_fields = [
        APIField("category"),
        APIField("date"),
        APIField("excerpt"),
        APIField("thumbnail", serializer=ImageRenditionField("fill-800x450")),
        APIField("body"),
        APIField("related_cases", serializer=RelatedCaseSerializer(many=True)),
    ]
