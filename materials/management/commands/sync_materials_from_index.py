"""Sync the legacy NGM document-source index into Materials.

Reads ``document_sources`` (the DocumentSourceIndex catalog of R2-hosted docs:
court orders, CIAA press releases / annual reports, Kanun Patrika, PPMO
blacklist) from the legacy ``ngm_v1`` DB, shapes each row into a Material
JSON-LD (``manuscript_jsonld``), converts the Bikram Sambat publication date to
AD (the schema.org ``datePublished``, keeping the original BS as
``jawafdehi:datePublishedBS``), and idempotently upserts into the lake's
``materials`` table. Re-runnable / safe: upsert by the material ``@id`` IRI.

Designed to run on a schedule (a CronJob) so new scraped docs flow into the lake
as the spiders append to the index. The source DB is read through a runtime
Django connection cloned from the ``ngm`` settings with ``NAME=ngm_v1`` (the
``platform_rw`` role has SELECT on ngm_v1).
"""
from __future__ import annotations

import hashlib
import json

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connections
from django.utils import timezone

from jawafdehi_shared.entities.ids import build_material_iri

from ...jsonld import (
    INDEX_SOURCE_TYPE_TO_MATERIAL,
    MaterialType,
    court_order_to_jsonld,
    manuscript_jsonld,
)
from ...models import Material

try:  # the `nepali` package provides the BS calendar tables
    from nepali.datetime import nepalidate
except ImportError:  # pragma: no cover
    nepalidate = None

SOURCE_DB_NAME = "ngm_v1"
SOURCE_ALIAS = "index_src"


def bs_to_ad_iso(date_bs) -> str | None:
    """``YYYY-MM-DD`` Bikram Sambat → AD ISO date string, or None if unconvertible."""
    if not date_bs or nepalidate is None:
        return None
    try:
        y, m, d = (int(p) for p in str(date_bs).split("-"))
        return nepalidate(y, m, d).to_datetime().date().isoformat()
    except Exception:
        return None


def parse_court_order_id(document_id) -> tuple[str, str] | None:
    """``ngm:court-order:<court>:<case_number>`` → ``(court, case_number)``.

    Court orders are reconciled to the importer's canonical model: the @id is
    minted by ``court_order_material_iri`` (hyphen-preserving — matching the
    case-number key, the courtcase IRI, and the read view's ``hasPart``) and the
    body by ``court_order_to_jsonld`` — so this sync path and the importer's
    ``--materialize-orders`` produce the IDENTICAL Material (one row, no
    hyphen/underscore IRI fork). Returns ``None`` for non-court-order ids.
    """
    parts = [p for p in str(document_id).split(":") if p]
    if parts and parts[0] == "ngm":
        parts = parts[1:]
    if len(parts) >= 3 and parts[0] == "court-order":
        return parts[1], ":".join(parts[2:])
    return None


def fallback_iri(document_id: str) -> str:
    """Deterministic Material IRI for index ids that ``manuscript_jsonld`` can't
    slug (e.g. Devanagari document_ids): ``/material/<source>/<sha1(id)[:16]>``."""
    parts = [p for p in document_id.split(":") if p]
    if parts and parts[0] == "ngm":
        parts = parts[1:]
    source = parts[0].replace("-", "_") if parts else "document"
    return build_material_iri(source, hashlib.sha1(document_id.encode()).hexdigest()[:16])


def _source_cursor():
    if SOURCE_ALIAS not in connections.databases:
        connections.databases[SOURCE_ALIAS] = {
            **connections["ngm"].settings_dict,
            "NAME": SOURCE_DB_NAME,
        }
    return connections[SOURCE_ALIAS].cursor()


