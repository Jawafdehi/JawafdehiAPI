"""Tests for single-source Material ingest + standalone court-order materialization.

Covers the gate-BYPASS write path (court orders are inherently single-publisher,
so the bulk ≥2-source HOLD gate must not apply) and the documents-only shape:
each order becomes its OWN ``court_order`` Material that ``isPartOf`` the case's
canonical ``/courtcase/<court>/<num>`` IRI. NO ``court_case`` shadow Material is
minted — case identity + metadata live in the courtcase read plane. Run from the
repo root:

    TESTING=true DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_single_source_ingest.py
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.test import TestCase

from courts.importer import CourtCaseImporter, ImportConfig, ImportMode
from jawafdehi_shared.entities.ids import build_courtcase_iri
from materials.jsonld import (
    MaterialType,
    court_order_material_iri,
    court_order_to_jsonld,
)
from materials.models import Material
from materials.single_source_ingest import upsert_single_source_material

_ORDER_SRC = {
    "document_id": "ngm:court-order:supreme:081-CR-0081",
    "url": [
        {"link": "https://r2/raw.pdf", "role": "RAW"},
        {"link": "https://perma/x", "role": "PERMALINK"},
    ],
}


class _NgmTestCase(TestCase):
    databases = "__all__"


class SingleSourceIngestTests(_NgmTestCase):
    def _doc(self):
        return court_order_to_jsonld(
            _ORDER_SRC, court_identifier="supreme", case_number="081-CR-0081"
        )

    def test_single_source_doc_is_written_not_held(self):
        mat = upsert_single_source_material(self._doc(), material_type=MaterialType.COURT_ORDER)
        self.assertEqual(mat.material_type, MaterialType.COURT_ORDER)
        self.assertTrue(Material.objects.using("ngm").filter(iri=mat.iri).exists())

    def test_upsert_idempotent_by_id(self):
        upsert_single_source_material(self._doc(), material_type=MaterialType.COURT_ORDER)
        upsert_single_source_material(self._doc(), material_type=MaterialType.COURT_ORDER)
        self.assertEqual(Material.objects.using("ngm").filter(source="court_order").count(), 1)

    def test_full_clean_rejects_malformed_id(self):
        bad = {"@type": "Manuscript", "@id": "not-a-material-iri", "name": {"ne": "x"}}
        with self.assertRaises(Exception):
            upsert_single_source_material(bad, material_type=MaterialType.COURT_ORDER)


class CourtOrderShapeTests(_NgmTestCase):
    def test_order_iri_and_type(self):
        doc = court_order_to_jsonld(
            _ORDER_SRC, court_identifier="supreme", case_number="081-CR-0081"
        )
        self.assertEqual(
            doc["@id"], "https://jawafdehi.org/material/court_order/supreme.081-cr-0081"
        )
        self.assertEqual(doc["@type"], ["Manuscript", "DigitalDocument"])
        self.assertEqual(len(doc["associatedMedia"]), 2)
        # isPartOf the case's canonical courtcase IRI (NOT a court_case shadow
        # Material); name is the human case-order title (raw document_id rides on
        # identifier only).
        self.assertEqual(
            doc["isPartOf"]["@id"], build_courtcase_iri("supreme", "081-CR-0081")
        )
        self.assertEqual(doc["name"], {"ne": "081-CR-0081 आदेश"})
        self.assertEqual(doc["identifier"], "ngm:court-order:supreme:081-CR-0081")

    def test_iri_is_stable_and_derivable_for_dedup(self):
        # cross-ref spec 06: the same (court, case_number) yields the SAME order IRI
        # regardless of input casing — so a case-cited order and a scraped order
        # converge on one Material.
        a = court_order_material_iri("supreme", "081-CR-0081")
        b = court_order_material_iri("Supreme", "081-cr-0081")
        self.assertEqual(a, b)

    def test_multiple_orders_get_stable_n_suffix(self):
        first = court_order_material_iri("supreme", "081-CR-0081", 1)
        second = court_order_material_iri("supreme", "081-CR-0081", 2)
        self.assertTrue(first.endswith("supreme.081-cr-0081.1"))
        self.assertTrue(second.endswith("supreme.081-cr-0081.2"))


class MaterializeOrdersViaImporterTests(_NgmTestCase):
    def _row(self):
        return {
            "court_identifier": "supreme",
            "court_type": "supreme",
            "court_full_name_nepali": "सर्वोच्च",
            "court_full_name_english": "Supreme Court",
            "case_number": "081-CR-0081",
            "registration_date_bs": "2081-01-01",
            "registration_date_ad": date(2024, 4, 13),
            "case_type": "भ्रष्टाचार",
            "case_status": "चालु",
            "plaintiff": "वादी",
            "defendant": "प्रतिवादी",
            "document_sources": [_ORDER_SRC],
            "hearings": [],
            "entities": [],
        }

    def _run(self, **over):
        # Annotated for the same reason as courts/tests/test_import_courtcases.py
        # ::_copy — an unannotated heterogeneous kwargs dict makes every splatted
        # field the union of all its value types.
        cfg: dict[str, Any] = dict(
            mode=ImportMode.COPY, courts=["supreme"], source_rows=[self._row()],
            materialize_orders=True, batch_size=10,
        )
        cfg.update(over)
        return CourtCaseImporter(ImportConfig(**cfg)).run()

    def test_order_is_standalone_material_and_no_case_shadow(self):
        res = self._run()
        self.assertEqual(res.orders_materialized, 1)
        order_iri = court_order_material_iri("supreme", "081-CR-0081")
        order = Material.objects.using("ngm").get(iri=order_iri)
        self.assertEqual(order.data["@type"], ["Manuscript", "DigitalDocument"])
        self.assertEqual(len(order.data["associatedMedia"]), 2)
        # Documents-only: the order isPartOf the case's canonical /courtcase IRI,
        # and NO court_case shadow Material is minted.
        self.assertEqual(
            order.data["isPartOf"]["@id"],
            build_courtcase_iri("supreme", "081-CR-0081"),
        )
        self.assertFalse(
            Material.objects.using("ngm").filter(source="court").exists()
        )

    def test_rerun_is_idempotent(self):
        self._run()
        res2 = self._run(allow_nonempty_target=True)
        self.assertEqual(res2.failed, 0)  # the re-upsert must not error (created_at)
        self.assertEqual(
            Material.objects.using("ngm").filter(source="court_order").count(), 1
        )
        # No court_case shadow Material is minted (case identity lives in the
        # courtcase read plane, /courtcase/<court>/<num>).
        self.assertEqual(
            Material.objects.using("ngm").filter(source="court").count(), 0
        )

    def test_dry_run_materializes_nothing(self):
        res = self._run(dry_run=True)
        self.assertEqual(res.orders_materialized, 1)  # counted (shaped + validated)
        self.assertEqual(Material.objects.using("ngm").count(), 0)  # but not written
