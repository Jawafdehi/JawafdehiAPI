"""What a read of an unparsed document tells a client.

There is no way to ASK for a document to be parsed, and that is deliberate:
everything that arrives is queued on arrival and the sweep reconciles the rest,
so a request to extract would be a request for work already scheduled. What the
read owes a client is the distinction between the two silences — nobody has
opened this yet, and there is nothing here to open.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_extract_api.py
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.test import APITestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

SOURCE = "ciaa_annual_report"
IDENT = "ee0b4f80b24b8665"
IRI = f"https://jawafdehi.org/material/{SOURCE}/{IDENT}"
URL = f"/api/materials/{SOURCE}/{IDENT}/content"
PDF_URL = "https://s3.jawafdehi.org/case_uploads/abc123.pdf"


def _seed_material(media: list | None = None) -> Material:
    data = {
        "@context": MATERIAL_CONTEXT,
        "@type": "DigitalDocument",
        "@id": IRI,
        "name": {"ne": "पैँतिसौँ वार्षिक प्रतिवेदन"},
    }
    if media is not None:
        data["associatedMedia"] = media
    return Material.objects.create(
        iri=IRI, material_type="document", source=SOURCE, ident=IDENT, data=data
    )


def _pdf_media(url: str = PDF_URL, **extra) -> dict:
    return {
        "@type": "MediaObject",
        "contentUrl": url,
        "jawafdehi:linkRole": "RAW",
        **extra,
    }


class ExtractionHintTests(APITestCase):
    databases = {"default", "ngm"}

    def test_a_404_says_whether_asking_would_help(self):
        _seed_material([_pdf_media()])

        response = self.client.get(URL, {"type": "page"})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Without this a client cannot tell "nobody has opened this document yet"
        # from "there is no document here to open", and the two have opposite
        # next steps.
        self.assertTrue(response.data["extractable"])

    def test_a_material_with_nothing_to_open_says_so(self):
        _seed_material([])

        response = self.client.get(URL, {"type": "table"})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(response.data["extractable"])
