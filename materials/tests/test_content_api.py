"""The material content read plane.

``GET /api/materials/<source>/<ident>/content?type=…`` serves one kind of
stored content: the pages of the transcript, the ruled tables, or the charts
with their series. Adding ``&num=`` (pages) or ``&id=`` (tables, figures)
returns one item in full.

The cases that matter here are the ones that bite silently: the visibility gate
(content inherits its material's publication tier), the distinction
between *no content at all* and *content with none of this kind*, the
listings' refusal to carry their heavy column, and the URL split between a
multi-segment ``source`` and a hash-style ``ident``.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_content_api.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from materials.content_api import MAX_PAGE_SPAN
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import (
    DocumentContent,
    DocumentFigure,
    DocumentFigurePoint,
    DocumentPage,
    DocumentTable,
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


def _seed_content(material: Material) -> DocumentContent:
    content = DocumentContent.objects.create(
        material=material,
        doc_id="ciaa-2081-82",
        dataset="damo-da/ciaa-annual-reports",
        dataset_revision="c3c6c673",
        page_count=446,
        text_source="likhit",
        transcript_verdict="clean",
        figures_verdict="trustworthy",
        n_tables=2,
        n_figures=1,
        n_points=2,
        pages_with_text=1,
    )
    DocumentPage.objects.create(
        content=content,
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
    DocumentPage.objects.create(
        content=content,
        page_no=31,
        uid="ciaa-2081-82#p0031",
        text="",
        coverage_state="ocr_unread",
        ocr_source="",
        chars=0,
    )
    DocumentTable.objects.create(
        content=content,
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
    DocumentTable.objects.create(
        content=content,
        ordinal=2,
        key="p0031-t1",
        uid="ciaa-2081-82#p0031#t1",
        page_no=31,
        n_rows=8,
        n_cols=5,
        markdown="| a |\n| --- |\n| b |",
    )
    figure = DocumentFigure.objects.create(
        content=content,
        ordinal=1,
        key="p0030-f1",
        uid="ciaa-2081-82#p0030#f1",
        page_no=30,
        title="उजुरीको संख्या",
        chart_type="bar",
        unit="संख्या",
        verified=True,
    )
    DocumentFigurePoint.objects.create(
        figure=figure, point_index=1, label="२०८०/८१", value=19580.0
    )
    DocumentFigurePoint.objects.create(
        figure=figure,
        point_index=2,
        label="२०८१/८२",
        value=19488.0,
        is_estimated=True,
    )
    return content


class ContentListingTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_content(self.material)
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
        _seed_content(self.material)
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


class PageRangeTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        self.content = _seed_content(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/content"

    def test_range_returns_the_span_inclusive_and_in_page_order(self):
        resp = self.client.get(self.url, {"type": "page", "from": 30, "to": 31})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["from"], 30)
        self.assertEqual(body["to"], 31)
        self.assertEqual(body["count"], 2)
        self.assertEqual([p["page_no"] for p in body["items"]], [30, 31])

    def test_range_items_carry_their_text(self):
        # Unlike the bare listing, a range is a READ: the text is the payload.
        body = self.client.get(self.url, {"type": "page", "from": 30, "to": 30}).json()
        self.assertIn("१९४८८", body["items"][0]["text"])

    def test_a_single_page_range_is_allowed(self):
        body = self.client.get(self.url, {"type": "page", "from": 31, "to": 31}).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["page_no"], 31)

    def test_a_range_over_pages_we_do_not_have_is_an_empty_list(self):
        # Not a 404: the document exists and has content, we simply hold no
        # pages in that span. "We looked and there are none" is a 200.
        body = self.client.get(
            self.url, {"type": "page", "from": 900, "to": 905}
        ).json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["items"], [])

    def test_an_oversized_range_is_refused_not_truncated(self):
        # Silent truncation is the failure that matters here: a caller that asked
        # for 200 pages and received 50 without being told has no way to know its
        # answer is missing three quarters of the evidence.
        resp = self.client.get(
            self.url, {"type": "page", "from": 1, "to": MAX_PAGE_SPAN + 1}
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(str(MAX_PAGE_SPAN), resp.json()["detail"])

    def test_the_largest_allowed_range_is_accepted(self):
        resp = self.client.get(
            self.url, {"type": "page", "from": 1, "to": MAX_PAGE_SPAN}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_half_a_range_is_refused(self):
        for params in ({"from": 30}, {"to": 40}):
            with self.subTest(params=params):
                resp = self.client.get(self.url, {"type": "page", **params})
                self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_num_and_range_together_are_refused(self):
        resp = self.client.get(
            self.url, {"type": "page", "num": 30, "from": 30, "to": 31}
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_reversed_range_is_refused(self):
        resp = self.client.get(self.url, {"type": "page", "from": 40, "to": 30})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_negative_and_non_integer_bounds_are_refused(self):
        # from=0 is deliberately NOT here: a page_no of 0 can exist (an absent
        # upstream folio) and is listed, so it must be fetchable.
        for params in (
            {"from": -5, "to": 10},
            {"from": 1, "to": -1},
            {"from": "a", "to": "b"},
        ):
            with self.subTest(params=params):
                resp = self.client.get(self.url, {"type": "page", **params})
                self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_page_the_listing_shows_can_always_be_fetched(self):
        # The ingest coerces an absent or unparseable upstream folio to 0, so a
        # page_no of 0 can exist and IS listed. Refusing num=0 would mean a page a
        # client can see and can never read; the listing and the fetch have to
        # agree about what exists.
        DocumentPage.objects.create(
            content=self.content,
            page_no=0,
            uid="ciaa-2081-82#p0000",
            text="folio unknown",
            coverage_state="anchored",
        )
        listed = self.client.get(self.url, {"type": "page"}).json()
        self.assertIn(0, [p["page_no"] for p in listed["items"]])
        resp = self.client.get(self.url, {"type": "page", "num": 0})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["item"]["text"], "folio unknown")

    def test_a_negative_page_is_still_refused(self):
        resp = self.client.get(self.url, {"type": "page", "num": -1})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_keyed_type_refuses_page_arguments_rather_than_ignoring_them(self):
        # Serving the whole listing for ?type=table&from=1&to=10 would hand the
        # caller everything while it believes it asked for a slice.
        for kind in ("table", "figure"):
            for params in ({"num": 1}, {"from": 1, "to": 10}, {"to": 10}):
                with self.subTest(kind=kind, params=params):
                    resp = self.client.get(self.url, {"type": kind, **params})
                    self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ContentArgumentTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        self.content = _seed_content(self.material)
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

    def test_no_content_at_all_is_404(self):
        _seed_material(ident="no-content")
        resp = self.client.get(
            f"/api/materials/{SOURCE}/no-content/content", {"type": "table"}
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Authorization", resp["Vary"])

    def test_content_with_no_tables_is_an_empty_list_not_404(self):
        # The distinction the whole design turns on: "we have not looked inside
        # this document" and "we looked and there are none" are different facts.
        material = _seed_material(ident="empty-content")
        DocumentContent.objects.create(
            material=material,
            doc_id="ciaa-0000-00",
            dataset="damo-da/ciaa-annual-reports",
        )
        resp = self.client.get(
            f"/api/materials/{SOURCE}/empty-content/content", {"type": "table"}
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
        _seed_content(self.material)
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

    def test_a_caseworker_can_read_what_anon_cannot(self):
        # The other direction, and it is not symmetry for its own sake: if the
        # gate were simply inverted — or _can_see_nonpublic always False — every
        # assertion above would still pass while the route 404'd for EVERYONE.
        # A gate that denies the authorised caller is indistinguishable from a
        # working one unless something fetches through it.
        user = get_user_model().objects.create_user("cw-visibility", password="x")
        user.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
        self.client.force_authenticate(user)
        resp = self.client.get(self.url, {"type": "table"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["count"], 2)


class ContentCachingTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        self.content = _seed_content(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/content"

    def test_the_etag_comes_back_as_a_304(self):
        first = self.client.get(self.url, {"type": "page"})
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        again = self.client.get(
            self.url, {"type": "page"}, HTTP_IF_NONE_MATCH=first["ETag"]
        )
        self.assertEqual(again.status_code, status.HTTP_304_NOT_MODIFIED)
        self.assertEqual(again["ETag"], first["ETag"])
        self.assertIn("Authorization", again["Vary"])

    def test_a_stale_etag_still_gets_the_body(self):
        resp = self.client.get(
            self.url, {"type": "page"}, HTTP_IF_NONE_MATCH='W/"0.000000"'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("items", resp.json())

    def test_a_reingest_invalidates_the_tag(self):
        first = self.client.get(self.url, {"type": "page"})
        # auto_now: any re-ingest moves ingested_at, and with it the tag.
        self.content.save()
        again = self.client.get(
            self.url, {"type": "page"}, HTTP_IF_NONE_MATCH=first["ETag"]
        )
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertNotEqual(again["ETag"], first["ETag"])

    def test_a_malformed_request_is_still_400_not_304(self):
        # The 304 check runs after validation: handing back "unchanged" for a
        # question we never looked at would let a client cache its own typo.
        first = self.client.get(self.url, {"type": "page"})
        resp = self.client.get(
            self.url, {"type": "banana"}, HTTP_IF_NONE_MATCH=first["ETag"]
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_star_matches_any_stored_content(self):
        resp = self.client.get(self.url, {"type": "table"}, HTTP_IF_NONE_MATCH="*")
        self.assertEqual(resp.status_code, status.HTTP_304_NOT_MODIFIED)


class ContentRoutingTests(APITestCase):
    databases = "__all__"

    def test_content_does_not_collide_with_the_detail_route(self):
        # material-detail's ``source`` is multi-segment, so without the ordering
        # in urls.py this URL would match it as source=<source>/<ident>,
        # ident="content" and 404 as a missing material.
        material = _seed_material()
        _seed_content(material)
        resp = self.client.get(
            f"/api/materials/{SOURCE}/{IDENT}/content", {"type": "figure"}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["type"], "figure")

    def test_multi_segment_source_still_resolves(self):
        material = _seed_material(source="province/koshi", ident="fy2083-84-21000011")
        _seed_content(material)
        resp = self.client.get(
            "/api/materials/province/koshi/fy2083-84-21000011/content",
            {"type": "table"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["count"], 2)
