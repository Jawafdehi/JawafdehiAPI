"""The merge's one advisory check: do these entities live under the same prefix?

The merge refuses nothing on the grounds of what the entities are. The caller is an
authenticated Caseworker naming both IRIs explicitly, with ``dry_run`` available to
preview — the same trust the endpoint already places in them over which entity survives.

A differing top-level prefix is reported so an odd pairing is visible, and it covers the
pairing worth noticing: ``person/…`` against ``organization/…`` or ``location/…`` reads
as a mismatch here without anything in this file knowing what a person is.
"""

from __future__ import annotations

from jawafdehi_shared.entities.ids import parse_entity_iri


def prefix_root(iri: str) -> str:
    """The first segment of an entity IRI's prefix — ``location/district`` → ``location``."""
    try:
        return parse_entity_iri(iri).prefix.split("/", 1)[0]
    except (ValueError, TypeError):
        return ""


def prefix_mismatch(survivor_iri: str, duplicate_iri: str) -> bool:
    """True when two IRIs sit under different top-level prefixes.

    Advisory only. Folding ``kalikot/…`` into ``location/district/…`` is exactly the
    cleanup this endpoint is for, so a mismatch warns and never refuses.
    """
    survivor_root, duplicate_root = prefix_root(survivor_iri), prefix_root(duplicate_iri)
    return bool(survivor_root) and bool(duplicate_root) and survivor_root != duplicate_root
