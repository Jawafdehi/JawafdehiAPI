"""Generation-based index aliasing, so a full rebuild costs no downtime.

THE PROBLEM. ``reindex(rebuild=True)`` used to DELETE the index and immediately
recreate it empty, then refill it over the length of the scan. For
``ngm-courtcases`` that is a ~1h sequential pass over 2.2M rows, during which
public search returned zero court-case results — not an error anyone would
notice, just a silently empty index. Because a rebuild cost that outage, it was
never put on a schedule, and the four weekly crons run WITHOUT ``--rebuild``.
That mode only ever adds, so a doc whose row has since become hidden is never
evicted and staleness accrues until somebody rebuilds by hand.

THE SHAPE OF THE FIX. The public name becomes an ALIAS over a numbered
generation:

    ngm-courtcases           (alias)
      └── ngm-courtcases-000002   (concrete, serving)

A rebuild creates ``-000003`` alongside, fills it while ``-000002`` keeps
serving every read and write, and then moves the alias in ONE cluster-state
update. There is no instant at which the name resolves to nothing.

WHAT THIS ALSO BUYS. ``create_index`` no-ops on an existing index, so a mapping
change could only reach a live index through a rebuild — which is why
``search.service._sort_spec`` carries ``unmapped_type`` for ``weight``, a field
the live index does not have. Each generation is created fresh from the current
``common_mappings()``, so a mapping migration is now just the next rebuild.

VERIFIED AGAINST THE LIVE CLUSTER (2026-08-14), because two of these are the
kind of thing that reads as obviously-true and is not:

* an alias can replace a concrete index of the SAME NAME atomically, via
  ``remove_index`` + ``add`` in one action list — so even the one-time
  bootstrap has no gap;
* ``client.index()`` / ``client.delete()`` against a single-index alias resolve
  to the backing index, so the live signal path needs no ``is_write_index`` and
  no dual-write.
"""

from __future__ import annotations

import re

# A generation is the alias name plus a zero-padded ordinal. Six digits is
# arbitrary but fixed: the width is part of the name, so it cannot be widened
# later without orphaning every existing generation.
GENERATION_WIDTH = 6

# Anchored on BOTH ends and checked against the alias exactly, so a neighbouring
# index that merely starts with the alias name (``ngm-courtcases-archive-000001``)
# is NOT mistaken for a generation and pruned.
_GENERATION_RE = re.compile(r"^(?P<base>.+)-(?P<ordinal>\d{%d})$" % GENERATION_WIDTH)


def generation_name(alias: str, ordinal: int) -> str:
    """``("ngm-courtcases", 3)`` -> ``"ngm-courtcases-000003"``."""
    return f"{alias}-{ordinal:0{GENERATION_WIDTH}d}"


def generation_ordinal(alias: str, index: str) -> int | None:
    """The generation number of ``index``, or None if it is not one of ``alias``."""
    match = _GENERATION_RE.match(index)
    if match is None or match.group("base") != alias:
        return None
    return int(match.group("ordinal"))


def _generation_indices(client, alias: str) -> dict[str, int]:
    """Every existing generation of ``alias``, as ``{index name: ordinal}``."""
    try:
        names = client.indices.get(index=f"{alias}-*", ignore_unavailable=True) or {}
    except Exception:  # noqa: BLE001 — a wildcard with no matches must read as none.
        return {}
    found: dict[str, int] = {}
    for name in names:
        ordinal = generation_ordinal(alias, name)
        if ordinal is not None:
            found[name] = ordinal
    return found


def alias_targets(client, alias: str) -> list[str]:
    """The concrete indices ``alias`` points at (empty if it is not an alias).

    Normally one. The list form exists so ``swap_alias`` detaches ALL of them:
    an alias left pointing at two generations by a half-finished swap would make
    every search return each doc twice.
    """
    try:
        if not client.indices.exists_alias(name=alias):
            return []
        return sorted(client.indices.get_alias(name=alias) or {})
    except Exception:  # noqa: BLE001 — absent alias reads as "not aliased yet".
        return []


def resolve_alias(client, alias: str) -> str | None:
    """The generation currently serving ``alias``, or None if it is not an alias."""
    targets = alias_targets(client, alias)
    if not targets:
        return None
    # Highest generation wins if a stale swap left more than one attached.
    return max(targets, key=lambda n: (generation_ordinal(alias, n) or 0, n))


def next_generation(client, alias: str) -> str:
    """The name to build into next.

    One past the HIGHEST generation that exists, not one past the one currently
    serving: a crashed run leaves an orphan, and reusing its name would build
    into a half-filled index instead of a clean one.
    """
    ordinals = _generation_indices(client, alias).values()
    return generation_name(alias, (max(ordinals) if ordinals else 0) + 1)


def swap_alias(client, alias: str, new_index: str) -> list[str]:
    """Point ``alias`` at ``new_index`` in ONE atomic cluster-state update.

    Returns the names displaced (the old generation, or the concrete index
    consumed by the bootstrap). Handles all three states the cluster can be in:

    * ``alias`` is already an alias — detach the old generation, attach the new;
    * ``alias`` is still a CONCRETE index (the pre-swap world, and the state all
      four indices are in today) — ``remove_index`` DELETES it in the same action
      list, because a name cannot be both an index and an alias;
    * ``alias`` does not exist at all (cold start) — just attach.

    The bootstrap branch is the only destructive step in a rebuild, it runs
    exactly once per index, and it is atomic with the add. Callers must not
    reach it until the new generation is built and non-empty — see the
    ``RebuildAborted`` guard in ``reindex``.
    """
    actions: list[dict] = []
    displaced = alias_targets(client, alias)
    if displaced:
        actions.extend({"remove": {"index": old, "alias": alias}} for old in displaced)
    elif client.indices.exists(index=alias):
        actions.append({"remove_index": {"index": alias}})
        displaced = [alias]
    actions.append({"add": {"index": new_index, "alias": alias}})
    client.indices.update_aliases(body={"actions": actions})
    return displaced


def prune_generations(client, alias: str, keep: str) -> list[str]:
    """Delete every generation of ``alias`` except ``keep``.

    Runs only AFTER a successful swap, never before one: pruning up front would
    race a concurrent build for the same alias, and the disk a stale generation
    holds is not worth that. An orphan from a crashed run therefore survives
    until the next successful rebuild, which is the intended trade.
    """
    doomed = sorted(n for n in _generation_indices(client, alias) if n != keep)
    for name in doomed:
        client.indices.delete(index=name, ignore_unavailable=True)
    return doomed
