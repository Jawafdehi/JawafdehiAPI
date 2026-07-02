from wagtail import blocks
from wagtail.documents.blocks import DocumentChooserBlock
from wagtail.embeds.blocks import EmbedBlock
from wagtail.images.blocks import ImageChooserBlock

from .chooser import case_chooser_viewset


def _absolute(url, context):
    """Absolutize a media URL against the request so the cross-domain SPA can
    load it. The headless SPA runs on a different origin than the API, so a
    relative ``/media/...`` / ``/documents/...`` URL would 404 there."""
    request = (context or {}).get("request")
    return request.build_absolute_uri(url) if request else url


class APIImageChooserBlock(ImageChooserBlock):
    """Image chooser that serializes a rendition URL for the headless client.

    The stock block serializes to a bare image id, which the SPA can't render.
    """

    def get_api_representation(self, value, context=None):
        if not value:
            return None
        data = {"id": value.pk, "title": value.title, "alt": value.default_alt_text}
        try:
            rendition = value.get_rendition("width-1200")
        except Exception:
            return data
        data.update(
            {
                "url": _absolute(rendition.url, context),
                "width": rendition.width,
                "height": rendition.height,
            }
        )
        return data


class APIDocumentChooserBlock(DocumentChooserBlock):
    """Document chooser that serializes the download URL and filename."""

    def get_api_representation(self, value, context=None):
        if not value:
            return None
        return {
            "id": value.pk,
            "title": value.title,
            "url": _absolute(value.url, context),
            "filename": value.filename,
        }


# Dynamically-built ChooserBlock wired to the case chooser modal. Kept at this
# module path so block migrations can import it by its deconstructed path.
BaseCaseChooserBlock = case_chooser_viewset.get_block_class(
    name="BaseCaseChooserBlock",
    module_path="content.blocks",
)


class CaseChooserBlock(BaseCaseChooserBlock):
    def get_api_representation(self, value, context=None):
        if value is None:
            return None
        return {
            "id": value.pk,
            "case_id": value.case_id,
            "title": value.title,
            "slug": value.slug,
        }


class ImageBlock(blocks.StructBlock):
    image = APIImageChooserBlock()
    caption = blocks.CharBlock(required=False)

    class Meta:
        icon = "image"
        label = "Image"


class CaseBlock(blocks.StructBlock):
    case = CaseChooserBlock()
    note = blocks.CharBlock(
        required=False, help_text="Optional context shown alongside the case"
    )

    class Meta:
        icon = "doc-full"
        label = "Related case"


class ArticleStreamBlock(blocks.StreamBlock):
    heading = blocks.CharBlock(form_classname="title", icon="title")
    paragraph = blocks.RichTextBlock(icon="pilcrow")
    image = ImageBlock()
    quote = blocks.BlockQuoteBlock()
    document = APIDocumentChooserBlock(icon="doc-full-inverse")
    embed = EmbedBlock(icon="media")
    case = CaseBlock()

    class Meta:
        block_counts = {}
