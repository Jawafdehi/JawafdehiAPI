"""``bulk_ingest`` management command — the batch write path.

Reads a JSON or JSONL file of records and runs them through
:class:`~nes_service.services.bulk_ingest.BulkIngestService`. This is the
operator entry point that REPLACES the per-entity migration runner for landing
large public-entity sources (decision Q10) — mirrors the FastAPI
``nes bulk-ingest`` CLI, ported to a Django management command.

Input formats (auto-detected by extension, ``--format`` overrides):
- ``.jsonl`` — one JSON record object per line.
- ``.json``  — a JSON array of records, or an object ``{"records": [...]}``.

Each record is an object understood by ``IngestRecord.from_dict``::

    {
      "entity_prefix": "person",
      "entity_data": {"slug": "ram-...", "names": [{"kind": "PRIMARY", ...}]},
      "sources": [{"url": "https://a.gov.np"}, {"url": "https://b.org"}]
    }

Records with fewer than 2 distinct-publisher sources are HELD (staged, not
published) per the sourcing plan's ≥2-source rule.

Examples::

    manage.py bulk_ingest officials.jsonl --author author:ecn-importer
    manage.py bulk_ingest officials.json --author author:ecn-importer --dry-run --json
"""

import json
from pathlib import Path
from typing import Any, List

from django.core.management.base import BaseCommand, CommandError

from nes_service.services.bulk_ingest import BulkIngestService


def _load_records(path: Path, fmt: str) -> List[Any]:
    if fmt == "auto":
        fmt = "jsonl" if path.suffix.lower() == ".jsonl" else "json"

    text = path.read_text(encoding="utf-8")
    if fmt == "jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    data = json.loads(text)
    if isinstance(data, dict) and "records" in data:
        return list(data["records"])
    if isinstance(data, list):
        return data
    raise CommandError(
        "JSON input must be an array of records or an object with a 'records' key."
    )


class Command(BaseCommand):
    help = "Bulk-ingest entity records from a JSON/JSONL file into the nes DB."

    def add_arguments(self, parser):
        parser.add_argument("input_file", type=str, help="Path to a .json/.jsonl file.")
        parser.add_argument(
            "--author", dest="author_id", required=True,
            help="Author id attributed to every write (e.g. author:ecn-importer).",
        )
        parser.add_argument(
            "--change-description", dest="change_description", default="Bulk ingest",
            help="Change description stored on each version row.",
        )
        parser.add_argument(
            "--format", dest="fmt", choices=["auto", "json", "jsonl"], default="auto",
            help="Input format (default: inferred from file extension).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Validate and classify records without writing to the database.",
        )
        parser.add_argument(
            "--json", dest="output_json", action="store_true",
            help="Output the result summary as JSON.",
        )

    def handle(self, *args, **opts):
        path = Path(opts["input_file"])
        if not path.is_file():
            raise CommandError(f"Input file not found: {path}")

        records = _load_records(path, opts["fmt"])
        service = BulkIngestService()
        result = service.ingest_entities(
            records=records,
            author_id=opts["author_id"],
            change_description=opts["change_description"],
            dry_run=opts["dry_run"],
        )

        if opts["output_json"]:
            self.stdout.write(json.dumps(result.to_dict(), indent=2))
            if result.failed:
                raise CommandError(f"{result.failed} record(s) failed.")
            return

        mode = "DRY RUN (no writes)" if opts["dry_run"] else "committed"
        self.stdout.write(f"\n=== Bulk ingest {mode} ===\n")
        self.stdout.write(f"  Total:   {result.total}")
        self.stdout.write(f"  Created: {result.created}")
        self.stdout.write(f"  Updated: {result.updated}")
        self.stdout.write(
            f"  Held:    {result.held}  "
            "(< 2 distinct-publisher sources — staged, not published)"
        )
        if result.deduped_in_batch:
            self.stdout.write(
                f"  Deduped: {result.deduped_in_batch}  "
                "(same id seen earlier in batch — first-wins)"
            )
        self.stdout.write(f"  Failed:  {result.failed}")

        if result.held_ids:
            self.stdout.write("\nHeld (need a second source):")
            for held_id in result.held_ids:
                self.stdout.write(f"  - {held_id}")
        if result.errors:
            self.stdout.write("\nErrors:")
            for err in result.errors:
                self.stdout.write(f"  - [{err.index}] {err.slug or '?'}: {err.message}")
        if result.failed:
            raise CommandError(f"{result.failed} record(s) failed.")
