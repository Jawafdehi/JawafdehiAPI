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

import pytest

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


class ReindexEntitiesCaseCountTests(TestCase):
    """``case_count`` is promoted from ONE bulk map, and a cases-DB failure aborts.

    Resolving the citation count per document would issue a query per entity
    across ~187k rows, so the command loads a single grouped map. And it must
    never fall back to an empty map: that indexes the whole corpus as uncited,
    which archives every entity out of public search in one alias swap.
    """

    databases = "__all__"

    def setUp(self):
        for target in ("entities.search_index.index", "entities.search_index.delete"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, **kwargs):
        """Run the command; return ``{iri: case_count}`` for the docs it built."""
        sent: dict[str, int] = {}

        def fake_stream_bulk(client, index, docs):
            docs = list(docs)
            for doc in docs:
                sent[doc["iri"]] = doc["case_count"]
            return len(docs)

        with (
            patch("jawafdehi_shared.search.reindex.make_client"),
            patch("jawafdehi_shared.search.reindex.create_index"),
            patch("jawafdehi_shared.search.reindex.stream_bulk", fake_stream_bulk),
        ):
            call_command("reindex_entities", **kwargs)
        return sent

    def test_every_doc_carries_its_citation_count(self):
        from cases.models import (
            Case,
            CaseEntityRelationship,
            CaseState,
            CaseType,
            RelationshipType,
        )

        cited = _seed("alpha-holdings")
        _seed("beta-traders")
        with patch("cases.search_index.index"):
            case = Case.objects.create(
                title="Reindex count test",
                offence_type=CaseType.CORRUPTION,
                state=CaseState.PUBLISHED,
            )
            CaseEntityRelationship.objects.create(
                case=case,
                nes_id=cited.iri,
                relationship_type=RelationshipType.ACCUSED,
            )

        sent = self._run()
        assert sent[cited.iri] == 1
        assert sent[f"{IRI_BASE}/organization/beta-traders"] == 0

    def test_catchup_re_reads_the_count_of_an_entity_cited_mid_rebuild(self):
        """The catchup detecting a mid-build citation is only half the job.

        ``build`` closes over the counts map loaded BEFORE the build started, so
        re-yielding the entity without re-reading its count rewrites it into the
        new generation still carrying 0 — hidden until the next reconcile, which
        is the exact outcome catchup source 2 exists to prevent.
        """
        from cases.models import (
            Case,
            CaseEntityRelationship,
            CaseState,
            CaseType,
            RelationshipType,
        )

        cited = _seed("alpha-holdings")
        sent: dict[str, int] = {}
        calls = {"n": 0}

        def fake_stream_bulk(client, index, docs):
            docs = list(docs)
            for doc in docs:
                sent[doc["iri"]] = doc["case_count"]
            calls["n"] += 1
            if calls["n"] == 1:
                # A case is published while the main pass is streaming.
                case = Case.objects.create(
                    title="Published mid-rebuild",
                    offence_type=CaseType.CORRUPTION,
                    state=CaseState.PUBLISHED,
                )
                CaseEntityRelationship.objects.create(
                    case=case,
                    nes_id=cited.iri,
                    relationship_type=RelationshipType.ACCUSED,
                )
            return len(docs)

        with (
            patch("entities.search_index.index_by_iri"),
            patch("cases.search_index.index"),
            patch("jawafdehi_shared.search.reindex.make_client"),
            patch("jawafdehi_shared.search.reindex.create_index"),
            patch("jawafdehi_shared.search.reindex.stream_bulk", fake_stream_bulk),
        ):
            call_command("reindex_entities", rebuild=True)

        # The main pass wrote 0, correctly — the bind did not exist yet. The
        # catchup must overwrite it with 1, or the entity stays invisible.
        assert sent[cited.iri] == 1

    def test_a_cases_db_failure_aborts_rather_than_archiving_everything(self):
        from entities.search_visibility import ReferenceLookupError

        _seed("alpha-holdings")
        with patch(
            "entities.management.commands.reindex_entities.case_counts",
            side_effect=ReferenceLookupError("cases DB unreachable"),
        ):
            with pytest.raises(ReferenceLookupError):
                self._run()


