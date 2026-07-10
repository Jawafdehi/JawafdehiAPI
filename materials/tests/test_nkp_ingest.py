"""Tests for NKP (Nepal Law Journal) precedent shaping + ingest.

Covers the ``precedent`` material type, the ``nkp_decision_to_jsonld`` shaper
(scraped decision dict → CreativeWork + jawafdehi:Precedent), and the
single-authoritative-source publish path (``--min-sources 1``: an official
government portal self-corroborates its own precedents). Run from the repo root:

    TESTING=true DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_nkp_ingest.py
"""

from __future__ import annotations

from django.test import TestCase

from jawafdehi_shared.entities.ids import is_valid_material_iri
from materials.bulk_ingest import MaterialBulkIngestService
from materials.jsonld import (
    MaterialType,
    nkp_decision_to_jsonld,
    nkp_precedent_material_iri,
    type_for,
    validate_material_jsonld,
)
from materials.management.commands.ingest_nkp_decisions import _record_from_decision
from materials.models import Material

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


class _NgmTestCase(TestCase):
    databases = "__all__"


class NkpShapeTests(_NgmTestCase):
    def test_precedent_type_maps_to_creativework(self):
        schema_type, additional = type_for(MaterialType.PRECEDENT)
        self.assertEqual(schema_type, "CreativeWork")
        self.assertEqual(additional, "jawafdehi:Precedent")

    def test_iri_is_valid_and_keyed_on_decision_no(self):
        doc = nkp_decision_to_jsonld(_DECISION)
        self.assertEqual(doc["@id"], "https://jawafdehi.org/material/nkp/11376")
        self.assertTrue(is_valid_material_iri(doc["@id"]))
        self.assertEqual(doc["@id"], nkp_precedent_material_iri("11376"))

    def test_shaped_doc_validates(self):
        doc = nkp_decision_to_jsonld(_DECISION)
        validate_material_jsonld(doc)  # raises on failure

    def test_metadata_and_text_carried(self):
        doc = nkp_decision_to_jsonld(_DECISION)
        self.assertEqual(doc["additionalType"], "jawafdehi:Precedent")
        self.assertEqual(doc["jawafdehi:caseNumber"], "079-RF-0016")
        self.assertEqual(doc["jawafdehi:journalVolume"], "67")
        self.assertEqual(doc["jawafdehi:judges"], _DECISION["judges"])
        self.assertIn("ne", doc["text"])
        self.assertIn("बैंकिङ", doc["text"]["ne"])

    def test_record_envelope_single_authoritative_source(self):
        rec = _record_from_decision(_DECISION)
        self.assertEqual(rec["material_type"], MaterialType.PRECEDENT)
        self.assertEqual(len(rec["sources"]), 1)
        self.assertEqual(rec["sources"][0]["authority"], "nkp.gov.np")

    def test_fallback_pdf_adds_second_source(self):
        rec = _record_from_decision(_DECISION_WITH_PDF)
        self.assertEqual(len(rec["sources"]), 2)
        self.assertEqual(rec["sources"][1]["authority"], "supremecourt.gov.np")


class NkpIngestTests(_NgmTestCase):
    def test_single_source_published_with_min_sources_1(self):
        service = MaterialBulkIngestService(min_sources=1)
        result = service.ingest([_record_from_decision(_DECISION)])
        self.assertEqual(result.written, 1)
        self.assertEqual(result.held, 0)
        mat = Material.objects.using("ngm").get(iri="https://jawafdehi.org/material/nkp/11376")
        self.assertEqual(mat.material_type, MaterialType.PRECEDENT)
        self.assertEqual(mat.source, "nkp")

    def test_single_source_held_with_default_gate(self):
        # Under the generic ≥2 gate, a lone-portal precedent HOLDs.
        service = MaterialBulkIngestService(min_sources=2)
        result = service.ingest([_record_from_decision(_DECISION)])
        self.assertEqual(result.written, 0)
        self.assertEqual(result.held, 1)

    def test_ingest_idempotent_by_decision_no(self):
        service = MaterialBulkIngestService(min_sources=1)
        service.ingest([_record_from_decision(_DECISION)])
        service.ingest([_record_from_decision(_DECISION)])
        self.assertEqual(
            Material.objects.using("ngm").filter(source="nkp").count(), 1
        )


class NkpIngestCommandTests(_NgmTestCase):
    """The ``ingest_nkp_decisions`` command's streaming/batched file path."""

    def _write_jsonl(self, decisions):
        import json
        import tempfile

        fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        for d in decisions:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        fh.close()
        return fh.name

    def test_command_batches_across_multiple_batches(self):
        from django.core.management import call_command

        # 5 distinct decisions, batch-size 2 → 3 batches; all should be written.
        decs = []
        for i in range(5):
            d = dict(_DECISION)
            d["decision_no"] = str(11000 + i)
            d["title"] = f"निर्णय नं. {11000 + i} - बैंकिङ कसुर"
            decs.append(d)
        path = self._write_jsonl(decs)
        call_command("ingest_nkp_decisions", path, "--min-sources", "1", "--batch-size", "2")
        self.assertEqual(Material.objects.using("ngm").filter(source="nkp").count(), 5)

    def test_command_skip_removed(self):
        from django.core.management import call_command

        keep = dict(_DECISION, decision_no="12000", title="निर्णय नं. 12000 - x", removed=False)
        drop = dict(_DECISION, decision_no="12001", title="निर्णय नं. 12001 - y", removed=True)
        path = self._write_jsonl([keep, drop])
        call_command("ingest_nkp_decisions", path, "--min-sources", "1", "--skip-removed")
        self.assertTrue(
            Material.objects.using("ngm").filter(iri="https://jawafdehi.org/material/nkp/12000").exists()
        )
        self.assertFalse(
            Material.objects.using("ngm").filter(iri="https://jawafdehi.org/material/nkp/12001").exists()
        )
