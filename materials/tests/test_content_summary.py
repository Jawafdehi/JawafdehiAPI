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

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
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
        _seed_material(ident="no-content")
        body = self.client.get(f"/api/materials/{SOURCE}/no-content").json()
        self.assertNotIn("jawafdehi:content", body)

    def test_empty_content_still_reports_itself(self):
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


class ContentSummaryOnTheListingTests(APITestCase):
    """The listing carries ``jawafdehi:content`` too, and must.

    On this plane the key's ABSENCE is a claim — *there is nothing inside this
    document*. A listing that never carried it would make that claim about all
    346k materials, including the few dozen it is false for, and a client
    rendering "has content" badges off a list page would show none of them.

    So this is not shape-consistency for its own sake: a key that means one
    thing on the detail route and nothing on the listing is worse than no key,
    because the second reading is indistinguishable from the first.
    """

    databases = "__all__"

    def setUp(self):
        self.with_content = _seed_material(ident="listed-with-content")
        _seed_content(self.with_content)
        self.without = _seed_material(ident="listed-without-content")

    def _rows(self):
        body = self.client.get("/api/materials/").json()
        return {r["@id"].rsplit("/", 1)[-1]: r for r in body["results"]}

    def test_a_listed_row_carries_its_summary(self):
        rows = self._rows()
        self.assertEqual(
            rows["listed-with-content"]["jawafdehi:content"]["tables"], 2
        )

    def test_a_row_with_nothing_inside_omits_it(self):
        # The same contract as the detail route: omitted, never zeros.
        self.assertNotIn(
            "jawafdehi:content", self._rows()["listed-without-content"]
        )

    def test_the_listing_agrees_with_the_detail_read(self):
        # The two must not disagree about the same document — a client that
        # drills from a list row into the detail page would see the count change
        # under it, and have no way to know which one to believe.
        listed = self._rows()["listed-with-content"]["jawafdehi:content"]
        detail = self.client.get(
            f"/api/materials/{SOURCE}/listed-with-content"
        ).json()["jawafdehi:content"]
        self.assertEqual(listed, detail)

    def test_the_listing_does_not_pay_a_query_per_row(self):
        # select_related on the listing queryset. Without it the summary costs
        # one SELECT per row, which is the shape that has bitten this plane
        # before — and it stays invisible until the page is big.
        for n in range(6):
            material = _seed_material(ident=f"listed-bulk-{n}")
            _seed_content(material)
        # using="ngm": materials and their content both live on that alias, and
        # a bare assertNumQueries would watch "default" and count nothing.
        with self.assertNumQueries(1, using="ngm"):
            # One SELECT for the page, with content LEFT JOINed in — not one per
            # row. Eight materials here, seven of them with content.
            self.client.get("/api/materials/")


class ContentSummaryIsNotWritableTests(APITestCase):
    """``jawafdehi:content`` is derived on read and must never reach ``data``.

    A read-modify-write — GET a document, change a title, PUT it back — is the
    ordinary way this plane is used, so a derived key that survives it would be
    persisted by a caller doing nothing wrong. Once stored it outlives the rows
    it describes: the search indexer copies ``data`` into ``raw``, and a document
    whose content was later removed goes on advertising a content tab whose
    ``/content`` route 404s.

    The derived copy and a stored one are indistinguishable in a response, which
    is what makes this worth a gate rather than a convention.
    """

    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        _seed_content(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}"
        user = get_user_model().objects.create_user("cw-content", password="x")
        user.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
        self.client.force_authenticate(user)

    def test_a_read_modify_write_round_trip_does_not_persist_it(self):
        body = self.client.get(self.url).json()
        self.assertIn("jawafdehi:content", body)
        resp = self.client.put(self.url, body, format="json")
        self.assertIn(resp.status_code, (status.HTTP_200_OK, status.HTTP_201_CREATED))
        stored = Material.objects.get(pk=self.material.iri).data
        self.assertNotIn("jawafdehi:content", stored)

    def test_a_patch_cannot_inject_it(self):
        # Rejected outright rather than stripped: a patch has no "strip"
        # semantics, so dropping the op silently would report success for a write
        # that did not happen.
        resp = self.client.patch(
            self.url,
            {"patch_ops": [{"op": "add", "path": "/jawafdehi:content", "value": {}}]},
            format="json",
        )
        # 422, the same answer /@id and a malformed op already get.
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        stored = Material.objects.get(pk=self.material.iri).data
        self.assertNotIn("jawafdehi:content", stored)


class ContentSummaryValidatorTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed_material()
        self.content = _seed_content(self.material)
        self.url = f"/api/materials/{SOURCE}/{IDENT}"

    def test_a_content_write_alone_does_not_move_the_etag(self):
        # This is the hazard, stated as a test rather than a comment: the counts
        # ride on this payload, but the ETag comes from the material's updated_at
        # and a content write does not touch that column. So the body can change
        # while the validator does not.
        #
        # Which is why the INGEST bumps updated_at — pinned on the ingest side by
        # test_the_material_is_touched_so_its_etag_moves. Asserting the bump here
        # would mean performing it here, and a test that performs the thing it is
        # checking passes with the production code deleted.
        before = self.client.get(self.url)["ETag"]
        DocumentContent.objects.filter(pk=self.material.iri).update(n_tables=99)
        after = self.client.get(self.url)
        self.assertEqual(after.json()["jawafdehi:content"]["tables"], 99)
        self.assertEqual(after["ETag"], before)

    def test_touching_the_material_moves_the_etag(self):
        before = self.client.get(self.url)["ETag"]
        Material.objects.filter(pk=self.material.iri).update(
            updated_at=self.material.updated_at.replace(year=2030)
        )
        self.assertNotEqual(self.client.get(self.url)["ETag"], before)

    def test_detail_still_varies_on_authorization(self):
        resp = self.client.get(self.url)
        self.assertIn("Authorization", resp["Vary"])
