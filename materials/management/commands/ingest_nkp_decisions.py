"""``ingest_nkp_decisions`` — land scraped Nepal Law Journal precedents as Materials.

Reads the JSONL emitted by the ``nkp_decisions`` Scrapy spider (one
``NkpDecisionItem`` per line) and ingests each as a ``precedent`` Material via
:class:`~materials.bulk_ingest.MaterialBulkIngestService`.

Two-stage pipeline, kept deliberately separate from the generic
``bulk_ingest_materials`` because NKP records need a shaping step (the scraper's
flat dict → Material JSON-LD, done by ``nkp_decision_to_jsonld``) and a
source-attribution step this command owns:

- The nkp.gov.np page is the single authoritative primary source (authority
  ``nkp.gov.np``). NKP is an official primary-source archive, so
  ``--min-sources`` defaults to 1 here (an official government portal is
  self-corroborating for its own published precedents).
- When a decision carried an upload-error note pointing at the scanned issue PDF
  on ``supremecourt.gov.np`` (``fallback_pdf_url``), that rides as a second,
  distinct-publisher ALTERNATE source.

Examples::

    manage.py ingest_nkp_decisions decisions.jsonl --dry-run --json
    manage.py ingest_nkp_decisions decisions.jsonl            # min-sources 1
    manage.py ingest_nkp_decisions decisions.jsonl --min-sources 2   # HOLD singletons
"""

import json
from pathlib import Path
from typing import Any, Dict, List

from django.core.management.base import BaseCommand, CommandError

from materials.bulk_ingest import MaterialBulkIngestService, MaterialIngestResult
from materials.jsonld import MaterialType, nkp_decision_to_jsonld


def _merge_result(acc: MaterialIngestResult, batch: MaterialIngestResult) -> None:
    """Fold one batch's ingest result into the running total (for batched runs)."""
    acc.total += batch.total
    acc.created += batch.created
    acc.updated += batch.updated
    acc.held += batch.held
    acc.deduped_in_batch += batch.deduped_in_batch
    acc.failed += batch.failed
    acc.held_ids.extend(batch.held_ids)
    acc.errors.extend(batch.errors)

#: NKP precedents publish from a single authoritative government portal, so the
#: publish gate defaults to 1 (vs. the generic material default of 2).
NKP_MIN_SOURCES = 1

NKP_AUTHORITY = "nkp.gov.np"
SUPREME_COURT_AUTHORITY = "supremecourt.gov.np"


def _record_from_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
    """One scraped decision → a bulk_ingest record envelope (material + sources)."""
    doc = nkp_decision_to_jsonld(decision)

    sources: List[Dict[str, Any]] = [
        {
            "url": decision.get("source_url"),
            "title": decision.get("title"),
            "authority": NKP_AUTHORITY,
            "kind": "primary",
        }
    ]
    # The scanned-issue PDF (when the HTML body was an upload-error note) is a
    # genuinely distinct publisher (supremecourt.gov.np), so it can corroborate.
    fallback = decision.get("fallback_pdf_url")
    if fallback:
        sources.append(
            {
                "url": fallback,
                "title": "नेपाल कानून पत्रिका (स्क्यान PDF)",
                "authority": SUPREME_COURT_AUTHORITY,
                "kind": "corroborator",
            }
        )

    return {
        "material": doc,
        "sources": sources,
        "material_type": MaterialType.PRECEDENT,
    }


class Command(BaseCommand):
    help = "Ingest scraped Nepal Law Journal (NKP) precedents as Materials."

    def add_arguments(self, parser):
        parser.add_argument(
            "input_file", type=str, help="Path to the nkp_decisions .jsonl output."
        )
        parser.add_argument(
            "--min-sources", dest="min_sources", type=int, default=NKP_MIN_SOURCES,
            help="Minimum distinct-publisher sources to publish (default 1 for NKP).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Validate + classify without writing to the database.",
        )
        parser.add_argument(
            "--json", dest="output_json", action="store_true",
            help="Output the result summary as JSON.",
        )
        parser.add_argument(
            "--skip-removed", action="store_true",
            help="Skip decisions flagged 'removed' (editorial takedown notices).",
        )
        parser.add_argument(
            "--batch-size", dest="batch_size", type=int, default=500,
            help="Ingest in batches of this many records (default 500). The full "
            "NKP corpus is ~10.5k records / ~500MB, so the file is streamed and "
            "ingested batch-by-batch rather than loaded whole. 0 = one batch.",
        )

    def handle(self, *args, **opts):
        path = Path(opts["input_file"])
        if not path.is_file():
            raise CommandError(f"Input file not found: {path}")

        batch_size = opts["batch_size"]
        service = MaterialBulkIngestService(min_sources=opts["min_sources"])
        result = MaterialIngestResult(dry_run=opts["dry_run"])
        skipped_removed = 0
        n_read = 0

        # Stream the JSONL and ingest in batches so a ~500MB corpus never has to
        # sit fully in memory (nor in one giant transaction). Per-batch results
        # are accumulated into a single summary.
        def _flush(batch):
            if not batch:
                return
            r = service.ingest(batch, dry_run=opts["dry_run"])
            _merge_result(result, r)

        batch: list = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                dec = json.loads(line)
                if opts["skip_removed"] and dec.get("removed"):
                    skipped_removed += 1
                    continue
                n_read += 1
                batch.append(_record_from_decision(dec))
                if batch_size and len(batch) >= batch_size:
                    _flush(batch)
                    batch = []
        _flush(batch)

        if opts["output_json"]:
            summary = result.to_dict()
            summary["skipped_removed"] = skipped_removed
            self.stdout.write(json.dumps(summary, indent=2))
            if result.failed:
                raise CommandError(f"{result.failed} record(s) failed.")
            return

        mode = "DRY RUN (no writes)" if opts["dry_run"] else "committed"
        self.stdout.write(f"\n=== NKP precedent ingest {mode} ===\n")
        self.stdout.write(f"  Read:    {n_read} decisions")
        if skipped_removed:
            self.stdout.write(f"  Skipped: {skipped_removed} (removed/takedown notices)")
        self.stdout.write(f"  Total:   {result.total}")
        self.stdout.write(f"  Created: {result.created}")
        self.stdout.write(f"  Updated: {result.updated}")
        self.stdout.write(
            f"  Held:    {result.held}  "
            f"(< {opts['min_sources']} distinct-publisher sources — not written)"
        )
        if result.deduped_in_batch:
            self.stdout.write(
                f"  Deduped: {result.deduped_in_batch}  (same @id earlier in batch)"
            )
        self.stdout.write(f"  Failed:  {result.failed}")
        if result.errors:
            self.stdout.write("\nErrors:")
            for err in result.errors[:50]:
                self.stdout.write(f"  - [{err['index']}] {err['message']}")
        if result.failed:
            raise CommandError(f"{result.failed} record(s) failed.")