class Command(BaseCommand):
    help = "Upsert ngm_v1.document_sources into materials (BS publication dates → AD)."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default=None, help="Limit to one dataset.")
        parser.add_argument(
            "--since", default=None,
            help="Only rows with updated_at >= this ISO timestamp (incremental sync).",
        )
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--reindex", action="store_true",
            help="After the upsert, INCREMENTALLY reindex only the materials this "
            "run touched (reindex_materials --since <run start>) — no full rebuild. "
            "This is the scheduled-cron default; the index is never dropped.",
        )
        parser.add_argument(
            "--rebuild-index", action="store_true",
            help="Full reindex_materials --rebuild instead of the incremental "
            "reindex (rare — drops + recreates the whole ngm-materials index).",
        )

    def handle(self, *args, **opts):
        now = timezone.now()
        where, params = [], []
        if opts["dataset"]:
            where.append("dataset = %s"); params.append(opts["dataset"])
        if opts["since"]:
            where.append("updated_at >= %s"); params.append(opts["since"])
        sql = (
            "SELECT document_id, source_type, title, publication_date_bs, "
            "primary_url, html_url, links, doc_metadata FROM document_sources"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)

        total = upserted = errors = bs_ok = bs_fail = 0
        with _source_cursor() as cur:
            cur.execute(sql, params)
            while True:
                rows = cur.fetchmany(opts["batch_size"])
                if not rows:
                    break
                objs: dict[str, Material] = {}
                for did, st, title, pub_bs, purl, hurl, links, meta in rows:
                    total += 1
                    try:
                        links = json.loads(links) if isinstance(links, str) else (links or [])
                        meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
                        court_order = (
                            parse_court_order_id(did) if st == "COURT_ORDER" else None
                        )
                        if court_order:
                            # Canonical court_order Material — IDENTICAL @id + shape
                            # to the importer's --materialize-orders, so the two
                            # paths reconcile to one row (no duplicate).
                            court, case_number = court_order
                            doc = court_order_to_jsonld(
                                {"document_id": did, "url": links},
                                court_identifier=court, case_number=case_number,
                            )
                            mt = MaterialType.COURT_ORDER
                        else:
                            doc = manuscript_jsonld({
                                "document_id": did, "source_type": st, "links": links,
                                "url": purl or hurl, "metadata": {**meta, "title": title},
                            })
                            if not doc.get("@id"):
                                doc["@id"] = fallback_iri(did)
                                doc["identifier"] = did
                            # BS publication date → AD datePublished; preserve the BS.
                            bs = pub_bs or meta.get("publication_date") or meta.get("date")
                            doc.pop("datePublished", None)
                            if bs:
                                ad = bs_to_ad_iso(bs)
                                if ad:
                                    doc["datePublished"] = ad; bs_ok += 1
                                else:
                                    bs_fail += 1
                                doc["jawafdehi:datePublishedBS"] = str(bs)
                            mt = INDEX_SOURCE_TYPE_TO_MATERIAL.get(st, MaterialType.DOCUMENT)
                        obj = Material.from_jsonld(doc, material_type=mt)
                        obj.created_at = obj.updated_at = now
                        objs[obj.iri] = obj
                    except Exception as exc:
                        errors += 1
                        if errors <= 10:
                            self.stderr.write(f"  err {str(did)[:50]}: {exc!r}")
                if objs and not opts["dry_run"]:
                    Material.objects.bulk_create(
                        list(objs.values()), update_conflicts=True,
                        unique_fields=["iri"],
                        update_fields=["material_type", "source", "ident", "data", "updated_at"],
                    )
                    upserted += len(objs)
                self.stdout.write(
                    f"processed={total} upserted={upserted} bs→ad={bs_ok} "
                    f"bs_unconvertible={bs_fail} errors={errors}", ending="\r",
                )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"DONE total={total} upserted={upserted} bs_converted={bs_ok} "
            f"bs_unconvertible={bs_fail} errors={errors}"
        ))
        if opts["dry_run"]:
            return
        if opts["rebuild_index"]:
            self.stdout.write("rebuilding materials index (full)…")
            call_command("reindex_materials", rebuild=True)
        elif opts["reindex"]:
            # INCREMENTAL: index only what this run upserted (their updated_at was
            # set to ``now``), NOT a rebuild — so a daily cron never drops the index.
            self.stdout.write("reindexing materials (incremental, --since run start)…")
            call_command("reindex_materials", since=now.isoformat())
