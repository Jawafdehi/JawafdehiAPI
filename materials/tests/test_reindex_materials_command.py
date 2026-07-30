"""Tests for the ``reindex_materials`` bulk-reindex command.

Two invariants:

1. The queryset matches the live signal's gate — live (``is_deleted=False``) and
   ``LISTED``. A ``--rebuild`` drops the index first, so this gate is the ONLY thing
   standing between a rebuild and (a) resurrecting soft-deleted tombstones,
   (b) leaking UNLISTED/PRIVATE case evidence into anonymous search. The sibling
   ``reindex_entities`` shipped WITHOUT this gate and really did republish deleted
   entities in prod, so it is worth pinning here.
2. ``--since`` narrows to recently-touched rows, so a routine reconcile never needs
   a full rebuild.

(Previously in ``test_sync_materials.py``, alongside tests for the
``sync_materials_from_index`` command; that command read the frozen legacy
``ngm_v1`` and was removed, but these two invariants are about ``reindex_materials``
and outlive it.)
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from materials.models import Material, Visibility


class ReindexMaterialsSinceTests(TestCase):
    databases = "__all__"

    def _make(self, ident: str, **extra) -> Material:
        iri = f"https://jawafdehi.org/material/nkp/{ident}"
        return Material.objects.using("ngm").create(
            iri=iri, material_type="legal_corpus", source="nkp", ident=ident,
            data={"@type": "Legislation", "@id": iri, "name": {"ne": ident}},
            **extra,
        )

    def test_only_searchable_rows_are_reindexed(self):
        # A full rebuild must index the SAME set the live signal keeps: live +
        # LISTED. Soft-deleted (tombstoned) and non-LISTED (draft/in-review case
        # evidence) rows must NOT be re-added, else a rebuild resurrects/leaks them.
        live = self._make("live-act")
        self._make("gone-act", is_deleted=True)
        self._make("unlisted-act", visibility=Visibility.UNLISTED)
        self._make("private-act", visibility=Visibility.PRIVATE)
        with mock.patch(
            "materials.management.commands.reindex_materials.reindex",
            return_value={"indexed": 0, "skipped": 0},
        ) as m:
            call_command("reindex_materials", rebuild=True)
        iris = {r.iri for r in m.call_args.kwargs["records"]}
        self.assertEqual(iris, {live.iri})
        self.assertTrue(m.call_args.kwargs["rebuild"])

    def test_since_indexes_only_recently_touched(self):
        old = self._make("old-act")
        fresh = self._make("fresh-act")
        # Age `old` out of the window (update() bypasses auto_now).
        Material.objects.using("ngm").filter(iri=old.iri).update(
            updated_at=timezone.now() - datetime.timedelta(days=10)
        )
        cutoff = (timezone.now() - datetime.timedelta(days=1)).isoformat()
        with mock.patch(
            "materials.management.commands.reindex_materials.reindex",
            return_value={"indexed": 0, "skipped": 0},
        ) as m:
            call_command("reindex_materials", since=cutoff)
        records = list(m.call_args.kwargs["records"])
        iris = {r.iri for r in records}
        self.assertIn(fresh.iri, iris)
        self.assertNotIn(old.iri, iris)  # excluded by --since (no full rebuild)
        self.assertFalse(m.call_args.kwargs["rebuild"])
