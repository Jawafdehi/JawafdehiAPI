"""Pure tests for the PPMO OCR enrichment's local (free / pre-Bedrock) logic.

No network, no Bedrock, no DB. Covers the two guards that protect the searchable
``text`` field and the paid budget:

* :func:`is_readable` — a length-only gate once let a junk OCR layer (dense
  mojibake in a font self-reporting as "Helvetica") into the index.
* :func:`render_page_png` — a page whose base64 exceeds Bedrock's per-image
  ceiling is rejected outright with a ``ValidationException``, losing the page.
"""

from __future__ import annotations

import base64

import unittest

from django.test import SimpleTestCase

from materials.sourcing.ppmo.ocr import (
    BEDROCK_MAX_B64_BYTES,
    DEFAULT_DPI,
    is_readable,
    render_page_png,
)

#: ``pymupdf`` ships only in the optional ``bigo-enrichment`` extra, and CI runs a
#: plain ``uv sync --frozen``. Import it softly and skip ONLY the render tests, so a
#: base install still exercises :func:`is_readable` (pure string logic, and the guard
#: that keeps mojibake out of the searchable text field). ``ocr.py`` imports pymupdf
#: lazily inside its functions for the same reason — cf. ``review/converter.py``.
try:
    import pymupdf
except ImportError:  # pragma: no cover — depends on install extras, not on logic
    pymupdf = None

_needs_pymupdf = unittest.skipIf(
    pymupdf is None, "pymupdf ships in the optional bigo-enrichment extra"
)

#: Real mojibake sampled from a PPMO scan's junk OCR text layer.
_MOJIBAKE = "yq[q{.* de[|- ffi qrrqtf ][|.,* }{[|] .*ffi q[|{ tf][ .,*}{ ][|" * 8

#: Devanagari; what a converted legacy-font or native Unicode page looks like.
_NEPALI = "सार्वजनिक खरिद पत्रिका बोलपत्र सम्झौता रकम ठेक्का कम्पनी " * 8

#: An English passage, as the bulletins' English sections extract.
_ENGLISH = (
    "The Public Procurement Monitoring Office publishes this bulletin to "
    "record contract awards made by government entities during the fiscal "
    "year, including the name of each supplier and the awarded amount. " * 3
)


class IsReadableTests(SimpleTestCase):
    def test_rejects_mojibake(self):
        # The regression that wrote garbage into the searchable text field: long
        # enough to clear a pure length gate, unreadable by any font map.
        assert len(_MOJIBAKE) > 200
        assert is_readable(_MOJIBAKE) is False

    def test_accepts_devanagari(self):
        assert is_readable(_NEPALI) is True

    def test_accepts_english_prose(self):
        assert is_readable(_ENGLISH) is True

    def test_rejects_short_text(self):
        # A near-empty divider page has a text layer but nothing worth keeping;
        # it must route to vision OCR rather than being accepted as "done".
        assert is_readable("सार्वजनिक खरिद") is False

    def test_rejects_consonant_soup_that_passes_the_char_ratio(self):
        # All-plausible characters, but no vowels — not words.
        assert is_readable("bcdfg hjklm npqrs tvwxz " * 20) is False

    def test_empty_is_not_readable(self):
        assert is_readable("") is False


def _page(width: int, height: int):
    """A one-page PDF of the given point size, with content so it doesn't
    compress to nothing."""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    # Dense varied drawing so the PNG can't trivially RLE down to a few KB.
    for i in range(0, int(height), 7):
        page.draw_line(
            pymupdf.Point(0, i),
            pymupdf.Point(width, (i * 13) % max(1, int(height))),
            color=((i % 11) / 11, (i % 7) / 7, (i % 5) / 5),
            width=1.5,
        )
    return doc, page


@_needs_pymupdf
class RenderPagePngTests(SimpleTestCase):
    def test_normal_page_renders_at_requested_dpi(self):
        doc, page = _page(612, 792)  # US Letter
        try:
            png = render_page_png(page, DEFAULT_DPI)
            expected_w = int(612 * DEFAULT_DPI / 72)
            # Within a pixel of the requested scale — i.e. NOT downscaled.
            assert abs(pymupdf.Pixmap(png).width - expected_w) <= 1
        finally:
            doc.close()

    def test_oversized_page_is_downscaled_under_the_ceiling(self):
        # A very large canvas at 150 DPI blows past Bedrock's per-image limit.
        doc, page = _page(2400, 3400)
        try:
            full = page.get_pixmap(
                matrix=pymupdf.Matrix(DEFAULT_DPI / 72, DEFAULT_DPI / 72)
            ).tobytes("png")
            assert len(base64.b64encode(full)) > BEDROCK_MAX_B64_BYTES, (
                "fixture no longer exceeds the ceiling; the test would be vacuous"
            )
            png = render_page_png(page, DEFAULT_DPI)
            assert len(base64.b64encode(png)) <= BEDROCK_MAX_B64_BYTES
            assert pymupdf.Pixmap(png).width < pymupdf.Pixmap(full).width
        finally:
            doc.close()

    def test_returns_a_render_even_if_the_floor_is_still_too_big(self):
        # Never return nothing: an oversized attempt still beats a hole in the
        # transcript, so the caller always gets bytes to try.
        doc, page = _page(612, 792)
        try:
            png = render_page_png(page, DEFAULT_DPI, max_b64=1)
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
        finally:
            doc.close()
