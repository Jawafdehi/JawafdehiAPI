"""Tests for NKP (Nepal Law Journal) precedent shaping + ingest.

Covers the ``precedent`` material type, the ``nkp_decision_to_jsonld`` shaper
(scraped decision dict → CreativeWork + jawafdehi:Precedent), the material_type
inference that keeps a bare precedent from flattening to ``document``, and the
API-plane ingest path (``POST /api/materials/``) the crawler drives. Run from the
repo root:

    TESTING=true DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_nkp_ingest.py
"""

from __future__ import annotations

from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from jawafdehi_shared.entities.ids import is_valid_material_iri
from materials.jsonld import (
    MaterialType,
    infer_material_type,
    type_for,
    validate_material_jsonld,
)
from materials.models import Material
from materials.sourcing.nkp.shaper import (
    nkp_decision_to_jsonld,
    nkp_precedent_material_iri,
)


# A representative scraped decision (the shape NkpDecisionItem emits).
_DECISION = {
    "detail_id": "10471",
    "source_url": "https://nkp.gov.np/full_detail/10471",
    "decision_no": "11376",
    "decision_no_bs": "११३७६",
    "title": "निर्णय नं. ११३७६ - बैंकिङ कसुर",
    "case_name": "बैंकिङ कसुर",
    "case_number": "079-RF-0016",
    "volume": "67",
    "year_bs": "2082",
    "month": "बैशाख",
    "issue": "1",
    "court": "सर्वोच्च अदालत",
    "bench": "पूर्ण इजलास",
    "judges": ["ईश्वरप्रसाद खतिवडा", "सुष्मालता माथेमा"],
    "decision_date_bs": "2080-03-24",
    "headnotes": [{"text": "कर्जा चुक्ता भए पनि …", "prakaran": "2"}],
    "full_text": "फैसला मिति : २०८०।३।२४\n\nमुद्दाः– बैंकिङ कसुर …",
    "referenced_laws": ["बैंकिङ कसुर तथा सजाय ऐन, 2064"],
    "view_count": 691,
    "removed": False,
}

# A decision whose HTML body was an upload-error note → carries a fallback PDF.
_DECISION_WITH_PDF = {
    "detail_id": "10475",
    "source_url": "https://nkp.gov.np/full_detail/10475",
    "decision_no": "11380",
    "title": "निर्णय नं. ११३८० - उत्प्रेषणसमेत",
    "year_bs": "2082",
    "month": "बैशाख",
    "issue": "1",
    "full_text": "नोट : अपलोडका क्रममा … हेर्नुहुन अनुरोध गरिन्छ ।",
    "fallback_pdf_url": "https://supremecourt.gov.np/publication/materials/102484.pdf",
    "removed": False,
}


class _NgmTestCase(APITestCase):
    databases = "__all__"


class NkpShapeTests(_NgmTestCase):
    def test_precedent_type_maps_to_creativework(self):
        schema_type, additional = type_for(MaterialType.PRECEDENT)
        self.assertEqual(schema_type, "CreativeWork")
        self.assertEqual(additional, "jawafdehi:Precedent")

    def test_shaper_returns_doc_and_material_type(self):
        doc, material_type = nkp_decision_to_jsonld(_DECISION)
        self.assertEqual(material_type, MaterialType.PRECEDENT)
        self.assertIsInstance(doc, dict)

    def test_iri_is_valid_and_keyed_on_decision_no(self):
        doc, _ = nkp_decision_to_jsonld(_DECISION)
        self.assertEqual(doc["@id"], "https://jawafdehi.org/material/nkp/11376")
        self.assertTrue(is_valid_material_iri(doc["@id"]))
        self.assertEqual(doc["@id"], nkp_precedent_material_iri("11376"))

    def test_shaped_doc_validates(self):
        doc, _ = nkp_decision_to_jsonld(_DECISION)
        validate_material_jsonld(doc)  # raises on failure

    def test_metadata_and_text_carried(self):
        doc, _ = nkp_decision_to_jsonld(_DECISION)
        self.assertEqual(doc["additionalType"], "jawafdehi:Precedent")
        self.assertEqual(doc["jawafdehi:caseNumber"], "079-RF-0016")
        self.assertEqual(doc["jawafdehi:journalVolume"], "67")
        self.assertEqual(doc["jawafdehi:judges"], _DECISION["judges"])
        self.assertIn("ne", doc["text"])
        self.assertIn("बैंकिङ", doc["text"]["ne"])

    def test_fallback_pdf_rides_as_associated_media(self):
        # When routed through the single-material API (which stores the doc
        # verbatim), the recoverable scanned PDF must survive on the doc itself.
        doc, _ = nkp_decision_to_jsonld(_DECISION_WITH_PDF)
        media = doc.get("associatedMedia") or []
        self.assertEqual(len(media), 1)
        self.assertEqual(
            media[0]["contentUrl"],
            "https://supremecourt.gov.np/publication/materials/102484.pdf",
        )
        self.assertEqual(media[0]["jawafdehi:linkRole"], "ALTERNATE")

    def test_no_fallback_no_associated_media(self):
        doc, _ = nkp_decision_to_jsonld(_DECISION)
        self.assertNotIn("associatedMedia", doc)


