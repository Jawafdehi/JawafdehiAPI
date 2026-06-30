"""Tests for the materials sync reconciliation + incremental reindex.

Covers (1) court orders synced from the legacy doc-index reconcile to the SAME
canonical Material as the importer's ``--materialize-orders`` (one row, no
hyphen/underscore IRI fork), and (2) ``reindex_materials --since`` indexes only
recently-touched materials (so the sync cron never needs a full rebuild).
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from ngm_service.materials.jsonld import (
    court_order_material_iri,
    court_order_to_jsonld,
)
from ngm_service.materials.management.commands.sync_materials_from_index import (
    parse_court_order_id,
)
from ngm_service.materials.models import Material


class CourtOrderReconciliationTests(SimpleTestCase):
    def test_parse_court_order_id(self):
        self.assertEqual(
            parse_court_order_id("ngm:court-order:supreme:081-WH-0079"),
            ("supreme", "081-WH-0079"),
        )
        # non-court-order ids → None (handled by the generic manuscript path)
        self.assertIsNone(parse_court_order_id("ngm:ciaa-press-release:42"))
        self.assertIsNone(parse_court_order_id("garbage"))
        self.assertIsNone(parse_court_order_id("ngm:court-order:supreme"))  # no case#

    def test_sync_court_order_iri_and_shape_match_importer(self):
        # The reconciliation invariant: the sync path (doc-index document_id) and
        # the importer path (court, case_number) mint the IDENTICAL court_order
        # Material @id + @type — so they upsert ONE row, not two.
        court, case_number = parse_court_order_id("ngm:court-order:special:069-CR-0003")
        doc = court_order_to_jsonld(
            {"document_id": "ngm:court-order:special:069-CR-0003", "url": []},
            court_identifier=court, case_number=case_number,
        )
        self.assertEqual(doc["@id"], court_order_material_iri("special", "069-CR-0003"))
        self.assertEqual(
            doc["@id"], "https://jawafdehi.org/material/court_order/special.069-cr-0003"
        )
        self.assertEqual(doc["@type"], ["Manuscript", "DigitalDocument"])


class ReindexMaterialsSinceTests(TestCase):
    databases = "__all__"

    def _make(self, ident: str) -> Material:
        iri = f"https://jawafdehi.org/material/nkp/{ident}"
        return Material.objects.using("ngm").create(
            iri=iri, material_type="legal_corpus", source="nkp", ident=ident,
            data={"@type": "Legislation", "@id": iri, "name": {"ne": ident}},
        )

    def test_since_indexes_only_recently_touched(self):
        old = self._make("old-act")
        fresh = self._make("fresh-act")
        # Age `old` out of the window (update() bypasses auto_now).
        Material.objects.using("ngm").filter(iri=old.iri).update(
            updated_at=timezone.now() - datetime.timedelta(days=10)
        )
        cutoff = (timezone.now() - datetime.timedelta(days=1)).isoformat()
        with mock.patch(
            "jawafdehi_shared.search.reindex.reindex",
            return_value={"indexed": 0, "skipped": 0},
        ) as m:
            call_command("reindex_materials", since=cutoff)
        records = list(m.call_args.kwargs["records"])
        iris = {r.iri for r in records}
        self.assertIn(fresh.iri, iris)
        self.assertNotIn(old.iri, iris)  # excluded by --since (no full rebuild)
        self.assertFalse(m.call_args.kwargs["rebuild"])
