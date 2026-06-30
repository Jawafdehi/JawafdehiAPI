"""``promote_held`` — clear/flip HELD entities that now have a 2nd source.

The ≥2-source gate stages single-source records in ``held_entities`` (consolidation
part 2). Two promotable situations:

1. **Already-resolved HELD** — the same ``@id`` was later PUBLISHED by another wave
   (it gained a 2nd independent source elsewhere), but the stale ``held_entities``
   row lingers. These are simply CLEARED (the entity is already live).
2. **Cross-publisher pairing inside HELD** — two HELD records describe the same
   real entity from DIFFERENT publishers (the union has ≥2 distinct publishers), so
   together they clear the gate. The clearest case: a PPMO-blacklisted contractor
   and a bolpatra-award contractor sharing a VAT/PAN → PPMO + bolpatra = 2
   independent publishers. These are MERGED + PUBLISHED.

Conservative: only acts on reliable keys (same ``@id`` / shared VAT-PAN identifier);
everything else stays HELD and is reported. ``--dry-run`` reports without writing.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, List

from django.core.management.base import BaseCommand
from django.db import transaction

from nes_service.entities.models import HeldEntity, StoredEntity
from nes_service.entities.persistence import EntityRepository
from nes_service.entities.validation import validate_jsonld_entity


def _vat_pan(data: Dict[str, Any]) -> set[str]:
    """Extract VAT/PAN identifiers from a record (the contractor join key)."""
    out: set[str] = set()
    ids = data.get("identifier") or []
    if isinstance(ids, dict):
        ids = [ids]
    for i in ids:
        if not isinstance(i, dict):
            continue
        pid = (i.get("propertyID") or "").lower()
        val = str(i.get("value") or "").strip()
        if val and ("vat" in pid or "pan" in pid or "tax" in pid):
            out.add(re.sub(r"\D", "", val) or val)
    return out


def _publishers(sources: Any) -> set[str]:
    """Distinct publisher keys from a held row's sources (authority/host)."""
    out: set[str] = set()
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        a = (s.get("authority") or "").strip().lower()
        if a:
            out.add(a)
            continue
        u = s.get("url") or ""
        m = re.search(r"https?://([^/]+)", u)
        if m:
            out.add(m.group(1).lower())
    return out


class Command(BaseCommand):
    help = "Promote/clear HELD entities that now have a 2nd independent source."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", dest="output_json", action="store_true")
        parser.add_argument(
            "--election-authority", action="store_true",
            help="ELECTION-AUTHORITY EXCEPTION (policy 2026-06-28): publish HELD "
            "records whose sources are the Election Commission of Nepal. ECN is the "
            "constitutional authority of record for elected officials, and each "
            "record carries two distinct ECN artifacts (the result JSON + the "
            "certified result-sheet PDF). Flips ward chairs / mayors / candidates live.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        repo = EntityRepository()

        held = list(HeldEntity.objects.all().values_list("iri", "entity_data", "sources"))
        published_ids = set(
            StoredEntity.objects.filter(
                iri__in=[h[0] for h in held]
            ).values_list("iri", flat=True)
        )

        cleared_resolved = 0
        promoted_paired = 0
        promoted_ecn = 0
        examples: List[Dict[str, Any]] = []

        # ECN authority hosts — the election-authority exception keys on these.
        _ECN_AUTHORITIES = ("election.gov.np", "result.election.gov.np")

        # (1) HELD whose @id is already published → clear the stale held row.
        for iri, _data, _src in held:
            if iri in published_ids:
                cleared_resolved += 1
                if not dry:
                    HeldEntity.objects.filter(iri=iri).delete()

        # (2) Cross-publisher VAT/PAN pairing among the still-held rows.
        remaining = [h for h in held if h[0] not in published_ids]
        by_vat: dict[str, list] = defaultdict(list)
        for iri, data, src in remaining:
            for v in _vat_pan(data):
                by_vat[v].append((iri, data, src))

        for vat, rows in by_vat.items():
            if len(rows) < 2:
                continue
            pubs: set[str] = set()
            for _iri, _data, src in rows:
                pubs |= _publishers(src)
            if len(pubs) < 2:
                continue  # still same-publisher; stays HELD
            # Merge: pick the richest doc, union sources; publish.
            rows_sorted = sorted(rows, key=lambda r: len(json.dumps(r[1])), reverse=True)
            canon_iri, canon_data, _ = rows_sorted[0]
            try:
                validate_jsonld_entity(canon_data)
            except Exception:
                continue
            promoted_paired += 1
            if len(examples) < 20:
                examples.append({"iri": canon_iri, "vat": vat, "publishers": sorted(pubs),
                                 "merged_from": [r[0] for r in rows]})
            if not dry:
                from datetime import datetime, timezone
                with transaction.atomic():
                    repo.put_entity(canon_data, version=1, created_at=datetime.now(timezone.utc))
                    for r in rows:
                        HeldEntity.objects.filter(iri=r[0]).delete()

        # (3) Election-authority exception: publish ECN-sourced HELD records.
        if opts["election_authority"]:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            still_held = HeldEntity.objects.all().values_list("iri", "entity_data", "sources")
            for iri, data, src in still_held:
                auths = _publishers(src)
                if not any(any(a in p for a in _ECN_AUTHORITIES) for p in auths):
                    continue
                try:
                    validate_jsonld_entity(data)
                except Exception:
                    continue
                promoted_ecn += 1
                if not dry:
                    with transaction.atomic():
                        repo.put_entity(data, version=1, created_at=now)
                        HeldEntity.objects.filter(iri=iri).delete()

        summary = {
            "cleared_already_published": cleared_resolved,
            "promoted_by_vat_pairing": promoted_paired,
            "promoted_election_authority": promoted_ecn,
            "dry_run": dry,
            "examples": examples,
        }
        if opts["output_json"]:
            self.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False))
            return
        mode = "DRY RUN" if dry else "applied"
        self.stdout.write(f"\n=== HELD promotion ({mode}) ===")
        self.stdout.write(f"  cleared (already published elsewhere): {cleared_resolved}")
        self.stdout.write(f"  promoted (cross-publisher VAT/PAN pairing): {promoted_paired}")
        self.stdout.write(f"  promoted (election-authority / ECN): {promoted_ecn}")
        for ex in examples:
            self.stdout.write(f"   {ex['iri']}  (VAT {ex['vat']}, publishers {ex['publishers']})")
