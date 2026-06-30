"""``bulk_ingest_materials`` — the NGM material batch write path.

Reads a JSON or JSONL file of material records and runs them through
:class:`~ngm_service.materials.bulk_ingest.MaterialBulkIngestService`. The NGM
counterpart to NES's ``bulk_ingest`` command — for landing CIAA/NKP/projects/
procurement documents as ``Material`` rows (which auto-index into
``ngm-materials`` via the post_save signal).

Input formats (auto-detected by extension, ``--format`` overrides):
- ``.jsonl`` — one JSON record per line.
- ``.json``  — an array of records, or an object with a ``materials`` (preferred)
  or ``records`` key.

Each record is either ``{"material": {<json-ld>}, "sources": [...],
"material_type": "..."}`` or a bare JSON-LD doc (``@id`` present). Records with
fewer than ``--min-sources`` (default 2) distinct-publisher sources are HELD
(reported, not written).

Examples::

    manage.py bulk_ingest_materials projects_materials.json --json
    manage.py bulk_ingest_materials ciaa.sample.json --dry-run --json
"""

import json
from pathlib import Path
from typing import Any, List

from django.core.management.base import BaseCommand, CommandError

from ngm_service.materials.bulk_ingest import (
    MIN_SOURCES_TO_PUBLISH,
    MaterialBulkIngestService,
)


def _load_records(path: Path, fmt: str) -> List[Any]:
    if fmt == "auto":
        fmt = "jsonl" if path.suffix.lower() == ".jsonl" else "json"

    text = path.read_text(encoding="utf-8")
    if fmt == "jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    data = json.loads(text)
    if isinstance(data, dict):
        if "materials" in data:
            return list(data["materials"])
        if "records" in data:
            return list(data["records"])
        raise CommandError(
            "JSON object input must have a 'materials' or 'records' key."
        )
    if isinstance(data, list):
        return data
    raise CommandError("JSON input must be an array or an object with 'materials'/'records'.")


class Command(BaseCommand):
    help = "Bulk-ingest material (schema.org JSON-LD) records into the ngm DB."

    def add_arguments(self, parser):
        parser.add_argument("input_file", type=str, help="Path to a .json/.jsonl file.")
        parser.add_argument(
            "--format", dest="fmt", choices=["auto", "json", "jsonl"], default="auto",
            help="Input format (default: inferred from file extension).",
        )
        parser.add_argument(
            "--min-sources", dest="min_sources", type=int, default=MIN_SOURCES_TO_PUBLISH,
            help="Minimum distinct-publisher sources to publish (default 2). "
            "Below this a record is HELD (reported, not written).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Validate + classify without writing to the database.",
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
        service = MaterialBulkIngestService(min_sources=opts["min_sources"])
        result = service.ingest(records, dry_run=opts["dry_run"])

        if opts["output_json"]:
            self.stdout.write(json.dumps(result.to_dict(), indent=2))
            if result.failed:
                raise CommandError(f"{result.failed} record(s) failed.")
            return

        mode = "DRY RUN (no writes)" if opts["dry_run"] else "committed"
        self.stdout.write(f"\n=== Material bulk ingest {mode} ===\n")
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
