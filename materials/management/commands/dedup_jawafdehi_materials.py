"""``dedup_jawafdehi_materials`` — detect (and optionally merge) duplicate case uploads.

Scans every ``/material/jawafdehi/*`` material (documents caseworkers attached to
cases) and decides, by natural key, whether each duplicates a canonical corpus
material (a ``ciaa_press_release`` by press-release number, a ``court_order`` by
court-case number).

Two modes, read-only by default:

* ``--dry-run`` (DEFAULT) — classify every material and, for a ``duplicate``, print the
  MERGE PLAN (which case references would repoint, which collide). **Mutates nothing.**
  This is the audit.
* ``--apply`` — perform the merge: repoint each ``CaseMaterialReference`` from the
  jawafdehi IRI to the canonical IRI (collision-deduping), then soft-delete the
  jawafdehi material. The canonical's visibility is deliberately left untouched (it is
  public NGM corpus). See ``materials.dedup_merge`` and the design spec.

Because the database is reachable only through the API (no local DB), this produces real
numbers only when run on a deployed environment (staging / prod) against the real
Postgres; locally its correctness is proven by the sqlite tests. On an ephemeral prod
pod, use ``--output -`` to stream the JSONL report to stdout (the summary then goes to
stderr) — the only reliably-retrievable channel.

Usage::

    python manage.py dedup_jawafdehi_materials [--output PATH|-] [--limit N]
    python manage.py dedup_jawafdehi_materials --apply [--limit N] [--output -]
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import nullcontext

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cases.models import CaseMaterialReference
from materials.dedup import CanonicalRef, Outcome, extract_canonical_key
from materials.dedup_merge import apply_merge, plan_merge
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
    help = "Detect (default) or merge (--apply) jawafdehi case uploads that duplicate a canonical material."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            help="Path for the JSONL report ('-' streams to stdout; default dedup-<stamp>.jsonl).",
        )
        parser.add_argument(
            "--limit", type=int, help="Only process the first N materials (spot check / staged apply)."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read-only: report + merge plan, mutate nothing (this is the default).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform the merge (repoint references, soft-delete duplicates). MUTATES DATA.",
        )

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("--apply and --dry-run are mutually exclusive.")
        do_apply = options["apply"]

        output = options.get("output")
        to_stdout = output == "-"
        if not output:
            stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
            output = f"dedup-{stamp}.jsonl"

        # When the report streams to stdout, the human summary must go to stderr so
        # `... --output - > report.jsonl` captures only JSONL.
        summary = self.stderr if to_stdout else self.stdout

        if do_apply:
            summary.write(self.style.WARNING(
                "APPLYING merge — this MUTATES case evidence (repoint + soft-delete).\n"
            ))

        qs = Material.objects.filter(source="jawafdehi", is_deleted=False).order_by("iri")
        if options.get("limit"):
            qs = qs[: options["limit"]]

        buckets: Counter = Counter()
        by_source_type: Counter = Counter()
        applied: Counter = Counter()
        total = 0

        sink = nullcontext(None) if to_stdout else open(output, "w", encoding="utf-8")
        with sink as fh:
            def emit(record: dict) -> None:
                line = json.dumps(record, ensure_ascii=False)
                if to_stdout:
                    self.stdout.write(line)
                else:
                    fh.write(line + "\n")

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

                if final == DUPLICATE:
                    if do_apply:
                        result = apply_merge(row.iri, canonical_iri)
                        record["applied"] = {
                            "refs_repointed": result.refs_repointed,
                            "refs_deduped": result.refs_deduped,
                            "soft_deleted": result.soft_deleted,
                        }
                        applied["materials"] += 1
                        applied["refs_repointed"] += result.refs_repointed
                        applied["refs_deduped"] += result.refs_deduped
                    else:
                        plan = plan_merge(row.iri, canonical_iri)
                        record["plan"] = {
                            "refs_to_repoint": plan.refs_to_repoint,
                            "collisions": plan.collisions,
                        }

                emit(record)

        self._print_summary(
            summary, total, buckets, by_source_type, applied, do_apply,
            "(stdout)" if to_stdout else output,
        )

    def _print_summary(self, out, total, buckets, by_source_type, applied, do_apply, output):
        dup = buckets.get(DUPLICATE, 0)
        if do_apply:
            out.write(self.style.SUCCESS(
                f"\nMerged {applied.get('materials', 0)} of {total} jawafdehi materials "
                "into the canonical document we already hold.\n"
            ))
            out.write(f"  references repointed:        {applied.get('refs_repointed', 0)}")
            out.write(f"  references deduped (collide): {applied.get('refs_deduped', 0)}")
        else:
            out.write(self.style.SUCCESS(
                f"\n{dup} of {total} jawafdehi materials duplicate a document we already hold.\n"
            ))
        out.write("Outcome buckets:")
        for name in _BUCKET_ORDER:
            out.write(f"  {buckets.get(name, 0):>5}  {name}")
        out.write("\nBy source type (what the case uploads are):")
        for source_type, count in by_source_type.most_common():
            out.write(f"  {count:>5}  {source_type}")
        out.write(f"\nReport written to {output}")


def _display_name(name) -> str:
    if isinstance(name, dict):
        return name.get("ne") or name.get("en") or ""
    if isinstance(name, str):
        return name
    return ""
