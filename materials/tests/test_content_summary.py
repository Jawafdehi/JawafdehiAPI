"""The content summary on the material detail payload.

``GET /api/materials/<source>/<ident>`` gains ``jawafdehi:content`` — how many
pages, tables, figures and points are inside, how much of the document was
actually read, and where the content came from.

It replaces the 404-probe the withdrawn ``/content`` route forced on a client
that only wanted to know whether a content tab exists. The cases that matter are
the ones that bite silently: the key is omitted (not zeroed) when we have not
looked inside, the stored document must not grow the derived key in memory, and
the ETag has to move when a re-ingest changes the counts — it is also the
If-Match token for a conditional PATCH.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_content_summary.py
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.test import APITestCase

from materials.models import DocumentContent, Material
from materials.tests.test_content_api import (
    IDENT,
    SOURCE,
    _seed_content,
    _seed_material,
)


class ContentSummaryTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        self.content = _seed_content(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}"

    def test_detail_reports_what_is_inside(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        summary = resp.json()["jawafdehi:content"]
        self.assertEqual(summary["pages"], 446)
        self.assertEqual(summary["tables"], 2)
        self.assertEqual(summary["figures"], 1)
        self.assertEqual(summary["points"], 2)

    def test_pages_read_is_reported_apart_from_pages(self):
        # A page we hold but could not transcribe is still a page of the
        # document. Collapsing the two would present an untranscribed report as
        # an empty one.
        summary = self.client.get(self.url).json()["jawafdehi:content"]
        self.assertEqual(summary["pages"], 446)
        self.assertEqual(summary["pages_read"], 1)

    def test_provenance_rides_with_the_counts(self):
        prov = self.client.get(self.url).json()["jawafdehi:content"]["provenance"]
        self.assertEqual(prov["doc_id"], "ciaa-2081-82")
        self.assertEqual(prov["dataset"], "damo-da/ciaa-annual-reports")
        self.assertEqual(prov["dataset_revision"], "c3c6c673")

    def test_the_counts_come_from_the_denormalised_columns(self):
        # Not from COUNT over the rows: this payload is the hottest read in the
        # archive. Prove it by making the columns disagree with the rows.
        DocumentContent.objects.filter(pk=self.material.iri).update(
            n_tables=99, n_figures=98, n_points=97
        )
        summary = self.client.get(self.url).json()["jawafdehi:content"]
        self.assertEqual(
            (summary["tables"], summary["figures"], summary["points"]), (99, 98, 97)
        )


class ContentSummaryAbsenceTests(APITestCase):
    databases = "__all__"

    def test_the_key_is_omitted_when_we_have_not_looked_inside(self):
        # Not zeros. Zeros would assert "we looked and found no tables", which is
        # false for ~346k of 346,341 materials — we have not looked.
        _seed_material(ident="not-extracted")
        body = self.client.get(f"/api/materials/{SOURCE}/not-extracted").json()
        self.assertNotIn("jawafdehi:content", body)

    def test_an_empty_extraction_still_reports_itself(self):
        # Here the zeros ARE true: we looked and there is nothing.
        material = _seed_material(ident="empty-content")
        DocumentContent.objects.create(
            material=material,
            doc_id="ciaa-0000-00",
            dataset="damo-da/ciaa-annual-reports",
        )
        body = self.client.get(f"/api/materials/{SOURCE}/empty-content").json()
        self.assertEqual(body["jawafdehi:content"]["tables"], 0)

    def test_the_stored_document_does_not_grow_the_derived_key(self):
        # row.data is what a later save would persist. The summary is derived and
        # must never leak into it.
        material = _seed_material(ident="stored-clean")
        _seed_content(material)
        self.client.get(f"/api/materials/{SOURCE}/stored-clean")
        stored = Material.objects.get(pk=material.iri).data
        self.assertNotIn("jawafdehi:content", stored)


class ContentSummaryValidatorTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        self.content = _seed_content(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}"

    def test_the_etag_covers_the_summary(self):
        # The counts ride on this payload but the ETag is derived from the
        # material's updated_at, which a content write does not touch by
        # itself. The ingest bumps it for exactly this reason; without that every
        # cache on the path would serve the old counts forever.
        before = self.client.get(self.url)["ETag"]
        DocumentContent.objects.filter(pk=self.material.iri).update(n_tables=99)
        Material.objects.filter(pk=self.material.iri).update(
            updated_at=self.material.updated_at.replace(year=2030)
        )
        after = self.client.get(self.url)["ETag"]
        self.assertNotEqual(before, after)

    def test_detail_still_varies_on_authorization(self):
        resp = self.client.get(self.url)
        self.assertIn("Authorization", resp["Vary"])
