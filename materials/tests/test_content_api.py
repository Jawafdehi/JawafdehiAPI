"""The material content read plane.

``GET /api/materials/<source>/<ident>/content?type=…`` serves one kind of
extracted content: the pages of the transcript, the ruled tables, or the charts
with their series. Adding ``&num=`` (pages) or ``&id=`` (tables, figures)
returns one item in full.

The cases that matter here are the ones that bite silently: the visibility gate
(extracted content inherits its material's publication tier), the distinction
between *no extraction at all* and *an extraction with none of this kind*, the
listings' refusal to carry their heavy column, and the URL split between a
multi-segment ``source`` and a hash-style ``ident``.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_content_api.py
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.test import APITestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import (
    DocumentExtraction,
    ExtractedFigure,
    ExtractedFigurePoint,
    ExtractedPage,
    ExtractedTable,
    Material,
    Policy,
    Visibility,
)

SOURCE = "ciaa_annual_report"
IDENT = "ee0b4f80b24b8665"


def _seed_material(source: str = SOURCE, ident: str = IDENT, **kwargs) -> Material:
    iri = f"https://jawafdehi.org/material/{source}/{ident}"
    return Material.objects.create(
        iri=iri,
        material_type="document",
        source=source,
        ident=ident,
        data={
            "@context": MATERIAL_CONTEXT,
            "@type": "DigitalDocument",
            "@id": iri,
            "name": {"ne": "पैँतिसौँ वार्षिक प्रतिवेदन"},
        },
        **kwargs,
    )


def _seed_extraction(material: Material) -> DocumentExtraction:
    extraction = DocumentExtraction.objects.create(
        material=material,
        doc_id="ciaa-2081-82",
        dataset="damo-da/ciaa-annual-reports",
        dataset_revision="c3c6c673",
        page_count=446,
        text_source="likhit",
        transcript_verdict="clean",
        figures_verdict="trustworthy",
        n_pages=446,
        n_tables=2,
        n_figures=1,
        n_points=2,
        pages_with_text=1,
    )
    ExtractedPage.objects.create(
        extraction=extraction,
        page_no=30,
        uid="ciaa-2081-82#p0030",
        text="आयोगमा दर्ता भएका उजुरीको संख्या १९४८८ रहेको छ।",
        coverage_state="anchored",
        anchor_fidelity="tight",
        ocr_source="pdf_text_layer",
        n_table_blocks=1,
        chars=46,
    )
    # A page we hold but could NOT read. Its empty text is a statement about us,
    # not about the page, and the API has to keep those apart.
    ExtractedPage.objects.create(
        extraction=extraction,
        page_no=31,
        uid="ciaa-2081-82#p0031",
        text="",
        coverage_state="ocr_unread",
        ocr_source="",
        chars=0,
    )
    ExtractedTable.objects.create(
        extraction=extraction,
        ordinal=1,
        key="p0030-t1",
        uid="ciaa-2081-82#p0030#t1",
        page_no=30,
        caption="तालिका २.१ आयोगमा दर्ता भएका उजुरीको संख्या",
        header=["आ.व.", "संख्या"],
        n_rows=9,
        n_cols=4,
        fidelity="tight",
        markdown="| आ.व. | संख्या |\n| --- | --- |\n| २०८१/८२ | १९४८८ |",
    )
    ExtractedTable.objects.create(
        extraction=extraction,
        ordinal=2,
        key="p0031-t1",
        uid="ciaa-2081-82#p0031#t1",
        page_no=31,
        n_rows=8,
        n_cols=5,
        markdown="| a |\n| --- |\n| b |",
    )
    figure = ExtractedFigure.objects.create(
        extraction=extraction,
        ordinal=1,
        key="p0030-f1",
        uid="ciaa-2081-82#p0030#f1",
        page_no=30,
        title="उजुरीको संख्या",
        chart_type="bar",
        unit="संख्या",
        verified=True,
    )
    ExtractedFigurePoint.objects.create(
        figure=figure, point_index=1, label="२०८०/८१", value=19580.0
    )
    ExtractedFigurePoint.objects.create(
        figure=figure,
        point_index=2,
        label="२०८१/८२",
        value=19488.0,
        is_estimated=True,
    )
    return extraction


class ContentListingTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_extraction(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/content"

    def test_table_listing(self):
        resp = self.client.get(self.url, {"type": "table"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["material"], self.material.iri)
        self.assertEqual(body["type"], "table")
        self.assertEqual(body["count"], 2)
        self.assertEqual([t["key"] for t in body["items"]], ["p0030-t1", "p0031-t1"])

    def test_table_listing_omits_markdown(self):
        # The whole reason the plane is split: one report's markdown runs to
        # hundreds of KiB, so a stub must never carry it.
        body = self.client.get(self.url, {"type": "table"}).json()
        for stub in body["items"]:
            self.assertNotIn("markdown", stub)

    def test_page_listing_omits_text(self):
        # Same contract as tables, and the one that matters most: the pages hold
        # the document's entire transcript.
        body = self.client.get(self.url, {"type": "page"}).json()
        self.assertEqual(body["count"], 2)
        for stub in body["items"]:
            self.assertNotIn("text", stub)

    def test_page_listing_exposes_unread_pages(self):
        # A client must be able to see which pages we could not read BEFORE it
        # fetches one and gets an empty string, or it will present a failed OCR
        # as a blank page.
        body = self.client.get(self.url, {"type": "page"}).json()
        states = {p["page_no"]: p["coverage_state"] for p in body["items"]}
        self.assertEqual(states, {30: "anchored", 31: "ocr_unread"})

    def test_figure_listing_inlines_its_series(self):
        body = self.client.get(self.url, {"type": "figure"}).json()
        self.assertEqual(body["count"], 1)
        points = body["items"][0]["points"]
        self.assertEqual([p["label"] for p in points], ["२०८०/८१", "२०८१/८२"])

    def test_estimated_rides_on_every_point(self):
        # A value read off a chart image is a weaker claim than a printed one and
        # must never serialize as equivalent.
        body = self.client.get(self.url, {"type": "figure"}).json()
        points = body["items"][0]["points"]
        self.assertEqual([p["estimated"] for p in points], [False, True])

    def test_listings_carry_etag_and_vary(self):
        resp = self.client.get(self.url, {"type": "table"})
        self.assertTrue(resp["ETag"].startswith('W/"'))
        self.assertIn("Authorization", resp["Vary"])


class ContentItemTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_extraction(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/content"

    def test_one_table_by_key(self):
        resp = self.client.get(self.url, {"type": "table", "id": "p0030-t1"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        item = resp.json()["item"]
        self.assertEqual(item["key"], "p0030-t1")
        self.assertIn("१९४८८", item["markdown"])

    def test_table_is_not_addressable_by_ordinal(self):
        # Ordinals are recomputed on every ingest. Addressing by one would make a
        # saved link quietly resolve to a DIFFERENT table after an upstream
        # revision — a 200 with the wrong content, which is worse than a 404.
        resp = self.client.get(self.url, {"type": "table", "id": "1"})
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_one_figure_by_key(self):
        resp = self.client.get(self.url, {"type": "figure", "id": "p0030-f1"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()["item"]["points"]), 2)

    def test_one_page_by_printed_folio(self):
        resp = self.client.get(self.url, {"type": "page", "num": 30})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        item = resp.json()["item"]
        self.assertEqual(item["page_no"], 30)
        self.assertIn("१९४८८", item["text"])

    def test_unread_page_returns_its_state_not_just_empty_text(self):
        item = self.client.get(self.url, {"type": "page", "num": 31}).json()["item"]
        self.assertEqual(item["text"], "")
        self.assertEqual(item["coverage_state"], "ocr_unread")

    def test_missing_page_is_404(self):
        resp = self.client.get(self.url, {"type": "page", "num": 9999})
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Authorization", resp["Vary"])


class ContentArgumentTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_extraction(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/content"

    def test_missing_type_is_400_not_404(self):
        # The route exists; the argument is wrong. A 404 here would send a client
        # looking for a missing document.
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("type is required", resp.json()["detail"])

    def test_unknown_type_is_400_and_names_the_valid_ones(self):
        resp = self.client.get(self.url, {"type": "paragraph"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        detail = resp.json()["detail"]
        for kind in ("page", "table", "figure"):
            self.assertIn(kind, detail)

    def test_page_rejects_id(self):
        resp = self.client.get(self.url, {"type": "page", "id": "p0030-t1"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_table_rejects_num(self):
        resp = self.client.get(self.url, {"type": "table", "num": "30"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_integer_page_num_is_400(self):
        resp = self.client.get(self.url, {"type": "page", "num": "thirty"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ContentAbsenceTests(APITestCase):
    databases = "__all__"

    def test_no_extraction_at_all_is_404(self):
        _seed_material(ident="not-extracted")
        resp = self.client.get(
            f"/api/materials/{SOURCE}/not-extracted/content", {"type": "table"}
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Authorization", resp["Vary"])

    def test_extraction_with_no_tables_is_an_empty_list_not_404(self):
        # The distinction the whole design turns on: "we have not looked inside
        # this document" and "we looked and there are none" are different facts.
        material = _seed_material(ident="empty-extraction")
        DocumentExtraction.objects.create(
            material=material,
            doc_id="ciaa-0000-00",
            dataset="damo-da/ciaa-annual-reports",
        )
        resp = self.client.get(
            f"/api/materials/{SOURCE}/empty-extraction/content", {"type": "table"}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["count"], 0)
        self.assertEqual(resp.json()["items"], [])

    def test_unknown_material_is_404(self):
        resp = self.client.get(
            f"/api/materials/{SOURCE}/no-such-doc/content", {"type": "table"}
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class ContentVisibilityTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material(
            visibility=Visibility.PRIVATE, visibility_policy=Policy.PRIVATE
        )
        _seed_extraction(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/content"

    def test_private_material_hides_its_content_from_anon(self):
        # Content inherits its material's publication tier. Without the gate a
        # draft case's evidence would be readable by guessing the sub-path.
        for params in (
            {"type": "table"},
            {"type": "table", "id": "p0030-t1"},
            {"type": "page", "num": 30},
            {"type": "figure"},
        ):
            with self.subTest(params=params):
                resp = self.client.get(self.url, params)
                self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
                self.assertIn("Authorization", resp["Vary"])


class ContentRoutingTests(APITestCase):
    databases = "__all__"

    def test_content_does_not_collide_with_the_detail_route(self):
        # material-detail's ``source`` is multi-segment, so without the ordering
        # in urls.py this URL would match it as source=<source>/<ident>,
        # ident="content" and 404 as a missing material.
        material = _seed_material()
        _seed_extraction(material)
        resp = self.client.get(
            f"/api/materials/{SOURCE}/{IDENT}/content", {"type": "figure"}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["type"], "figure")

    def test_multi_segment_source_still_resolves(self):
        material = _seed_material(source="province/koshi", ident="fy2083-84-21000011")
        _seed_extraction(material)
        resp = self.client.get(
            "/api/materials/province/koshi/fy2083-84-21000011/content",
            {"type": "table"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["count"], 2)
