"""Tests for generation naming and the atomic alias swap.

The swap is the step that makes a rebuild safe, and its bootstrap branch is
destructive (``remove_index`` deletes the pre-swap concrete index), so each of
the three cluster states it has to handle is pinned here.
"""

from __future__ import annotations

import pytest

from jawafdehi_shared.search import aliases
from jawafdehi_shared.search.tests.fakes import AliasNameConflict, Client

ALIAS = "ngm-courtcases"


def test_generation_name_is_zero_padded():
    assert aliases.generation_name(ALIAS, 3) == "ngm-courtcases-000003"
    assert aliases.generation_name(ALIAS, 42) == "ngm-courtcases-000042"


def test_generation_ordinal_round_trips():
    assert aliases.generation_ordinal(ALIAS, "ngm-courtcases-000003") == 3


def test_generation_ordinal_rejects_a_neighbouring_index():
    # Starts with the alias AND ends in six digits, but is not a generation of it.
    # If this were misread the pruner would delete somebody else's index.
    assert aliases.generation_ordinal(ALIAS, "ngm-courtcases-archive-000001") is None
    assert aliases.generation_ordinal(ALIAS, "ngm-materials-000001") is None
    assert aliases.generation_ordinal(ALIAS, ALIAS) is None
    assert aliases.generation_ordinal(ALIAS, "ngm-courtcases-0003") is None


def test_next_generation_from_cold_start():
    assert aliases.next_generation(Client(), ALIAS) == "ngm-courtcases-000001"


def test_next_generation_follows_the_highest_not_the_live_one():
    # -000004 is an orphan from a run that crashed before its swap. Building into
    # its name again would fill a half-built index instead of a clean one.
    client = Client(
        indices={"ngm-courtcases-000002": {}, "ngm-courtcases-000004": {}},
        aliases={ALIAS: ["ngm-courtcases-000002"]},
    )
    assert aliases.next_generation(client, ALIAS) == "ngm-courtcases-000005"


def test_resolve_alias_returns_none_for_a_concrete_index():
    client = Client(indices={ALIAS: {}})
    assert aliases.resolve_alias(client, ALIAS) is None


def test_resolve_alias_returns_the_backing_generation():
    client = Client(
        indices={"ngm-courtcases-000002": {}},
        aliases={ALIAS: ["ngm-courtcases-000002"]},
    )
    assert aliases.resolve_alias(client, ALIAS) == "ngm-courtcases-000002"


def test_swap_bootstraps_over_a_concrete_index():
    """The one-time migration: every index is in this state today."""
    client = Client(indices={ALIAS: {"old": {}}, "ngm-courtcases-000001": {"new": {}}})

    displaced = aliases.swap_alias(client, ALIAS, "ngm-courtcases-000001")

    assert displaced == [ALIAS]
    # The concrete index is gone and the name is now an alias onto the new one.
    assert ALIAS not in client.store.indices
    assert client.store.aliases == {ALIAS: ["ngm-courtcases-000001"]}
    # A read through the name serves the NEW generation.
    assert client.search_ids(ALIAS) == ["new"]


def test_swap_in_steady_state_detaches_the_old_generation():
    client = Client(
        indices={"ngm-courtcases-000002": {"old": {}}, "ngm-courtcases-000003": {"new": {}}},
        aliases={ALIAS: ["ngm-courtcases-000002"]},
    )

    displaced = aliases.swap_alias(client, ALIAS, "ngm-courtcases-000003")

    assert displaced == ["ngm-courtcases-000002"]
    assert client.store.aliases == {ALIAS: ["ngm-courtcases-000003"]}
    # Detached, but NOT deleted — deleting is the pruner's job, after the swap.
    assert "ngm-courtcases-000002" in client.store.indices
    assert client.search_ids(ALIAS) == ["new"]


def test_swap_on_a_cold_cluster_just_attaches():
    client = Client(indices={"ngm-courtcases-000001": {"new": {}}})

    assert aliases.swap_alias(client, ALIAS, "ngm-courtcases-000001") == []
    assert client.store.aliases == {ALIAS: ["ngm-courtcases-000001"]}


def test_swap_leaves_exactly_one_backing_index():
    """Two attached generations would make every search return each doc twice."""
    client = Client(
        indices={
            "ngm-courtcases-000001": {},
            "ngm-courtcases-000002": {},
            "ngm-courtcases-000003": {},
        },
        aliases={ALIAS: ["ngm-courtcases-000001", "ngm-courtcases-000002"]},
    )

    aliases.swap_alias(client, ALIAS, "ngm-courtcases-000003")

    assert client.store.aliases[ALIAS] == ["ngm-courtcases-000003"]


def test_a_name_cannot_be_both_index_and_alias():
    """Guards the fake itself: without remove_index the bootstrap must fail."""
    client = Client(indices={ALIAS: {}, "ngm-courtcases-000001": {}})
    with pytest.raises(AliasNameConflict):
        client.indices.update_aliases(
            body={"actions": [{"add": {"index": "ngm-courtcases-000001", "alias": ALIAS}}]}
        )


def test_create_index_no_ops_when_the_name_is_already_an_alias():
    """``reindex_all`` calls ensure_indices() on the four public names. Once those
    are aliases, creating a concrete index over one would be a hard conflict —
    the live cluster answers HEAD /<alias> with 200, so create_index skips."""
    from jawafdehi_shared.search.opensearch import create_index

    client = Client(
        indices={"ngm-courtcases-000002": {}},
        aliases={ALIAS: ["ngm-courtcases-000002"]},
    )

    assert create_index(client, ALIAS) is False
    assert client.indices.created == []


def test_prune_keeps_the_live_generation_and_spares_neighbours():
    client = Client(
        indices={
            "ngm-courtcases-000001": {},
            "ngm-courtcases-000002": {},
            "ngm-courtcases-000003": {},
            "ngm-courtcases-archive-000001": {},
            "ngm-materials": {},
        },
        aliases={ALIAS: ["ngm-courtcases-000003"]},
    )

    pruned = aliases.prune_generations(client, ALIAS, keep="ngm-courtcases-000003")

    assert pruned == ["ngm-courtcases-000001", "ngm-courtcases-000002"]
    assert set(client.store.indices) == {
        "ngm-courtcases-000003",
        "ngm-courtcases-archive-000001",
        "ngm-materials",
    }
