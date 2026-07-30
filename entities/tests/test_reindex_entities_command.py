"""``reindex_entities`` must index ONLY rows still on the read plane.

DELETE on the NES entity plane is a SOFT delete (``is_deleted=True``; the row and
its version history survive), and ``entities.signals`` reacts by EVICTING the doc
from ``nes-entities``. A bulk reindex that streams ``.all()`` therefore resurrects
every tombstone — a deleted entity reappears in anonymous unified search. These
tests pin the gate, since the reindex now runs on a schedule and would otherwise
re-add tombstones on every run.

Run under the platform settings (DB-less: sqlite fallback) from the repo root::

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest entities/tests/test_reindex_entities_command.py
"""

from __future__ import annotations

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from entities.models import StoredEntity

IRI_BASE = "https://jawafdehi.org/entity"


def _seed(slug: str, *, is_deleted: bool = False) -> StoredEntity:
    """A minimal StoredEntity; signals are stubbed by the tests so no I/O happens."""
    iri = f"{IRI_BASE}/organization/{slug}"
    return StoredEntity.objects.create(
        iri=iri,
        entity_type="Organization",
        prefix="organization",
        slug=slug,
        data={"@id": iri, "@type": "Organization", "name": {"en": slug}},
        is_deleted=is_deleted,
    )


class ReindexEntitiesGateTests(TestCase):
    databases = "__all__"

    def setUp(self):
        # The post_save signal would otherwise attempt a live OpenSearch upsert.
        for target in ("entities.search_index.index", "entities.search_index.delete"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, **kwargs):
        """Run the command with the bulk driver stubbed; return the indexed IRIs."""
        sent: list[str] = []

        def fake_stream_bulk(client, index, docs):
            sent.extend(d["iri"] for d in docs)
            return len(docs)

        with (
            patch("jawafdehi_shared.search.reindex.make_client"),
            patch("jawafdehi_shared.search.reindex.create_index"),
            patch("jawafdehi_shared.search.reindex.stream_bulk", fake_stream_bulk),
        ):
            call_command("reindex_entities", **kwargs)
        return sent

    def test_soft_deleted_entities_are_not_reindexed(self):
        _seed("alpha-holdings")
        _seed("beta-traders")
        _seed("gamma-supplies", is_deleted=True)

        sent = self._run()

        assert sorted(sent) == [
            f"{IRI_BASE}/organization/alpha-holdings",
            f"{IRI_BASE}/organization/beta-traders",
        ]
        # The tombstone must never be resurrected into public search.
        assert f"{IRI_BASE}/organization/gamma-supplies" not in sent

    def test_rebuild_also_honours_the_gate(self):
        """--rebuild drops the index first, so the gate is what refills it."""
        _seed("alpha-holdings")
        _seed("gamma-supplies", is_deleted=True)

        sent = self._run(rebuild=True)

        assert sent == [f"{IRI_BASE}/organization/alpha-holdings"]

    def test_all_live_entities_are_indexed(self):
        """Guard against the gate over-filtering (e.g. an inverted flag)."""
        for slug in ("alpha-holdings", "beta-traders", "delta-works"):
            _seed(slug)

        assert len(self._run()) == 3
