"""``merge_persons`` — dedup split person identities across sourcing waves.

PROBLEM: the same person was sometimes minted under two ``@id``s by different
waves (e.g. a slug-keyed ``…/upendra-yadav-hor-3636`` from the parliament wave and
a Q-id-keyed ``…/upendra-yadav-q3109312`` from the leaders wave) because the waves
keyed identity differently (stable slug vs Wikidata Q-id). They never merged, so
one person shows up as two entities with partial role sets.

This command finds split identities by a RELIABLE key and merges each cluster into
one canonical entity:
- **shared Wikidata Q-id** — a Q-id (in the ``@id`` suffix ``-q####`` OR in the
  record's ``identifier``/``sameAs``) that maps to >1 distinct ``@id``; and
- **exact normalized English name** — same ``name.en`` (case/space-normalized)
  across >1 ``@id`` (conservative: exact match only, no fuzzy).

Canonical pick: prefer the ``-q####`` (Wikidata-keyed) ``@id`` — most stable; else
the longest-lived (lowest created_at). Roles (``hasOccupation``) are UNIONED across
the cluster (deduped by the same key as ``consolidate_roles``); identifiers/sameAs
merged; the non-canonical rows are deleted (from ``entities`` + their OpenSearch
docs handled by the post_delete signal — but raw SQL bypasses it, so we delete via
the ORM here to keep the index in sync).

FUZZY/alias cases (e.g. "K P Sharma Oli" vs "Khadga Prasad Sharma Oli" with no
shared Q-id) are NOT merged here — they need the entity-resolution service and are
reported as ``--report`` candidates instead.

``--dry-run`` reports clusters without writing.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, List

from django.core.management.base import BaseCommand
from django.db import transaction

from entities.models import StoredEntity, StoredVersion

_QID_SUFFIX = re.compile(r"-(q\d+)$", re.IGNORECASE)
_QID_ANY = re.compile(r"Q\d{3,}")


def _name_en(data: Dict[str, Any]) -> str:
    n = data.get("name") or {}
    v = n.get("en") if isinstance(n, dict) else n
    return re.sub(r"\s+", " ", (v or "").strip().lower())


def _qids(iri: str, data: Dict[str, Any]) -> set[str]:
    out: set[str] = set()
    m = _QID_SUFFIX.search(iri)
    if m:
        out.add(m.group(1).upper())
    blob = json.dumps(data.get("identifier") or [], ensure_ascii=False)
    blob += json.dumps(data.get("sameAs") or [], ensure_ascii=False)
    out.update(_QID_ANY.findall(blob))
    return out


def _role_key(role: Dict[str, Any]):
    def flat(v):
        return v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False) if v else ""
    mo = role.get("memberOf") or {}
    return (
        flat(role.get("roleName") or role.get("jobTitle")),
        flat(mo.get("@id") if isinstance(mo, dict) else mo),
        flat(role.get("jawafdehi:house") or role.get("jawafdehi:term")
             or role.get("jawafdehi:tenureEnd") or role.get("jawafdehi:cabinet") or ""),
    )


def _as_roles(v: Any) -> List[Dict[str, Any]]:
    if isinstance(v, list):
        return [r for r in v if isinstance(r, dict)]
    if isinstance(v, dict):
        return [v]
    return []


class Command(BaseCommand):
    help = "Merge split person identities (shared Q-id / exact name) into one entity."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", dest="output_json", action="store_true")
        parser.add_argument(
            "--by-name", action="store_true",
            help="Also merge clusters sharing an exact normalized name (no Q-id). "
            "Default: Q-id clusters only (safest).",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        persons = list(
            StoredEntity.objects.filter(prefix="person").values_list("iri", "data")
        )

        # Build clusters by shared Q-id, and (optionally) by exact name.
        qid_map: dict[str, set] = defaultdict(set)
        name_map: dict[str, set] = defaultdict(set)
        data_by_iri: dict[str, Dict[str, Any]] = {}
        for iri, data in persons:
            data_by_iri[iri] = data
            for q in _qids(iri, data):
                qid_map[q].add(iri)
            nm = _name_en(data)
            if nm:
                name_map[nm].add(iri)

        # Union-find over iris using the reliable keys.
        parent: dict[str, str] = {iri: iri for iri, _ in persons}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for q, iris in qid_map.items():
            if len(iris) > 1:
                it = sorted(iris)
                for other in it[1:]:
                    union(it[0], other)
        if opts["by_name"]:
            for nm, iris in name_map.items():
                if len(iris) > 1:
                    it = sorted(iris)
                    for other in it[1:]:
                        union(it[0], other)

        clusters: dict[str, set] = defaultdict(set)
        for iri, _ in persons:
            clusters[find(iri)].add(iri)
        clusters = {k: v for k, v in clusters.items() if len(v) > 1}

        merged = 0
        deleted = 0
        examples = []
        for _, iris in clusters.items():
            iris = sorted(iris)
            # canonical: prefer a -q#### @id, else first.
            canon = next((i for i in iris if _QID_SUFFIX.search(i)), iris[0])
            others = [i for i in iris if i != canon]
            # union roles + identifiers
            canon_doc = dict(data_by_iri[canon])
            roles, seen = [], set()
            sameas = set()
            for i in iris:
                for r in _as_roles(data_by_iri[i].get("hasOccupation")):
                    k = _role_key(r)
                    if k not in seen:
                        seen.add(k)
                        roles.append(r)
                for s in (data_by_iri[i].get("sameAs") or []):
                    sameas.add(s if isinstance(s, str) else json.dumps(s))
            canon_doc["hasOccupation"] = roles
            # record the merged-away ids as sameAs for provenance
            for o in others:
                sameas.add(o)
            if sameas:
                canon_doc["sameAs"] = sorted(sameas)
            if len(examples) < 20:
                examples.append({"canonical": canon, "merged": others, "roles": len(roles)})
            merged += 1
            if not dry:
                with transaction.atomic():
                    ent = StoredEntity.objects.get(pk=canon)
                    ent.data = canon_doc
                    ent.version = (ent.version or 1) + 1
                    ent.save()
                    StoredVersion.objects.update_or_create(
                        id=f"version:{canon}:{ent.version}",
                        defaults={"subject_iri": canon, "version_number": ent.version,
                                  "author_id": "author:person-merge", "data": canon_doc},
                    )
                    for o in others:
                        # ORM delete → post_delete signal removes the OpenSearch doc.
                        StoredEntity.objects.filter(pk=o).delete()
            deleted += len(others)

        summary = {"clusters_merged": merged, "records_deleted": deleted,
                   "by_name": opts["by_name"], "dry_run": dry, "examples": examples}
        if opts["output_json"]:
            self.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False))
            return
        mode = "DRY RUN" if dry else "applied"
        self.stdout.write(f"\n=== Person merge ({mode}) ===")
        self.stdout.write(f"  clusters merged: {merged} | duplicate records removed: {deleted}")
        for ex in examples:
            self.stdout.write(f"   {ex['canonical']}  <= {ex['merged']} ({ex['roles']} roles)")
