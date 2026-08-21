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


# The `serve_preview` signatures of `HeadlessPreviewMixin` (wagtail-headless-preview)
# and Wagtail's own `PreviewableMixin` genuinely disagree upstream. Overriding it is
# the entire reason the mixin is here and it must come FIRST in the MRO — see the
# docstring below. Nothing in this repo can reconcile the two declarations.
class ArticlePage(HeadlessPreviewMixin, Page):  # ty: ignore[invalid-method-override]
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

    # Two renditions off the one ``thumbnail`` FK, because the same image is
    # consumed at wildly different sizes: a card in the /updates grid, and a
    # full-bleed hero on the article page. `thumbnail_large` carries an explicit
    # ``source`` so it reads the same FK under a different payload key.
    #
    # The hero sits in a ``max-w-4xl`` (896px) column at ``w-full``, so 800px was
    # being upscaled by the browser even at DPR 1. 1600px covers it at ~1.8x;
    # a true 2x isn't reachable anyway, since the article thumbnails we hold top
    # out around 1672px wide.
    #
    # ``format-webp`` is on both: these are PNG uploads, and a PNG rendition at
    # this size is measured in megabytes (1600x900 lands at ~2.7MB as PNG vs
    # ~330KB as WebP — smaller than the 640KB the 800x450 PNG costs today).
    #
    # NOTE ``fill-`` never upscales — ``FillOperation`` crops to the target ratio
    # and then only resizes when ``scale < 1.0``. So a source below the target
    # doesn't error and doesn't blur: it silently returns a SMALLER rendition,
    # and both specs collapse to the same size. Live example: a 750x400 source
    # yields 712x400 for `thumbnail` AND `thumbnail_large`, so that article keeps
    # a soft hero until a bigger source is uploaded. The payload's width/height
    # report the real size, so the client's intrinsic sizing stays honest —
    # nothing breaks, you just don't get the resolution you asked for. Article
    # thumbnails want to be >=1600px on the long edge.
    api_fields = [
        APIField("category"),
        APIField("date"),
        APIField("excerpt"),
        APIField("thumbnail", serializer=ImageRenditionField("fill-800x450|format-webp")),
        APIField(
            "thumbnail_large",
            serializer=ImageRenditionField(
                "fill-1600x900|format-webp", source="thumbnail"
            ),
        ),
        # Social preview. Deliberately NOT WebP and NOT 16:9: link unfurlers
        # want 1200x630 (1.91:1), and WhatsApp/LinkedIn previews are unreliable
        # with WebP. Without this field the og:image would have silently become
        # a WebP the moment `thumbnail` did.
        APIField(
            "og_image",
            serializer=ImageRenditionField(
                "fill-1200x630|format-jpeg|jpegquality-85", source="thumbnail"
            ),
        ),
        APIField("body"),
        APIField("related_cases", serializer=RelatedCaseSerializer(many=True)),
    ]
