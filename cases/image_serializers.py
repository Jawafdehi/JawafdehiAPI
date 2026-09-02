"""Responsive-image serialization for case thumbnails and heroes.

Wagtail's own :class:`~wagtail.images.api.fields.ImageRenditionField` takes one
filter spec and calls ``get_rendition`` once, so a responsive ``srcset`` built
from it costs one declared API field (and one query) per width. This module
serializes a whole width ladder as a single field instead, via
``Image.get_renditions(*specs)`` — one batched fetch-or-create for all of them.

Two things the specs here are load-bearing about:

* **Every spec ends in** ``|format-webp``. Renditions otherwise inherit the
  source format, and case uploads are overwhelmingly PNG. Measured on one
  source: 1600x900 as PNG is 2.70 MB and as WebP is 332 KB, so format buys more
  than resolution costs.
* **They are** ``width-`` **specs, not** ``fill-``. Both surfaces render into a
  box whose aspect ratio changes with the viewport (the card is a tall crop on
  mobile and a wide one in list view; the hero is 208px tall on a phone and
  560px on a desktop), and CSS ``object-fit: cover`` already does the cropping.
  A ``fill-`` spec would bake in one aspect ratio and crop twice.

The ladders stop at 1600: ``s3.jawafdehi.org`` has no edge cache and no
Cloudflare image resizing, so every rendition byte is origin egress on every
view, and a 2400px tier would only serve desktop displays that already get a
sharp image from the 1600 at 2x.
"""

from __future__ import annotations

from collections import OrderedDict

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from wagtail.images import get_image_model
from wagtail.images.models import SourceImageIOError

from jawafdehi_shared.storage import absolute_media_url

#: OpenAPI shape of a :class:`SrcsetRenditionField` value. Declared by hand
#: because the field is a bare ``serializers.Field`` — without this drf-spectacular
#: types it as a free-form object, and the two image fields are the part of the
#: case payload a client is most likely to code against.
SRCSET_SCHEMA = {
    "type": "object",
    "nullable": True,
    "properties": {
        "src": {"type": "string", "format": "uri"},
        "srcset": {"type": "string"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "alt": {"type": "string"},
    },
    "required": ["src", "srcset", "width", "height", "alt"],
}

#: Width ladder for the case card (home page, search results, embeds). The card
#: box tops out around 400 CSS px, so 800 covers it at 2x and 1200 covers a
#: wide list-view row on a high-DPI display.
CARD_SPECS = (
    "width-400|format-webp",
    "width-800|format-webp",
    "width-1200|format-webp",
)

#: Width ladder for the detail-page hero, which spans the full container.
HERO_SPECS = (
    "width-640|format-webp",
    "width-1280|format-webp",
    "width-1600|format-webp",
)


def _width_of(spec: str) -> int:
    """The pixel width a ``width-N|...`` spec resolves to.

    Used only to order the ``srcset`` and to pick the ``src`` fallback. Parsing
    the spec rather than reading ``rendition.width`` keeps this working when a
    source image is narrower than the requested width — Wagtail does not upscale,
    so the rendition's real width is what lands in the ``srcset`` descriptor,
    while this ordering stays stable.

    Returns 0 for anything unparseable. ``get_renditions`` keys its result by the
    *cleaned* spec, and for an SVG source ``clean_filter_for_svg`` strips the
    raster-only operations — the leading ``width-N`` survives that, but sorting
    must not raise if some future cleaning step changes more than expected.
    """
    head = spec.split("|", 1)[0]
    try:
        return int(head.removeprefix("width-"))
    except ValueError:
        return 0


class ImageIdField(serializers.PrimaryKeyRelatedField):
    """Write field for an image FK that validates the id but yields the id.

    ``PrimaryKeyRelatedField`` normally puts the resolved *instance* into
    ``validated_data``. The case PATCH path persists scalars with a bulk
    ``Case.objects.update(**fields)``, which needs the raw pk for the
    ``..._image_id`` attname — an instance there raises. So take the existence
    check (an unknown id 422s instead of surfacing as an IntegrityError deep in
    the update) and hand back the pk.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", get_image_model().objects.all())
        kwargs.setdefault("pk_field", serializers.IntegerField())
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        return super().to_internal_value(data).pk

    def to_representation(self, value):
        # ``value`` is already the pk here: the snapshot the editor patches is
        # built from ``case.thumbnail_image_id``, not from the relation.
        return value


@extend_schema_field(SRCSET_SCHEMA)
class SrcsetRenditionField(serializers.Field):
    """Serialize one image as a full responsive ``srcset`` payload.

    Emits the shape the frontend ``<img>`` needs directly::

        {
          "src": "https://.../foo.width-1200.format-webp.webp",
          "srcset": "https://.../foo.width-400... 400w, ... 1200w",
          "width": 1200,
          "height": 675,
          "alt": "..."
        }

    ``src`` is the largest rendition, so a browser that ignores ``srcset``
    (and any consumer that reads the JSON and wants one URL — share cards,
    the search index) still gets a usable image.

    ``alt`` is the image's ``description`` and nothing else — see
    :meth:`_alt_text`.

    Mirrors ``ImageRenditionField``'s failure contract: an unreadable source
    image serializes to ``{"error": "SourceImageIOError"}`` rather than raising,
    so one broken upload cannot 500 a whole case list.
    """

    def __init__(self, specs=CARD_SPECS, **kwargs):
        self.specs = tuple(specs)
        kwargs.setdefault("read_only", True)
        super().__init__(**kwargs)

    @staticmethod
    def _alt_text(image) -> str:
        """Alt text for ``image``, or empty when it has none.

        NOT ``Rendition.alt``, which is Wagtail's ``default_alt_text`` and falls
        back to the image *title*. The upload endpoint titles an image after the
        file it arrived as, so that fallback would put ``IMG_0001.jpg`` in front
        of a screen reader — worse than nothing, because an empty alt lets the
        consumer supply its own description (the card and the hero both use the
        case title) while a filename overrides it with noise.
        """
        return getattr(image, "description", "") or ""

    def to_representation(self, value):
        if value is None:
            return None
        try:
            renditions = value.get_renditions(*self.specs)
        except SourceImageIOError:
            return OrderedDict([("error", "SourceImageIOError")])

        ordered = sorted(renditions.items(), key=lambda item: _width_of(item[0]))
        if not ordered:
            # No spec survived cleaning, so there is no image to point at. Same
            # contract as a null image rather than an IndexError on ordered[-1]:
            # this field renders inside case LISTS, where one odd source must not
            # take the whole page down.
            return None

        # absolute_media_url, not ``Rendition.full_url``: full_url absolutizes
        # against WAGTAILADMIN_BASE_URL (the newsroom), which is the wrong host
        # for public media and, off S3, points at a machine that does not have
        # the file. Both are no-ops in production, where MEDIA_URL is already the
        # absolute R2 domain — this only matters in dev and CI, where getting it
        # wrong means every case image in the editor is a broken link.
        largest = ordered[-1][1]
        return OrderedDict(
            [
                ("src", absolute_media_url(largest.url)),
                (
                    "srcset",
                    ", ".join(
                        f"{absolute_media_url(r.url)} {r.width}w" for _spec, r in ordered
                    ),
                ),
                ("width", largest.width),
                ("height", largest.height),
                ("alt", self._alt_text(value)),
            ]
        )
