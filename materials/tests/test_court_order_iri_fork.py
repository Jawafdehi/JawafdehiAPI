"""Characterization test for the F5 court-order IRI hyphen/underscore fork.

Two minters disagree on the ident for the same underlying court order:

* ``court_order_material_iri`` PRESERVES hyphens (the ident grammar ``[a-z0-9._-]``
  allows them) — this is the canonical court_order Material ``@id``.
* the generic ``manuscript_jsonld`` shaper UNDERSCORES the ident.

**Scope note (2026-07-30).** F5 was originally a *duplicate Material row* risk: the
``sync_materials_from_index`` command routed a COURT_ORDER row whose ``document_id``
lacked the literal ``court-order`` marker through the generic manuscript shaper, so
the same order could land as two Material rows under two IRIs. That command read the
frozen legacy ``ngm_v1`` and has been removed, and ``manuscript_jsonld`` no longer
feeds any Material-row writer — its only remaining consumer is the R2 published-index
export (``lakehouse.index_publish``). So the duplicate-row vector is CLOSED; what
survives is a shape inconsistency in published linked data.

This test keeps the divergence VISIBLE (the threat model's original reason for
pinning it) without depending on the deleted command. If someone unifies the two
minters, this assertion flips and forces a conscious, reviewed update — plus a
re-key of anything already published under the underscored form.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from materials.jsonld import court_order_material_iri, manuscript_jsonld


class CourtOrderIriForkTests(SimpleTestCase):
    def test_canonical_and_manuscript_minters_still_diverge(self):
        # A COURT_ORDER-ish document_id WITHOUT the ``court-order`` marker: the
        # generic manuscript path is what shapes it.
        marker_less_id = "ngm:supreme:082-OA-0503"

        canonical = court_order_material_iri("supreme", "082-OA-0503")
        shaped = manuscript_jsonld(
            {"document_id": marker_less_id, "source_type": "COURT_ORDER", "links": []}
        )

        self.assertEqual(
            canonical, "https://jawafdehi.org/material/court_order/supreme.082-oa-0503"
        )
        self.assertEqual(
            shaped["@id"], "https://jawafdehi.org/material/supreme/082_oa_0503"
        )
        self.assertNotEqual(
            canonical,
            shaped["@id"],
            "F5 fork resolved? Update this assertion and re-key published rows.",
        )

    def test_canonical_minter_preserves_hyphens(self):
        """The half of the fork that Material rows are actually keyed on."""
        self.assertEqual(
            court_order_material_iri("special", "069-CR-0003"),
            "https://jawafdehi.org/material/court_order/special.069-cr-0003",
        )