class NkpDateShapingTests(_NgmTestCase):
    """Precedent dates must land in the keys the search indexer reads."""

    def test_shaper_emits_search_readable_dates(self):
        doc, _ = nkp_decision_to_jsonld(_DECISION)
        # build_doc reads jawafdehi:publicationDateBS for date_bs and datePublished
        # for the Gregorian date — both must be present, not only decisionDateBS.
        self.assertEqual(doc["jawafdehi:publicationDateBS"], "2080-03-24")
        self.assertEqual(doc["jawafdehi:decisionDateBS"], "2080-03-24")
        self.assertEqual(doc["datePublished"], "2023-07-09")  # BS 2080-03-24 → AD

    def test_bad_bs_date_does_not_break_shaping(self):
        d = dict(_DECISION, decision_date_bs="not-a-date")
        doc, _ = nkp_decision_to_jsonld(d)
        self.assertEqual(doc["jawafdehi:publicationDateBS"], "not-a-date")
        self.assertNotIn("datePublished", doc)  # unparseable → no Gregorian date


class InferMaterialTypeTests(_NgmTestCase):
    """A bare precedent doc (no explicit material_type) must not flatten to document."""

    def test_additionaltype_wins_over_bare_creativework(self):
        doc, _ = nkp_decision_to_jsonld(_DECISION)  # CreativeWork + jawafdehi:Precedent
        self.assertEqual(infer_material_type(doc), MaterialType.PRECEDENT)

    def test_court_case_additionaltype_inferred(self):
        doc = {"@type": "CreativeWork", "additionalType": "jawafdehi:CourtCase"}
        self.assertEqual(infer_material_type(doc), MaterialType.COURT_CASE)

    def test_plain_creativework_still_document(self):
        self.assertEqual(infer_material_type({"@type": "CreativeWork"}), MaterialType.DOCUMENT)


class NkpApiIngestTests(_NgmTestCase):
    """Precedents source through the material API plane (POST /api/materials/)."""

    _IRI = "https://jawafdehi.org/material/nkp/11376"

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        g, _ = Group.objects.get_or_create(name="Caseworker")  # an NGM-capable role
        cls.writer = User.objects.create(username="oidc-nkp-writer")
        cls.writer.groups.add(g)
        cls.norole = User.objects.create(username="oidc-nkp-norole")

    def _body(self):
        doc, material_type = nkp_decision_to_jsonld(_DECISION)
        return {"material": doc, "material_type": material_type}

    def test_post_precedent_creates_material(self):
        self.client.force_authenticate(user=self.writer)
        resp = self.client.post("/api/materials/", self._body(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, getattr(resp, "data", None))
        row = Material.objects.using("ngm").get(iri=self._IRI)
        self.assertEqual(row.material_type, MaterialType.PRECEDENT)
        self.assertEqual(row.source, "nkp")

    def test_repost_is_idempotent_upsert(self):
        self.client.force_authenticate(user=self.writer)
        self.client.post("/api/materials/", self._body(), format="json")
        resp = self.client.post("/api/materials/", self._body(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)  # updated, not duplicated
        self.assertEqual(Material.objects.using("ngm").filter(source="nkp").count(), 1)

    def test_repost_revives_soft_deleted(self):
        # Platform policy: an incoming write is the source of truth, so re-posting
        # a soft-deleted @id republishes it (is_deleted cleared, data overwritten).
        self.client.force_authenticate(user=self.writer)
        self.client.post("/api/materials/", self._body(), format="json")
        Material.objects.using("ngm").filter(iri=self._IRI).update(
            is_deleted=True, data={"tomb": 1}
        )
        resp = self.client.post("/api/materials/", self._body(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row = Material.objects.using("ngm").get(iri=self._IRI)
        self.assertFalse(row.is_deleted)
        self.assertNotEqual(row.data, {"tomb": 1})  # real doc, not the tombstone

    def test_index_doc_carries_dates(self):
        from materials.search_index import build_doc

        self.client.force_authenticate(user=self.writer)
        self.client.post("/api/materials/", self._body(), format="json")
        row = Material.objects.using("ngm").get(iri=self._IRI)
        idx = build_doc(row)
        self.assertEqual(idx["date"], "2023-07-09")
        self.assertEqual(idx["date_bs"], "2080-03-24")

    def test_write_requires_ngm_role(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.post("/api/materials/", self._body(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class NkpCrawlerClientTests(_NgmTestCase):
    """The crawler posts through MaterialApiClient (unless --dry-run)."""

    def _crawler(self, **overrides):
        from argparse import Namespace

        from materials.sourcing.nkp.crawl import NkpCrawler

        import tempfile
        cache = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
        args = Namespace(
            cache=cache, api_base=None, token=None, dry_run=True, from_cache=False,
            year=None, year_min=None, year_max=None, delay=0.0, transport="requests",
            headful=False, max_decisions=0,
        )
        for k, v in overrides.items():
            setattr(args, k, v)
        return NkpCrawler(args)

    def test_dry_run_posts_nothing(self):
        c = self._crawler(dry_run=True)
        self.assertIsNone(c.api)
        self.assertTrue(c._post_decision(_DECISION))  # no-op success
        self.assertEqual(c.posted_count, 0)

    def test_post_decision_shapes_and_calls_client(self):
        c = self._crawler(dry_run=True)
        sent = {}

        class _FakeApi:
            def post(self, doc, material_type):
                sent["doc"] = doc
                sent["material_type"] = material_type

            def close(self):
                pass

        c.api = _FakeApi()
        self.assertTrue(c._post_decision(_DECISION))
        self.assertEqual(sent["material_type"], "precedent")
        self.assertEqual(sent["doc"]["@id"], "https://jawafdehi.org/material/nkp/11376")
        self.assertEqual(c.posted_count, 1)

    def test_api_error_leaves_id_for_retry(self):
        from materials.sourcing.nkp.crawl import MaterialApiError

        c = self._crawler(dry_run=True)

        class _FailApi:
            def post(self, doc, material_type):
                raise MaterialApiError("503: down")

            def close(self):
                pass

        c.api = _FailApi()
        self.assertFalse(c._post_decision(_DECISION))  # False → not checkpointed
        self.assertEqual(c.posted_count, 0)
