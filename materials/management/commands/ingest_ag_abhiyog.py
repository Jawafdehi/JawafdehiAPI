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

Modeled on ``ingest_nkp_decisions`` / ``sync_materials_from_index``: batched,
resumable-by-``@id``, ``--dry-run`` prints shaped docs without writing or uploading.

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
        parser.add_argument("--batch-size", type=int, default=200)
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

        store_file_as_link = None
        if not dry and pdf_dir is not None:
            from jawafdehi_shared.storage import store_file_as_link  # noqa

        total = shaped = ingested = errors = 0
        for line in index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if tier and rec.get("court_tier") != tier:
                continue
            if rec.get("type") != "pdf" and not (md_dir / f"{rec.get('local')}.md").exists():
                # non-PDF still ingestible if it has markdown; else skip
                pass
            total += 1
            if opts["limit"] and total > opts["limit"]:
                total -= 1
                break

            local = rec.get("local")
            md_path = md_dir / f"{local}.md"
            markdown = md_path.read_text(encoding="utf-8") if md_path.exists() else None

            # BS filing date → AD (record may already carry date_ad). If the
            # BS→AD converter is unavailable (nepali_datetime not installed),
            # fall back to preserving the raw BS date so the filing date is NOT
            # silently dropped — datePublished then carries the BS string, and
            # the BS original always rides on jawafdehi:filingDateBS below.
            if not rec.get("date_ad"):
                ad = _bs_to_ad_iso(rec.get("created_date_np"))
                if ad:
                    rec["date_ad"] = ad

            pdf_url = markdown_url = None
            try:
                if not dry and pdf_dir is not None:
                    pdf_file = pdf_dir / local
                    if pdf_file.exists():
                        with pdf_file.open("rb") as fh:
                            pdf_url = store_file_as_link(fh, role="RAW")["link"]
                    if markdown is not None:
                        from django.core.files.base import ContentFile

                        md_link = store_file_as_link(
                            ContentFile(markdown.encode("utf-8"), name=f"{local}.md"),
                            role="MARKDOWN",
                        )
                        markdown_url = md_link["link"]

                doc, mt = ag_abhiyog_to_jsonld(
                    rec, markdown=markdown, pdf_url=pdf_url, markdown_url=markdown_url
                )
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
                         "media": [m["jawafdehi:linkRole"] for m in doc.get("associatedMedia", [])],
                         "text_chars": len(doc.get("text", {}).get("ne", ""))},
                        ensure_ascii=False))
                continue

            try:
                upsert_single_source_material(doc, material_type=mt)
                ingested += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.stderr.write(f"INGEST-ERR {doc['@id']}: {e}")

        mode = "DRY RUN" if dry else "INGESTED"
        self.stdout.write(self.style.SUCCESS(
            f"[{mode}] scanned={total} shaped={shaped} "
            f"{'would-ingest' if dry else 'ingested'}={shaped if dry else ingested} "
            f"errors={errors} (tier={tier or 'ALL'})"))
