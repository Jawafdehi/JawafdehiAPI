"""Ingest scraped Attorney-General अभियोगपत्र (indictments) as charge_sheet Materials.

Streams the AG-scrape corpus (a JSONL index of records + their likhit-converted,
font-normalized markdown files) and, per record:

  1. reads the markdown (full text) from ``--markdown-dir`` (already OCR'd — we do
     NOT re-OCR: the text is embedded as ``data["text"]``);
  2. uploads the source PDF (and the markdown) to object storage (R2) via
     ``store_file_as_link``, getting durable ``contentUrl``s (decision: archive in
     R2, not depend on the fragile ag.gov.np host);
  3. shapes a charge_sheet Material via ``materials.sourcing.ag_abhiyog`` (LISTED,
     no entity links — defendant→NES resolution is a later enrichment pass);
  4. upserts via ``upsert_single_source_material`` (bypasses the ≥2-publisher HOLD
     gate — an indictment is inherently single-source). Idempotent by ``@id``.

Modeled on ``ingest_nkp_decisions`` / ``sync_materials_from_index``:
resumable-by-``@id`` (a re-run skips records already ingested, so it does NOT
re-upload files or re-write rows unless ``--reingest`` is passed), and
``--dry-run`` shapes + validates + prints without uploading or writing.

Input JSONL record shape (from the corpus ``index.jsonl``), per line e.g.:
    {"local": "<sha1>.pdf", "court_case_no": "०८२-FT-०५२४", "record_id": 118689,
     "name": "...", "office": "विशेष सरकारी वकील कार्यालय",
     "created_date_np": "2083-3-23", "court_tier": "Special", "type": "pdf"}
The markdown for a record is expected at ``<markdown-dir>/<local>.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

try:  # BS→AD (same dep sync_materials_from_index uses)
    from nepali_datetime import date as nepalidate
except Exception:  # noqa: BLE001
    nepalidate = None


def _bs_to_ad_iso(date_bs) -> str | None:
    """``YYYY-M-D`` Bikram Sambat → AD ISO ``YYYY-MM-DD``, or None if unconvertible."""
    if not date_bs or nepalidate is None:
        return None
    try:
        y, m, d = (int(p) for p in str(date_bs).split("-"))
        return nepalidate(y, m, d).to_datetime().date().isoformat()
    except Exception:  # noqa: BLE001
        return None


class Command(BaseCommand):
    help = "Ingest AG abhiyogpatra (indictments) as charge_sheet Materials."

    def add_arguments(self, parser):
        parser.add_argument("index", help="Path to the corpus index JSONL.")
        parser.add_argument(
            "--markdown-dir", required=True,
            help="Dir holding <local>.md (likhit-converted, normalized) full text.",
        )
        parser.add_argument(
            "--pdf-dir", default=None,
            help="Dir holding <local> source PDFs to upload to R2 (omit to skip "
            "upload and reference no RAW file — text-only Material).",
        )
        parser.add_argument(
            "--tier", default=None,
            help="Only ingest this court_tier (e.g. Special, High, District).",
        )
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument(
            "--progress-every", type=int, default=200,
            help="Emit a progress line every N processed records.",
        )
        parser.add_argument(
            "--reingest", action="store_true",
            help="Re-process records whose @id already exists (default: skip them, "
            "so an interrupted run resumes without re-uploading or re-writing).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Shape + validate + print; NO upload, NO DB write.",
        )

    def handle(self, *args, **opts):
        # Imports deferred so --help / import stays cheap and DB-free.
        from materials.jsonld import validate_material_jsonld
        from materials.single_source_ingest import upsert_single_source_material
        from materials.sourcing.ag_abhiyog import ag_abhiyog_to_jsonld

        index_path = Path(opts["index"])
        if not index_path.exists():
            raise CommandError(f"index not found: {index_path}")
        md_dir = Path(opts["markdown_dir"])
        pdf_dir = Path(opts["pdf_dir"]) if opts["pdf_dir"] else None
        tier = opts["tier"]
        dry = opts["dry_run"]
        progress_every = max(1, opts["progress_every"])

        store_file_as_link = ContentFile = Material = None
        if not dry:
            from materials.models import Material  # for resume-skip lookups
            if pdf_dir is not None:
                from django.core.files.base import ContentFile
                from jawafdehi_shared.storage import store_file_as_link

        total = shaped = ingested = skipped = errors = 0
        for line in index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if tier and rec.get("court_tier") != tier:
                continue
            if opts["limit"] and total >= opts["limit"]:
                break
            local = rec.get("local")
            md_path = md_dir / f"{local}.md"
            markdown = md_path.read_text(encoding="utf-8") if md_path.exists() else None

            # A record with neither markdown nor a PDF to upload carries no content
            # (empty Material) — skip it rather than emit a text-less shell.
            if markdown is None and pdf_dir is None:
                skipped += 1
                continue

            total += 1

            # BS filing date → AD (record may already carry date_ad). If the
            # BS→AD converter is unavailable (nepali_datetime not installed),
            # preserve the raw BS date so the filing date is NOT silently dropped
            # (datePublished falls back to the BS string; jawafdehi:filingDateBS
            # always carries the BS original — see the shaper).
            if not rec.get("date_ad"):
                ad = _bs_to_ad_iso(rec.get("created_date_np"))
                if ad:
                    rec["date_ad"] = ad

            # Shape + validate FIRST (pure, no I/O) so a bad record fails cheaply
            # BEFORE any upload — never orphan an R2 blob for a record we can't ingest.
            try:
                doc, mt = ag_abhiyog_to_jsonld(rec, markdown=markdown)
                validate_material_jsonld(doc, iri=doc["@id"])
                shaped += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.stderr.write(f"SHAPE-ERR {local}: {e}")
                continue

            if dry:
                if shaped <= 5:
                    self.stdout.write(json.dumps(
                        {"@id": doc["@id"], "type": mt,
                         "caseNumber": doc.get("jawafdehi:caseNumber"),
                         "datePublished": doc.get("datePublished"),
                         "has_pdf": pdf_dir is not None and (pdf_dir / local).exists(),
                         "text_chars": len(doc.get("text", {}).get("ne", ""))},
                        ensure_ascii=False))
                if total % progress_every == 0:
                    self.stdout.write(f"  ... {total} scanned")
                continue

            # Resume: skip records already ingested (idempotent by @id) so a
            # re-run does NOT re-upload files or re-write rows.
            if not opts["reingest"] and Material.objects.using("ngm").filter(
                iri=doc["@id"]
            ).exists():
                skipped += 1
                if total % progress_every == 0:
                    self.stdout.write(f"  ... {total} scanned, {ingested} ingested, {skipped} skipped")
                continue

            try:
                # Upload files ONLY now that the record is known-good. Hashed
                # filenames mean a retried upload reuses the same blob (no dup).
                if pdf_dir is not None:
                    pdf_file = pdf_dir / local
                    pdf_url = None
                    if pdf_file.exists():
                        with pdf_file.open("rb") as fh:
                            pdf_url = store_file_as_link(fh, role="RAW")["link"]
                    md_url = None
                    if markdown is not None:
                        md_url = store_file_as_link(
                            ContentFile(markdown.encode("utf-8"), name=f"{local}.md"),
                            role="MARKDOWN",
                        )["link"]
                    # Re-shape with the R2 URLs now attached as associatedMedia.
                    doc, mt = ag_abhiyog_to_jsonld(
                        rec, markdown=markdown, pdf_url=pdf_url, markdown_url=md_url,
                    )
                upsert_single_source_material(doc, material_type=mt)
                ingested += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.stderr.write(f"INGEST-ERR {doc['@id']}: {e}")

            if total % progress_every == 0:
                self.stdout.write(f"  ... {total} scanned, {ingested} ingested, {skipped} skipped")

        mode = "DRY RUN" if dry else "INGESTED"
        self.stdout.write(self.style.SUCCESS(
            f"[{mode}] scanned={total} shaped={shaped} "
            f"{'would-ingest' if dry else 'ingested'}={shaped if dry else ingested} "
            f"skipped={skipped} errors={errors} (tier={tier or 'ALL'})"))
