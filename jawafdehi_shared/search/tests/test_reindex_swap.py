"""Tests for the no-downtime rebuild path of the shared reindex driver.

The property under test is not "the new index ends up correct" — it is that the
OLD one keeps serving every read until the instant it is replaced, and that no
write is lost in the swap.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jawafdehi_shared.search import reindex as reindex_mod
from jawafdehi_shared.search.reindex import RebuildAborted, reindex, summary
from jawafdehi_shared.search.tests.fakes import Client, TransportError

ALIAS = "ngm-courtcases"


def build_doc(record):
    """Records are bare IRIs; an empty record stands for a row that fails the gate."""
    return {"iri": record} if record else {}


def run(client, records, **kwargs):
    """Drive the real reindex with bulk writes/deletes landing in the fake."""
    with (
        patch.object(
            reindex_mod, "stream_bulk", lambda c, index, docs: c.bulk_write(index, docs)
        ),
        patch.object(
            reindex_mod,
            "stream_bulk_delete",
            lambda c, index, iris: c.bulk_delete(index, iris),
        ),
    ):
        return reindex(
            index=ALIAS,
            records=records,
            build_doc=build_doc,
            client=client,
            **kwargs,
        )


def test_incremental_path_writes_through_the_name_and_never_swaps():
    client = Client(indices={ALIAS: {"a": {}}})

    result = run(client, ["b"])

    assert result["swapped"] is False
    assert result["index"] == ALIAS
    # Additive, as before: "a" survives even though it was not re-streamed.
    assert client.search_ids(ALIAS) == ["a", "b"]


def test_rebuild_serves_the_old_index_throughout_the_build():
    """The whole point. Mid-stream, a reader must still see the old corpus."""
    client = Client(indices={ALIAS: {"old-1": {}, "old-2": {}}})
    seen_mid_build = []

    def records():
        for iri in ("new-1", "new-2", "new-3"):
            seen_mid_build.append(client.search_ids(ALIAS))
            yield iri

    run(client, records(), rebuild=True, batch_size=1)

    # At every point during the build the name still served the OLD docs.
    assert seen_mid_build == [["old-1", "old-2"]] * 3
    # And only after it completed did the name flip to the new corpus.
    assert client.search_ids(ALIAS) == ["new-1", "new-2", "new-3"]


def test_rebuild_evicts_a_doc_the_stream_no_longer_yields():
    """The reason rebuilds exist: incremental passes can only ever add."""
    client = Client(indices={ALIAS: {"stale": {}, "keep": {}}})

    run(client, ["keep"], rebuild=True)

    assert client.search_ids(ALIAS) == ["keep"]


def test_rebuild_bootstraps_a_concrete_index_into_an_alias():
    client = Client(indices={ALIAS: {"old": {}}})

    result = run(client, ["new"], rebuild=True)

    assert result["swapped"] is True
    assert result["index"] == "ngm-courtcases-000001"
    assert result["displaced"] == [ALIAS]
    assert client.store.aliases == {ALIAS: ["ngm-courtcases-000001"]}


def test_rebuild_on_a_cold_cluster():
    client = Client()

    result = run(client, ["new"], rebuild=True)

    assert result["displaced"] == []
    assert client.search_ids(ALIAS) == ["new"]


def test_second_rebuild_advances_the_generation_and_prunes_the_first():
    client = Client(indices={ALIAS: {"old": {}}})
    run(client, ["a"], rebuild=True)

    result = run(client, ["b"], rebuild=True)

    assert result["index"] == "ngm-courtcases-000002"
    assert result["pruned"] == ["ngm-courtcases-000001"]
    assert list(client.store.indices) == ["ngm-courtcases-000002"]


def test_catchup_recovers_writes_made_during_the_build():
    """Without this a rebuild silently drops an hour of live writes."""
    client = Client(indices={ALIAS: {"old": {}}})
    written_during_build = []

    def records():
        yield "a"
        # The importer upserts a case while the scan is still running. It lands on
        # the OLD generation, which the swap is about to detach.
        client.bulk_write(ALIAS, [{"iri": "mid-build"}])
        written_during_build.append("mid-build")
        yield "b"

    result = run(
        client,
        records(),
        rebuild=True,
        catchup=lambda since: [(iri, iri) for iri in written_during_build],
    )

    assert (result["caught_up"], result["evicted"]) == (1, 0)
    assert client.search_ids(ALIAS) == ["a", "b", "mid-build"]


def test_catchup_evicts_a_record_that_became_ineligible_during_the_build():
    """The regression that made catch-up mandatory rather than upsert-only.

    A case indexed early in the scan is reclassified SENSITIVE while the scan is
    still running. The live signal deletes it — but from the OLD generation. If
    catch-up only upserted, the swap would put the doc back and a reindex would
    undo the sensitive floor.
    """
    client = Client(indices={ALIAS: {"old": {}}})

    def records():
        yield "keep"
        yield "goes-sensitive"

    result = run(
        client,
        records(),
        rebuild=True,
        # The command's own gate rejected it, so it arrives as a tombstone.
        catchup=lambda since: [("goes-sensitive", None)],
    )

    assert (result["caught_up"], result["evicted"]) == (0, 1)
    assert client.search_ids(ALIAS) == ["keep"]


def test_catchup_evicts_when_build_doc_disagrees_with_the_gate():
    """Belt and braces: a record the gate passed but build_doc will not index."""
    client = Client(indices={ALIAS: {"old": {}}})

    result = run(
        client, ["a"], rebuild=True, catchup=lambda since: [("a", "")]
    )

    assert result["evicted"] == 1
    assert client.search_ids(ALIAS) == []


def test_evicting_an_absent_doc_is_not_an_error():
    """A row hidden mid-build that the scan never reached has nothing to delete."""
    client = Client(indices={ALIAS: {"old": {}}})

    result = run(
        client, ["a"], rebuild=True, catchup=lambda since: [("never-indexed", None)]
    )

    assert result["evicted"] == 1
    assert client.search_ids(ALIAS) == ["a"]


def test_without_catchup_the_mid_build_write_is_lost():
    """Pins the cost of omitting catchup, so nobody drops it by accident."""
    client = Client(indices={ALIAS: {"old": {}}})

    def records():
        yield "a"
        client.bulk_write(ALIAS, [{"iri": "mid-build"}])
        yield "b"

    run(client, records(), rebuild=True)

    assert client.search_ids(ALIAS) == ["a", "b"]


def test_empty_rebuild_is_refused_and_leaves_the_old_index_serving():
    """A broken visibility gate must not be able to wipe public search."""
    client = Client(indices={ALIAS: {"old-1": {}, "old-2": {}}})

    with pytest.raises(RebuildAborted, match="refusing to swap"):
        run(client, [], rebuild=True)

    # Old index untouched and still the one the name resolves to...
    assert client.search_ids(ALIAS) == ["old-1", "old-2"]
    assert client.store.aliases == {}
    # ...and the half-built generation is cleaned up rather than left as an orphan.
    assert "ngm-courtcases-000001" not in client.store.indices


def test_empty_rebuild_is_allowed_when_the_index_is_also_empty():
    """A genuinely empty corpus is not an error — only a REGRESSION to empty is."""
    client = Client(indices={ALIAS: {}})

    result = run(client, [], rebuild=True)

    assert result["swapped"] is True
    assert result["indexed"] == 0


def test_skipped_records_are_counted_not_indexed():
    client = Client(indices={ALIAS: {}})

    result = run(client, ["a", "", "b"], rebuild=True)

    assert (result["indexed"], result["skipped"]) == (2, 1)


def test_each_generation_is_created_fresh_so_mappings_can_change():
    """create_index no-ops on an existing index, so a mapping change can only
    reach the cluster through a NEW one. Two rebuilds, two creations."""
    client = Client(indices={ALIAS: {"old": {}}})

    run(client, ["a"], rebuild=True)
    run(client, ["b"], rebuild=True)

    assert client.indices.created == [
        "ngm-courtcases-000001",
        "ngm-courtcases-000002",
    ]


def test_an_unreadable_cluster_aborts_instead_of_swapping():
    """A 5xx on the doc count must not read as "the old index was empty".

    That is the path that turns a transient outage into a destructive swap onto
    a generation nobody has verified.
    """
    client = Client(
        indices={ALIAS: {"old": {}}}, fail_on={"count": TransportError(503)}
    )

    with pytest.raises(TransportError):
        run(client, ["a"], rebuild=True)

    # Untouched: still a concrete index, still serving its docs.
    assert client.store.aliases == {}
    assert client.search_ids(ALIAS) == ["old"]


def test_an_unreadable_alias_lookup_aborts_before_the_destructive_branch():
    """`remove_index` DELETES. Reading a timeout as "not an alias" would send a
    live alias into that branch."""
    client = Client(
        indices={"ngm-courtcases-000002": {"old": {}}},
        aliases={ALIAS: ["ngm-courtcases-000002"]},
        fail_on={"exists_alias": TransportError(503)},
    )

    with pytest.raises(TransportError):
        run(client, ["a"], rebuild=True)

    assert client.store.aliases == {ALIAS: ["ngm-courtcases-000002"]}


def test_an_unreadable_generation_listing_aborts_before_reusing_a_name():
    """Restarting the numbering would build into the index still serving."""
    client = Client(
        indices={"ngm-courtcases-000002": {"old": {}}},
        aliases={ALIAS: ["ngm-courtcases-000002"]},
        fail_on={"get": TransportError(503)},
    )

    with pytest.raises(TransportError):
        run(client, ["a"], rebuild=True)

    assert client.search_ids(ALIAS) == ["old"]


def test_summary_reports_the_swap():
    swapped = {
        "indexed": 27222,
        "skipped": 0,
        "index": "ngm-courtcases-000003",
        "swapped": True,
        "caught_up": 4,
        "evicted": 2,
        "pruned": ["ngm-courtcases-000002"],
    }
    assert summary("ngm-courtcases", swapped) == (
        "ngm-courtcases: indexed=27222 skipped=0 "
        "swapped->ngm-courtcases-000003 caught_up=4 evicted=2 pruned=1"
    )
    plain = {"indexed": 5, "skipped": 1, "index": ALIAS, "swapped": False}
    assert summary("ngm-courtcases", plain) == "ngm-courtcases: indexed=5 skipped=1"
