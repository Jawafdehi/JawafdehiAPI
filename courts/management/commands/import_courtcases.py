"""``import_courtcases`` — production loader for legacy NGM court data (spec 01).

Loads legacy NGM court cases / hearings / parties (+ optionally materialises court
orders as standalone ``court_order`` Materials) into the consolidated ``ngm`` lake,
with a ``nes_id`` re-key, data-quality guards, and OpenSearch (re)indexing.

Unlike ``seed_courtcases`` (DEV-ONLY, DEBUG-guarded fixtures), this is the
PRODUCTION loader — no DEBUG guard. The prod path is ``--mode=inplace`` (the
``ngm`` alias points at the existing ``ngm_v1``, so the rows are already present
and the run NORMALISES them in place); ``--mode=copy --source-dsn=...`` is the
fresh-target / DR ETL path.

    # Dry-run supreme (no writes; eyeball the DQ + re-key counters):
    manage.py import_courtcases --court supreme --dry-run --json
    # Full supreme + special with order materialisation + incremental index:
    manage.py import_courtcases --court supreme --materialize-orders --json
    manage.py import_courtcases --court special --materialize-orders --json
    # Fresh target (DR) via copy ETL:
    manage.py import_courtcases --all-courts --mode copy --source-dsn postgresql://.../ngm_v1
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from courts.importer import (
    ORDER_COURTS,
    CourtCaseImporter,
    ImportConfig,
    ImportMode,
)


class Command(BaseCommand):
    help = (
        "Production importer: load legacy NGM court cases/hearings/parties/orders "
        "into the ngm lake (re-key + DQ + optional order Materials + index)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--court", action="append", dest="courts",
                            help="Restrict to a court (repeatable). Mutually exclusive with --all-courts.")
        parser.add_argument("--all-courts", action="store_true",
                            help="Import every court.")
        parser.add_argument("--mode", choices=["inplace", "copy"], default="inplace",
                            help="inplace (prod, rows already present) | copy (fresh-target ETL).")
        parser.add_argument("--source-dsn",
                            help="Legacy Postgres DSN to read from. REQUIRED for --mode=copy.")
        parser.add_argument("--since",
                            help="Only rows with updated_at >= this AD date/ISO datetime.")
        parser.add_argument("--batch-size", type=int, default=1000,
                            help="Rows per transaction/commit (default 1000).")
        parser.add_argument("--limit", type=int,
                            help="Cap total cases scanned (smoke tests).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Transform + count, no writes, no index.")
        parser.add_argument("--materialize-orders", action="store_true",
                            help="Also upsert standalone court_order Materials (supreme/special only).")
        parser.add_argument("--reindex", choices=["none", "incremental", "rebuild"],
                            default="incremental",
                            help="Post-load OpenSearch reindex (default incremental). "
                                 "rebuild is a search-outage window — requires --yes.")
        parser.add_argument("--allow-nonempty-target", action="store_true",
                            help="(copy) allow writing into a target that already has rows.")
        parser.add_argument("--strict", action="store_true",
                            help="Fail the run on any per-record transform/validation error.")
        parser.add_argument("--yes", action="store_true",
                            help="Confirm a destructive --reindex=rebuild.")
        parser.add_argument("--json", dest="as_json", action="store_true",
                            help="Emit a machine-readable summary.")

    def handle(self, *args, **o):
        if o["mode"] == "copy" and not o["source_dsn"]:
            raise CommandError("--mode=copy requires --source-dsn")
        if bool(o["courts"]) == bool(o["all_courts"]):
            raise CommandError("Pass exactly one of --court ... / --all-courts")
        if o["reindex"] == "rebuild" and not o["yes"]:
            raise CommandError(
                "--reindex=rebuild drops + recreates the index (search-outage "
                "window). Re-run with --yes to confirm, or use --reindex=incremental."
            )
        if o["materialize_orders"] and o["courts"]:
            no_orders = [c for c in o["courts"] if c not in ORDER_COURTS]
            if no_orders:
                self.stderr.write(self.style.WARNING(
                    f"--materialize-orders is a no-op for {', '.join(no_orders)} "
                    f"(only {', '.join(sorted(ORDER_COURTS))} have orders)."
                ))

        cfg = ImportConfig(
            mode=ImportMode(o["mode"]),
            courts=None if o["all_courts"] else list(o["courts"]),
            source_dsn=o["source_dsn"],
            since=o["since"],
            batch_size=o["batch_size"],
            limit=o["limit"],
            dry_run=o["dry_run"],
            materialize_orders=o["materialize_orders"],
            allow_nonempty_target=o["allow_nonempty_target"],
            strict=o["strict"],
        )
        importer = CourtCaseImporter(cfg, stdout=self.stdout, style=self.style)
        try:
            result = importer.run()
        except ValueError as exc:  # guard / strict-mode failure
            raise CommandError(str(exc)) from exc

        index_summary = None
        if not o["dry_run"] and o["reindex"] != "none":
            index_summary = importer.reindex(rebuild=o["reindex"] == "rebuild")

        self._emit(result, index_summary, as_json=o["as_json"], dry_run=o["dry_run"])
        if o["strict"] and result.failed:
            raise CommandError(f"{result.failed} record(s) failed (strict mode).")

    def _emit(self, result, index_summary, *, as_json, dry_run):
        summary = result.to_dict()
        summary["dry_run"] = dry_run
        if index_summary is not None:
            summary["reindex"] = index_summary
        if as_json:
            self.stdout.write(json.dumps(summary))
            return
        prefix = "(dry-run) " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}import_courtcases: scanned={result.scanned} "
            f"upserted={result.upserted} orders={result.orders_materialized} "
            f"dq_verdict={result.dq_verdict_nulled} dq_hc={result.dq_hc_recovered} "
            f"dq_special={result.dq_special_flagged} skipped={result.skipped} "
            f"failed={result.failed}"
        ))
        if index_summary is not None:
            self.stdout.write(self.style.SUCCESS(
                f"reindex: indexed={index_summary['indexed']} "
                f"skipped={index_summary['skipped']}"
            ))
