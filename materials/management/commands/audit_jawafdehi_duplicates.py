"""``audit_jawafdehi_duplicates`` — READ-ONLY duplicate audit for case uploads.

Scans every ``/material/jawafdehi/*`` material (documents caseworkers attached to
cases) and decides, by natural key, whether each duplicates a canonical corpus
material (a ``ciaa_press_release`` by press-release number, a ``court_order`` by
court-case number). Writes a JSONL report and prints a summary.

This command MUTATES NOTHING — no soft-delete, no reference rewrite, no save. It is
the detect (Stage 1) step of a staged deduplication; the merge is a separate,
reviewed follow-up. Because the database is reachable only through the API (no
local DB), this produces real numbers only when run on a deployed environment
(staging / prod) against the real Postgres; locally its correctness is proven by
the sqlite tests. See docs/superpowers/specs/2026-07-14-jawafdehi-dedup-audit-design.md.

Usage::

    python manage.py audit_jawafdehi_duplicates [--output PATH] [--limit N]
"""

from __future__ import annotations

import json
from collections import Counter

from django.core.management.base import BaseCommand
from django.utils import timezone

from cases.models import CaseMaterialReference
from materials.dedup import CanonicalRef, Outcome, extract_canonical_key
from materials.models import Material

#: Final report buckets (a HAS_KEY match splits on canonical existence).
DUPLICATE = "duplicate"          # parsed a key AND the canonical material exists
KEY_BUT_ABSENT = "key_but_absent"  # parsed a key but we hold no canonical copy
NO_CANONICAL_KEY = "no_canonical_key"    # type known, no shared key (charge sheets, laws)
NO_CANONICAL_TWIN = "no_canonical_twin"  # no canonical source at all (news, social, misc)

_BUCKET_ORDER = [DUPLICATE, KEY_BUT_ABSENT, NO_CANONICAL_KEY, NO_CANONICAL_TWIN]


def _find_canonical(ref: CanonicalRef) -> str | None:
    """Return the canonical material IRI for ``ref`` if a live row exists, else None.

    Exact-ident refs (press releases) match on ``(source, ident)``; court refs match
    a ``court_order`` whose ident ends in ``.<case-number>`` (the court is unknown
    from the jawafdehi name). Read-only.
    """
    if ref.ident is not None:
        row = Material.objects.filter(
            source=ref.source, ident=ref.ident, is_deleted=False
        ).first()
        return row.iri if row else None
    if ref.case_number:
        row = Material.objects.filter(
            source=ref.source,
            ident__endswith=f".{ref.case_number}",
            is_deleted=False,
        ).first()
        return row.iri if row else None
    return None


class Command(BaseCommand):
    help = "Read-only audit: which jawafdehi case uploads duplicate a canonical material."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            help="Path for the JSONL report (default: duplicate-audit-<stamp>.jsonl).",
        )
        parser.add_argument(
            "--limit", type=int, help="Only scan the first N materials (spot check)."
        )

    def handle(self, *args, **options):
        stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
        output = options.get("output") or f"duplicate-audit-{stamp}.jsonl"

        qs = Material.objects.filter(source="jawafdehi", is_deleted=False).order_by("iri")
        if options.get("limit"):
            qs = qs[: options["limit"]]

        buckets: Counter = Counter()
        by_source_type: Counter = Counter()
        total = 0

        with open(output, "w", encoding="utf-8") as fh:
            for row in qs.iterator(chunk_size=500):
                total += 1
                data = row.data or {}
                source_type = data.get("jawafdehi:sourceType") or "MISC"
                by_source_type[source_type] += 1

                outcome, ref = extract_canonical_key(data)
                canonical_iri = None
                signal = None
                if outcome == Outcome.NO_CANONICAL_TWIN:
                    final = NO_CANONICAL_TWIN
                elif outcome == Outcome.NO_CANONICAL_KEY:
                    final = NO_CANONICAL_KEY
                else:  # HAS_KEY
                    signal = ref.signal
                    canonical_iri = _find_canonical(ref)
                    final = DUPLICATE if canonical_iri else KEY_BUT_ABSENT

                buckets[final] += 1
                referencing_cases = list(
                    CaseMaterialReference.objects.filter(material_iri=row.iri)
                    .values_list("case__slug", flat=True)
                )
                record = {
                    "jawafdehi_iri": row.iri,
                    "source_type": source_type,
                    "name": _display_name(data.get("name")),
                    "outcome": final,
                    "canonical_iri": canonical_iri,
                    "signal": signal,
                    "referencing_cases": referencing_cases,
                }
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        self._print_summary(total, buckets, by_source_type, output)

    def _print_summary(self, total, buckets, by_source_type, output):
        w = self.stdout.write
        dup = buckets.get(DUPLICATE, 0)
        w(self.style.SUCCESS(
            f"\n{dup} of {total} jawafdehi materials duplicate a document we already hold.\n"
        ))
        w("Outcome buckets:")
        for name in _BUCKET_ORDER:
            w(f"  {buckets.get(name, 0):>5}  {name}")
        w("\nBy source type (what the case uploads are):")
        for source_type, count in by_source_type.most_common():
            w(f"  {count:>5}  {source_type}")
        w(f"\nReport written to {output}")


def _display_name(name) -> str:
    if isinstance(name, dict):
        return name.get("ne") or name.get("en") or ""
    if isinstance(name, str):
        return name
    return ""