class ReindexEntitiesGeographyTests(TestCase):
    """``district``/``province`` ride the same bulk-map machinery as the count.

    Same three obligations, for the same reasons: one grouped load rather than a
    query per document, a catchup that RE-READS rather than re-writing the
    pre-build answer, and an abort rather than a silent empty map — which here
    would strip the facet off every document in one alias swap instead of
    archiving them.
    """

    databases = "__all__"

    def setUp(self):
        for target in ("entities.search_index.index", "entities.search_index.delete"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        from django.core.cache import cache

        from entities.search_geography import clear_cache

        cache.clear()
        clear_cache()
        self.district = StoredEntity.objects.create(
            iri=f"{IRI_BASE}/location/district/kathmandu-np0327",
            entity_type="AdministrativeArea",
            prefix="location/district",
            slug="kathmandu-np0327",
            data={
                "@id": f"{IRI_BASE}/location/district/kathmandu-np0327",
                "@type": "AdministrativeArea",
                "identifier": [{"propertyID": "ocha-pcode", "value": "NP0327"}],
            },
        )
        clear_cache()

    def _run(self, **kwargs):
        """Run the command; return ``{iri: (district, province)}`` per doc."""
        sent: dict[str, tuple] = {}

        def fake_stream_bulk(client, index, docs):
            docs = list(docs)
            for doc in docs:
                sent[doc["iri"]] = (doc.get("district"), doc.get("province"))
            return len(docs)

        with (
            patch("jawafdehi_shared.search.reindex.make_client"),
            patch("jawafdehi_shared.search.reindex.create_index"),
            patch("jawafdehi_shared.search.reindex.stream_bulk", fake_stream_bulk),
        ):
            call_command("reindex_entities", **kwargs)
        return sent

    def _publish(self, *, entity_iri):
        from cases.models import (
            Case,
            CaseEntityRelationship,
            CaseState,
            CaseType,
            RelationshipType,
        )

        case = Case.objects.create(
            title="Reindex geography test",
            offence_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
        )
        CaseEntityRelationship.objects.create(
            case=case, nes_id=entity_iri, relationship_type=RelationshipType.ACCUSED
        )
        CaseEntityRelationship.objects.create(
            case=case,
            nes_id=self.district.iri,
            relationship_type=RelationshipType.LOCATION,
        )
        return case

    def test_every_doc_carries_the_geography_of_its_citing_cases(self):
        cited = _seed("alpha-holdings")
        _seed("beta-traders")
        with patch("cases.search_index.index"), patch(
            "entities.search_index.index_by_iri"
        ):
            self._publish(entity_iri=cited.iri)

        sent = self._run()
        assert sent[cited.iri] == (["Kathmandu"], ["Bagmati"])
        # An uncited entity carries NO field, not an empty list.
        assert sent[f"{IRI_BASE}/organization/beta-traders"] == (None, None)

    def test_catchup_re_reads_the_geography_of_a_case_published_mid_rebuild(self):
        """The same closure trap the citation count fell into.

        ``build`` closes over the geography map loaded BEFORE the build started,
        so re-yielding a mid-build entity without re-reading writes it into the
        new generation with no district — and the facet would be wrong until the
        next reindex, which is precisely what the catchup exists to prevent.
        """
        cited = _seed("alpha-holdings")
        sent: dict[str, tuple] = {}
        calls = {"n": 0}

        def fake_stream_bulk(client, index, docs):
            docs = list(docs)
            for doc in docs:
                sent[doc["iri"]] = (doc.get("district"), doc.get("province"))
            calls["n"] += 1
            if calls["n"] == 1:
                self._publish(entity_iri=cited.iri)
            return len(docs)

        with (
            patch("entities.search_index.index_by_iri"),
            patch("cases.search_index.index"),
            patch("jawafdehi_shared.search.reindex.make_client"),
            patch("jawafdehi_shared.search.reindex.create_index"),
            patch("jawafdehi_shared.search.reindex.stream_bulk", fake_stream_bulk),
        ):
            call_command("reindex_entities", rebuild=True)

        assert sent[cited.iri] == (["Kathmandu"], ["Bagmati"])

    def test_a_geography_failure_aborts_rather_than_blanking_the_facet(self):
        from entities.search_geography import GeographyLookupError

        _seed("alpha-holdings")
        with patch(
            "entities.management.commands.reindex_entities.case_locations",
            side_effect=GeographyLookupError("places unreachable"),
        ):
            with pytest.raises(GeographyLookupError):
                self._run()

    def test_catchup_covers_the_siblings_of_a_location_bound_mid_rebuild(self):
        """A location bind moves the district of EVERY entity its case cites.

        The live fan-out in ``cases.signals`` writes those siblings too, but
        during a ``--rebuild`` it writes them into the OLD generation, which the
        alias swap discards. Without the sibling arm in the catchup, an entity
        bound before the build keeps the district it had when the build started
        and nothing repairs it until the next reindex.
        """
        from cases.models import (
            Case,
            CaseEntityRelationship,
            CaseState,
            CaseType,
            RelationshipType,
        )

        sibling = _seed("alpha-holdings")
        with patch("cases.search_index.index"), patch(
            "entities.search_index.index_by_iri"
        ):
            case = Case.objects.create(
                title="Located mid-rebuild",
                offence_type=CaseType.CORRUPTION,
                state=CaseState.PUBLISHED,
            )
            CaseEntityRelationship.objects.create(
                case=case,
                nes_id=sibling.iri,
                relationship_type=RelationshipType.ACCUSED,
            )

        sent: dict[str, tuple] = {}
        calls = {"n": 0}

        def fake_stream_bulk(client, index, docs):
            docs = list(docs)
            for doc in docs:
                sent[doc["iri"]] = (doc.get("district"), doc.get("province"))
            calls["n"] += 1
            if calls["n"] == 1:
                # Only the LOCATION bind is added — the sibling's own row is
                # untouched and its case is not saved, so sources 1 and 3 are
                # both blind to it.
                CaseEntityRelationship.objects.create(
                    case=case,
                    nes_id=self.district.iri,
                    relationship_type=RelationshipType.LOCATION,
                )
            return len(docs)

        with (
            patch("entities.search_index.index_by_iri"),
            patch("cases.search_index.index"),
            patch("jawafdehi_shared.search.reindex.make_client"),
            patch("jawafdehi_shared.search.reindex.create_index"),
            patch("jawafdehi_shared.search.reindex.stream_bulk", fake_stream_bulk),
        ):
            call_command("reindex_entities", rebuild=True)

        assert sent[sibling.iri] == (["Kathmandu"], ["Bagmati"])
