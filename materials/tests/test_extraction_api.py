"""The document-extraction read plane.

``GET /api/materials/<source>/<ident>/extraction`` serves a manifest (provenance,
figures with their series, table stubs) and
``…/extraction/tables/<key>`` serves one table's markdown.

The cases that matter here are the ones that bite silently: the visibility gate
(an extraction inherits its material's publication tier), the absent-extraction
404 the client uses to decide whether to show a tab at all, and the URL split
between a multi-segment ``source`` and a hash-style ``ident``.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_extraction_api.py
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.test import APITestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import (
    DocumentExtraction,
    ExtractedFigure,
    ExtractedFigurePoint,
    ExtractedTable,
    Material,
    Visibility,
)

SOURCE = "ciaa_annual_report"
IDENT = "8._28th_ciaa_annual_report_2074_75"


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
            "name": {"ne": "२८औं वार्षिक प्रतिवेदन"},
        },
        **kwargs,
    )


def _seed_extraction(material: Material) -> DocumentExtraction:
    extraction = DocumentExtraction.objects.create(
        material=material,
        doc_id="ciaa-2074-75",
        dataset="damo-da/ciaa-annual-reports",
        dataset_revision="abc1234",
        page_count=255,
        text_source="likhit",
        transcript_verdict="clean",
        figures_verdict="trustworthy",
    )
    ExtractedTable.objects.create(
        extraction=extraction,
        ordinal=1,
        key="p0018-t1",
        uid="ciaa-2074-75#p0018#t1",
        page_no=18,
        caption="तालिका २.१ आयोगमा दर्ता भएका उजुरीको संख्या",
        header=["आ.व.", "संख्या"],
        n_rows=9,
        n_cols=4,
        fidelity="tight",
        markdown="| आ.व. | संख्या |\n| --- | --- |\n| २०७४/७५ | १९४८८ |",
    )
    ExtractedTable.objects.create(
        extraction=extraction,
        ordinal=2,
        key="p0019-t1",
        uid="ciaa-2074-75#p0019#t1",
        page_no=19,
        n_rows=8,
        n_cols=5,
        markdown="| a |\n| --- |\n| b |",
    )
    figure = ExtractedFigure.objects.create(
        extraction=extraction,
        ordinal=1,
        key="p0018-f1",
        uid="ciaa-2074-75#p0018#f1",
        page_no=18,
        title="उजुरीको संख्या",
        chart_type="bar",
        unit="संख्या",
        verified=True,
    )
    ExtractedFigurePoint.objects.create(
        figure=figure, point_index=1, label="२०७३/७४", value=19580.0
    )
    ExtractedFigurePoint.objects.create(
        figure=figure,
        point_index=2,
        label="२०७४/७५",
        value=19488.0,
        is_estimated=True,
    )
    return extraction


class ExtractionManifestTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_extraction(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}/extraction"

    def test_manifest_shape(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["material"], self.material.iri)
        self.assertEqual(body["counts"], {"tables": 2, "figures": 1, "points": 2})
        self.assertEqual(body["provenance"]["doc_id"], "ciaa-2074-75")
        self.assertEqual(body["provenance"]["dataset_revision"], "abc1234")

    def test_manifest_omits_table_markdown(self):
        # The whole reason the plane is split in two: one report's markdown runs
        # to hundreds of KiB, so a stub must never carry it.
        body = self.client.get(self.url).json()
        self.assertEqual(len(body["tables"]), 2)
        for table in body["tables"]:
            self.assertNotIn("markdown", table)
        self.assertEqual(body["tables"][0]["caption"].startswith("तालिका"), True)

    def test_figures_carry_their_full_series_with_the_estimated_flag(self):
        body = self.client.get(self.url).json()
        points = body["figures"][0]["points"]
        self.assertEqual([p["value"] for p in points], [19580.0, 19488.0])
        # A chart routinely mixes printed and chart-read values; the flag is
        # per point, never per figure.
        self.assertEqual([p["estimated"] for p in points], [False, True])

    def test_manifest_carries_etag_and_varies_on_authorization(self):
        resp = self.client.get(self.url)
        self.assertTrue(resp["ETag"].startswith('W/"'))
        self.assertIn("Authorization", resp["Vary"])

    def test_trailing_slash_is_accepted(self):
        self.assertEqual(
            self.client.get(self.url + "/").status_code, status.HTTP_200_OK
        )

    def test_material_without_extraction_404s(self):
        _seed_material(ident="no_extraction_here")
        resp = self.client.get(
            f"/api/materials/{SOURCE}/no_extraction_here/extraction"
        )
        # The client keys "should I show the tab?" off this 404, so it must be a
        # 404 and not an empty 200.
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Authorization", resp["Vary"])

    def test_unknown_material_404s(self):
        resp = self.client.get(f"/api/materials/{SOURCE}/nope/extraction")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_hash_style_ident_is_not_swallowed_by_the_source_group(self):
        # A bare-hash ident matches the same character class as a source segment,
        # so the route only resolves correctly by backtracking off the trailing
        # ``/extraction``. Pin it: a regression here silently 404s four of the
        # thirty-five CIAA reports.
        material = _seed_material(ident="fac3ebb039506855")
        _seed_extraction(material)
        resp = self.client.get(
            f"/api/materials/{SOURCE}/fac3ebb039506855/extraction"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["material"], material.iri)


class ExtractionVisibilityTests(APITestCase):
    databases = "__all__"

    def test_private_material_hides_its_extraction_from_anon(self):
        material = _seed_material(ident="draft_evidence")
        _seed_extraction(material)
        Material.objects.filter(pk=material.iri).update(
            visibility=Visibility.PRIVATE
        )
        resp = self.client.get(
            f"/api/materials/{SOURCE}/draft_evidence/extraction"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_private_material_hides_its_tables_from_anon(self):
        material = _seed_material(ident="draft_evidence_two")
        _seed_extraction(material)
        Material.objects.filter(pk=material.iri).update(
            visibility=Visibility.PRIVATE
        )
        resp = self.client.get(
            f"/api/materials/{SOURCE}/draft_evidence_two/extraction/tables/p0018-t1"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_soft_deleted_material_hides_its_extraction(self):
        material = _seed_material(ident="deleted_one")
        _seed_extraction(material)
        Material.objects.filter(pk=material.iri).update(is_deleted=True)
        resp = self.client.get(
            f"/api/materials/{SOURCE}/deleted_one/extraction"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class ExtractionTableTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_extraction(self.material)

    def _url(self, key):
        return f"/api/materials/{SOURCE}/{IDENT}/extraction/tables/{key}"

    def test_table_returns_markdown(self):
        resp = self.client.get(self._url("p0018-t1"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["key"], "p0018-t1")
        self.assertIn("१९४८८", body["markdown"])
        self.assertEqual(body["header"], ["आ.व.", "संख्या"])

    def test_manifest_key_is_what_the_table_route_accepts(self):
        # The contract the client depends on: take ``key`` from the manifest,
        # append it to the tables path, get that table.
        manifest = self.client.get(
            f"/api/materials/{SOURCE}/{IDENT}/extraction"
        ).json()
        for stub in manifest["tables"]:
            resp = self.client.get(self._url(stub["key"]))
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.json()["uid"], stub["uid"])

    def test_an_ordinal_is_not_a_table_key(self):
        # Addressing by ordinal must not work at all, rather than work until the
        # day an upstream revision shifts the numbering and starts serving a
        # different table under the same URL.
        self.assertEqual(
            self.client.get(self._url("1")).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_unknown_key_404s(self):
        self.assertEqual(
            self.client.get(self._url("p9999-t9")).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_malformed_key_does_not_resolve(self):
        self.assertEqual(
            self.client.get(self._url("abc")).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_one_materials_key_never_reaches_anothers_table(self):
        # Keys are per-extraction and collide across reports by construction
        # (every report has a p0018-t1), so the lookup MUST be scoped by material
        # — an unscoped filter would serve one report's table to all of them.
        other = _seed_material(ident="another_report")
        extraction = DocumentExtraction.objects.create(
            material=other,
            doc_id="ciaa-2075-76",
            dataset="damo-da/ciaa-annual-reports",
        )
        ExtractedTable.objects.create(
            extraction=extraction,
            ordinal=1,
            key="p0018-t1",  # the SAME key the 28th report uses
            uid="ciaa-2075-76#p0018#t1",
            page_no=18,
            markdown="OTHER REPORT TABLE",
        )
        body = self.client.get(self._url("p0018-t1")).json()
        self.assertNotIn("OTHER REPORT", body["markdown"])
        other_body = self.client.get(
            f"/api/materials/{SOURCE}/another_report/extraction/tables/p0018-t1"
        ).json()
        self.assertEqual(other_body["markdown"], "OTHER REPORT TABLE")


class ExtractionCascadeTests(APITestCase):
    databases = "__all__"

    def test_deleting_the_extraction_takes_tables_figures_and_points(self):
        # The ingest replaces an extraction by deleting it; if the cascade were
        # wrong, a re-ingest would accumulate orphans forever.
        material = _seed_material(ident="cascade_check")
        extraction = _seed_extraction(material)
        extraction.delete()
        self.assertEqual(ExtractedTable.objects.count(), 0)
        self.assertEqual(ExtractedFigure.objects.count(), 0)
        self.assertEqual(ExtractedFigurePoint.objects.count(), 0)

    def test_deleting_the_material_takes_the_extraction(self):
        material = _seed_material(ident="cascade_check_two")
        _seed_extraction(material)
        material.delete()
        self.assertEqual(DocumentExtraction.objects.count(), 0)
        self.assertEqual(ExtractedFigurePoint.objects.count(), 0)
