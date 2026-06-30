"""``consolidate_roles`` — merge cross-wave roles clobbered by sequential upserts.

PROBLEM this fixes: ``bulk_ingest`` upserts the WHOLE entity document (last-write-
wins). When the same person is sourced by multiple role-waves under the same ``@id``
(e.g. Pushpa Kamal Dahal as an HoR member in the parliament wave, then as PM in the
pms-kings wave), the later wave's document OVERWRITES the earlier one — so the live
doc ends up with only the last wave's ``hasOccupation`` roles. The earlier roles are
NOT lost: every write kept a ``StoredVersion`` snapshot, so the union of all
snapshots holds the complete role history.

This command, for every person whose ``hasOccupation`` differs across its version
snapshots, rebuilds the live ``data`` doc with the UNION of all roles seen across
every version (deduplicated), so a multi-role person carries all their roles at once.

Role identity for dedup = (roleName, memberOf @id, jawafdehi:house|term|tenureEnd) —
so genuinely distinct roles (a person's HoR term AND their PM term, or two different
HoR terms) are all kept, while an identical role re-ingested twice collapses to one.

Idempotent: re-running after a clean merge is a no-op (the live doc already holds
the union). ``--dry-run`` reports what would change without writing. Scoped to
``prefix='person'`` by default (roles are a person concept).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction

from nes_service.entities.models import StoredEntity, StoredVersion


def _as_role_list(has_occupation: Any) -> List[Dict[str, Any]]:
    """Normalize hasOccupation (object | list | absent) to a list of role dicts."""
    if has_occupation is None:
        return []
    if isinstance(has_occupation, list):
        return [r for r in has_occupation if isinstance(r, dict)]
    if isinstance(has_occupation, dict):
        return [has_occupation]
    return []


def _role_key(role: Dict[str, Any]) -> Tuple:
    """Identity of a role for dedup.

    (roleName, memberOf-@id, house/term/tenureEnd discriminator). Distinct terms or
    distinct offices stay distinct; an identical role seen in two snapshots collapses.
    """
    def _flat(v: Any) -> str:
        """Coerce any value (incl. dict/list) to a stable hashable string."""
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        return json.dumps(v, sort_keys=True, ensure_ascii=False)

    role_name = role.get("roleName") or _flat(role.get("jobTitle"))
    member_of = role.get("memberOf") or {}
    member_iri = member_of.get("@id") if isinstance(member_of, dict) else _flat(member_of)
    discriminator = _flat(
        role.get("jawafdehi:house")
        or role.get("jawafdehi:term")
        or role.get("jawafdehi:tenureEnd")
        or role.get("jawafdehi:tenureStart")
        or ""
    )
    return (_flat(role_name), _flat(member_iri), discriminator)


def _merged_roles(iri: str) -> Tuple[List[Dict[str, Any]], int]:
    """Union of all roles across an entity's version snapshots (order-stable, deduped).

    Returns (merged_role_list, distinct_role_count). Iterates snapshots oldest→newest
    so the earliest occurrence of each distinct role wins its position.
    """
    seen: set = set()
    merged: List[Dict[str, Any]] = []
    snapshots = (
        StoredVersion.objects.filter(subject_iri=iri)
        .order_by("version_number")
        .values_list("data", flat=True)
    )
    for snap in snapshots:
        for role in _as_role_list((snap or {}).get("hasOccupation")):
            key = _role_key(role)
            if key not in seen:
                seen.add(key)
                merged.append(role)
    return merged, len(merged)


class Command(BaseCommand):
    help = (
        "Merge cross-wave roles: rebuild each person's hasOccupation as the union "
        "of all roles across its version snapshots (fixes last-write-wins clobber)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--prefix", default="person",
            help="Only consolidate entities whose prefix starts with this (default: person).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--json", dest="output_json", action="store_true",
            help="Emit a JSON summary.",
        )

    def handle(self, *args, **opts):
        prefix = opts["prefix"]
        dry_run = opts["dry_run"]

        scanned = 0
        merged_count = 0
        roles_added = 0
        examples: List[Dict[str, Any]] = []

        # Candidates: entities with >1 version snapshot (single-version entities can't
        # have been clobbered). Scope by prefix.
        qs = StoredEntity.objects.filter(prefix__startswith=prefix).values_list("iri", flat=True)

        for iri in qs.iterator():
            scanned += 1
            live = StoredEntity.objects.filter(pk=iri).values_list("data", flat=True).first()
            if not live:
                continue
            live_roles = _as_role_list(live.get("hasOccupation"))
            union_roles, union_n = _merged_roles(iri)

            # Only act when the union has MORE distinct roles than the live doc
            # currently shows (i.e. a clobber actually happened).
            if union_n > len(live_roles):
                roles_added += union_n - len(live_roles)
                merged_count += 1
                if len(examples) < 15:
                    examples.append({
                        "iri": iri,
                        "live_roles": len(live_roles),
                        "merged_roles": union_n,
                    })
                if not dry_run:
                    new_doc = dict(live)
                    new_doc["hasOccupation"] = union_roles
                    with transaction.atomic():
                        # Persist the merged doc. Bump version + snapshot so the
                        # merge is itself auditable and the post_save reindexes.
                        ent = StoredEntity.objects.get(pk=iri)
                        ent.data = new_doc
                        ent.version = (ent.version or 1) + 1
                        ent.save()
                        StoredVersion.objects.update_or_create(
                            id=f"version:{iri}:{ent.version}",
                            defaults={
                                "subject_iri": iri,
                                "version_number": ent.version,
                                "author_id": "author:role-consolidation",
                                "data": new_doc,
                            },
                        )

        summary = {
            "scanned": scanned,
            "persons_merged": merged_count,
            "roles_recovered": roles_added,
            "dry_run": dry_run,
            "examples": examples,
        }
        if opts["output_json"]:
            self.stdout.write(json.dumps(summary, indent=2))
            return
        mode = "DRY RUN" if dry_run else "applied"
        self.stdout.write(f"\n=== Role consolidation ({mode}) ===")
        self.stdout.write(f"  Scanned (prefix={prefix!r}): {scanned}")
        self.stdout.write(f"  Persons with clobbered roles merged: {merged_count}")
        self.stdout.write(f"  Roles recovered into live docs: {roles_added}")
        for ex in examples:
            self.stdout.write(f"   - {ex['iri']}: {ex['live_roles']} -> {ex['merged_roles']} roles")
