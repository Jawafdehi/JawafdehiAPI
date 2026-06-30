"""Production court-case importer service (spec 01).

Loads legacy NGM court cases + hearings + parties (+ optionally materializes
court orders) into the consolidated ``ngm`` lake, with data-quality guards and
(driven by the command) OpenSearch indexing.

NOTE: ``nes_id`` (the per-party NES entity link) is INTENTIONALLY NOT imported —
the scraper never populates it on ``ngm_v1`` (it is resolved by a separate pass
that hasn't run), so it is assumed null everywhere. Imported parties carry
``nes_id = NULL``; entity resolution is a later, separate concern.

Two modes (spec 01 §1):

* ``inplace`` (PROD) — the monolith's ``ngm`` alias points at the existing
  ``ngm_v1``; the rows are ALREADY present. The "import" is a NORMALISE + DQ +
  (optional) order-materialize + index pass over rows that are already there.
  Writes are SURGICAL (``.update(...)`` of ORM-owned fields only) so the
  scraper-owned columns (``status``, ``verdict_*``) are never clobbered.
* ``copy`` (FRESH-TARGET / DR) — the ``ngm`` alias points at a fresh/empty target;
  source rows stream in from the legacy DB and are written via the ORM. The source
  is read from ``--source-dsn`` (a psycopg connection), OR — for tests/programmatic
  use — from an injected ``source_rows`` iterable on :class:`ImportConfig`.

This module is the testable service object; the management command
(``import_courtcases``) is a thin CLI wrapper over it.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator

from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save
from django.utils import timezone

from jawafdehi_shared.entities.ids import build_courtcase_iri
from ngm_service.courts import search_index
from ngm_service.courts.models import CaseEntity, Court, CourtCase, CourtCaseHearing
from ngm_service.courts.normalize import best_effort_normalize, is_verdict_sentinel
from ngm_service.materials.jsonld import (
    MaterialType,
    case_order_sources,
    court_case_to_jsonld,
    court_order_to_jsonld,
)
from ngm_service.materials.models import Material
from ngm_service.materials.single_source_ingest import upsert_single_source_material

logger = logging.getLogger("ngm.courts.importer")

#: Only these courts publish order PDFs; ``--materialize-orders`` is a no-op
#: elsewhere.
ORDER_COURTS = frozenset({"supreme", "special"})

#: Legacy ``extra_data`` Devanagari keys the high-court enrichment buries core
#: fields under (spec 01 §6.3).
_HC_PLAINTIFF_KEY = "वादीहरु"
_HC_DEFENDANT_KEY = "प्रतिवादीहरु"
_HC_CASE_TYPE_KEY = "case_type_display"
_HC_CASE_STATUS_KEY = "raw_status_display"

#: The ORM-owned ``CourtCase`` columns the COPY writer populates from a source row
#: (the original projection + the 8 re-added columns; spec 01 §5). Excludes the
#: composite key (court/case_number) and the auto timestamps.
_COPY_CASE_FIELDS = (
    "registration_date_bs", "registration_date_ad", "case_type", "case_status",
    "plaintiff", "defendant", "status", "verdict_type", "verdict_date_bs",
    "verdict_date_ad", "verdict_judge", "case_subject", "hearing_count",
    "registration_number", "extra_data", "document_sources",
)
_COPY_HEARING_FIELDS = (
    "hearing_date_bs", "hearing_date_ad", "bench", "bench_type", "judge_names",
    "lawyer_names", "serial_no", "case_status", "decision_type", "remarks",
    "scraped_at", "extra_data",
)


class ImportMode(str, Enum):
    INPLACE = "inplace"
    COPY = "copy"


class _SkipRow(Exception):
    """Raised to skip a row (e.g. a non-ASCII natural key) without failing it."""


@dataclass
class ImportConfig:
    mode: ImportMode
    courts: list[str] | None = None
    source_dsn: str | None = None
    since: str | None = None
    batch_size: int = 1000
    limit: int | None = None
    dry_run: bool = False
    materialize_orders: bool = False
    allow_nonempty_target: bool = False
    strict: bool = False
    #: Programmatic/test injection of COPY-mode source rows (each a dict — see
    #: ``_upsert_case_copy``). When set, COPY mode reads these instead of opening
    #: ``source_dsn``. Ignored in INPLACE mode.
    source_rows: Iterable[dict[str, Any]] | None = None


@dataclass
class ImportResult:
    scanned: int = 0
    upserted: int = 0
    orders_materialized: int = 0
    dq_verdict_nulled: int = 0
    dq_hc_recovered: int = 0
    dq_special_flagged: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "upserted": self.upserted,
            "orders_materialized": self.orders_materialized,
            "dq_verdict_nulled": self.dq_verdict_nulled,
            "dq_hc_recovered": self.dq_hc_recovered,
            "dq_special_flagged": self.dq_special_flagged,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": list(self.errors),
        }


class CourtCaseImporter:
    """Load/normalise legacy NGM court cases into the ``ngm`` lake."""

    def __init__(self, cfg: ImportConfig, *, stdout=None, style=None):
        self.cfg = cfg
        self.out = stdout
        self.style = style
        self.res = ImportResult()
        # A dry-run is STRICTLY read-only — it issues NO writes to any DB, not
        # even ones that roll back (the statement still executes, and the source
        # ngm_v1 is FROZEN read-only during the v2 migration, so an UPDATE/INSERT
        # is rejected outright). In read-only mode the guards compute their
        # counters + in-memory values but skip every write, and COPY mode reads
        # transforms off the source row dict (a transient, unsaved CourtCase)
        # rather than persisting + re-reading them — so a dry-run needs no
        # writable/populated target at all.
        self._read_only = cfg.dry_run

    # ── orchestration ────────────────────────────────────────────────────────
    def run(self) -> ImportResult:
        self._guard_target()
        with self._signals_muted():
            for batch in self._iter_source_batches():
                if self.cfg.limit:
                    remaining = self.cfg.limit - self.res.scanned
                    if remaining <= 0:
                        break
                    # Truncate the final batch so --limit is exact, not rounded up
                    # to a batch boundary (inplace/injected sources don't push the
                    # limit into their query the way the DSN reader does).
                    batch = batch[:remaining]
                self._process_batch(batch)
                if self.cfg.limit and self.res.scanned >= self.cfg.limit:
                    break
        logger.info("import_courtcases finished: %s", self.res.to_dict())
        return self.res

    def reindex(self, *, rebuild: bool = False) -> dict[str, int]:
        """Bulk-(re)index the cases this run could have touched.

        Incremental: a queryset filtered to the run's courts (+ ``--since`` when
        set) so we don't re-stream the whole corpus. ``rebuild`` drops + recreates
        the index (a search-outage window — caller gates it).
        """
        from jawafdehi_shared.search.opensearch import COURTCASE_INDEX
        from jawafdehi_shared.search.reindex import reindex

        qs = CourtCase.objects.using("ngm").select_related("court")
        if self.cfg.courts:
            qs = qs.filter(court_id__in=self.cfg.courts)
        if self.cfg.since and not rebuild:
            qs = qs.filter(updated_at__gte=self.cfg.since)
        return reindex(
            index=COURTCASE_INDEX,
            records=qs.order_by("court_id", "case_number").iterator(),
            build_doc=search_index.build_doc,
            rebuild=rebuild,
        )

    # ── target guard ─────────────────────────────────────────────────────────
    def _guard_target(self) -> None:
        if self.cfg.dry_run:
            return  # a dry-run never writes, so the target is irrelevant.
        if self.cfg.mode is not ImportMode.COPY or self.cfg.allow_nonempty_target:
            return
        qs = CourtCase.objects.using("ngm")
        if self.cfg.courts:
            qs = qs.filter(court_id__in=self.cfg.courts)
        if qs.exists():
            scope = ",".join(self.cfg.courts) if self.cfg.courts else "all courts"
            raise ValueError(
                f"--mode=copy target already has court_cases for {scope}; refusing "
                "to clobber. Pass --allow-nonempty-target to override."
            )

    # ── signal muting (suppress per-row OpenSearch upserts during bulk write) ──
    @contextmanager
    def _signals_muted(self) -> Iterator[None]:
        from ngm_service.courts import signals as s

        specs = [
            (post_save, CourtCase, "ngm_courtcase_search_index", s._index_courtcase),
            (post_delete, CourtCase, "ngm_courtcase_search_delete", s._delete_courtcase),
            (post_save, CaseEntity, "ngm_caseentity_reindex", s._reindex_on_party_change),
            (post_delete, CaseEntity, "ngm_caseentity_reindex_del", s._reindex_on_party_change),
        ]
        disconnected = []
        for sig, sender, uid, func in specs:
            if sig.disconnect(dispatch_uid=uid, sender=sender):
                disconnected.append((sig, sender, uid, func))
        try:
            yield
        finally:
            for sig, sender, uid, func in disconnected:
                sig.connect(func, sender=sender, dispatch_uid=uid)

    # ── source iteration ─────────────────────────────────────────────────────
    def _iter_source_batches(self) -> Iterator[list[Any]]:
        if self.cfg.mode is ImportMode.INPLACE:
            yield from self._iter_inplace_batches()
        else:
            yield from _chunked(self._iter_copy(), self.cfg.batch_size)

    def _iter_inplace_batches(self) -> Iterator[list[CourtCase]]:
        """Keyset-paginate the target by the composite PK (court, case_number).

        Each batch is an INDEPENDENT ``LIMIT`` query fully materialised to a list
        before processing — NOT a single spanning ``.iterator()`` server-side
        cursor. That is deliberate: the per-batch write transaction commits/rolls
        back between batches, which would invalidate a spanning server-side cursor
        on PostgreSQL ("cursor does not exist"). Our surgical ``.update()`` writes
        don't touch ``updated_at`` (QuerySet.update bypasses auto_now), so the
        keyset stays stable across writes.
        """
        last: tuple[str, str] | None = None
        while True:
            qs = CourtCase.objects.using("ngm").select_related("court")
            if self.cfg.courts:
                qs = qs.filter(court_id__in=self.cfg.courts)
            if self.cfg.since:
                qs = qs.filter(updated_at__gte=self.cfg.since)
            if last is not None:
                lc, lk = last
                qs = qs.filter(Q(court_id__gt=lc) | Q(court_id=lc, case_number__gt=lk))
            batch = list(
                qs.order_by("court_id", "case_number")[: self.cfg.batch_size]
            )
            if not batch:
                return
            yield batch
            last = (batch[-1].court_id, batch[-1].case_number)

    def _iter_copy(self) -> Iterator[dict[str, Any]]:
        if self.cfg.source_rows is not None:
            yield from self.cfg.source_rows
            return
        yield from self._iter_copy_dsn()

    def _iter_copy_dsn(self) -> Iterator[dict[str, Any]]:  # pragma: no cover
        """Stream source rows from a legacy ``--source-dsn`` Postgres.

        KEYSET-paginated on the composite key (court_identifier, case_number) with
        a SQL ``LIMIT`` per page — NOT one unbounded ``SELECT`` (which would pull
        the whole court, e.g. 103k supreme rows, into client memory) and NOT a
        spanning server-side cursor (which would tie up the connection while we
        fetch each case's children). ``--limit`` is pushed into SQL so a smoke run
        reads only what it processes. Read-only (``autocommit=True``) — never opens
        a write transaction on the frozen source. Lazily imports psycopg (only the
        real copy path needs it; tests inject ``source_rows``).
        """
        if not self.cfg.source_dsn:
            raise ValueError("--mode=copy requires --source-dsn (or injected source_rows)")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "copy mode needs psycopg installed to read --source-dsn"
            ) from exc

        where = ["TRUE"]
        base_params: list[Any] = []
        if self.cfg.courts:
            where.append("c.court_identifier = ANY(%s)")
            base_params.append(list(self.cfg.courts))
        if self.cfg.since:
            where.append("c.updated_at >= %s")
            base_params.append(self.cfg.since)
        where_sql = " AND ".join(where)

        conn = psycopg.connect(self.cfg.source_dsn, autocommit=True)
        try:
            last: tuple[str, str] | None = None
            fetched = 0
            while True:
                size = self.cfg.batch_size
                if self.cfg.limit:
                    size = min(size, self.cfg.limit - fetched)
                    if size <= 0:
                        return
                params = list(base_params)
                keyset = ""
                if last is not None:
                    keyset = " AND (c.court_identifier, c.case_number) > (%s, %s)"
                    params += [last[0], last[1]]
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    "SELECT c.*, ct.court_type, "
                    "ct.full_name_nepali AS court_full_name_nepali, "
                    "ct.full_name_english AS court_full_name_english "
                    "FROM court_cases c LEFT JOIN courts ct "
                    "ON ct.identifier = c.court_identifier "
                    f"WHERE {where_sql}{keyset} "
                    "ORDER BY c.court_identifier, c.case_number LIMIT %s",
                    params + [size],
                )
                rows = cur.fetchall()
                if not rows:
                    return
                # Fetch ALL children for the page in ONE query each (not 2 per
                # case — that N+1 turns a minutes job into hours over 1.6M rows).
                keys = [(r["court_identifier"], r["case_number"]) for r in rows]
                hearings = self._fetch_children_batch(
                    conn, "court_case_hearings", keys, dict_row
                )
                entities = self._fetch_children_batch(
                    conn, "court_case_entities", keys, dict_row
                )
                for row in rows:
                    k = (row["court_identifier"], row["case_number"])
                    row["hearings"] = hearings.get(k, [])
                    row["entities"] = entities.get(k, [])
                    yield row
                fetched += len(rows)
                last = (rows[-1]["court_identifier"], rows[-1]["case_number"])
                if len(rows) < size:
                    return
        finally:
            conn.close()

    @staticmethod
    def _fetch_children_batch(conn, table, keys, dict_row):  # pragma: no cover
        """Fetch every child row for a page's ``(court, case_number)`` keys in ONE
        query, grouped into ``{(court, case_number): [rows]}``. Replaces the
        per-case N+1 (two queries per case) with one query per page per table."""
        out: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if not keys:
            return out
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"SELECT * FROM {table} WHERE (court_identifier, case_number) IN ("
            "SELECT court_identifier, case_number "
            "FROM unnest(%s::text[], %s::text[]) AS t(court_identifier, case_number))",
            [[k[0] for k in keys], [k[1] for k in keys]],
        )
        for child in cur:
            out.setdefault(
                (child["court_identifier"], child["case_number"]), []
            ).append(child)
        return out

    # ── per-batch / per-row ──────────────────────────────────────────────────
    def _process_batch(self, batch: list[Any]) -> None:
        if self._read_only:
            # No write transaction at all — the guards issue only SELECTs.
            for row in batch:
                self._process_row(row)
            return
        try:
            with transaction.atomic(using="ngm"):
                for row in batch:
                    self._process_row(row)
                if self.cfg.dry_run:
                    # Roll back AFTER processing — set_rollback() sets
                    # needs_rollback, which would block every subsequent query in
                    # the block, so it must be the LAST statement (not the first).
                    transaction.set_rollback(True, using="ngm")
        except Exception as exc:  # noqa: BLE001 — isolate a batch-level failure
            if self.cfg.strict:
                raise
            self.res.failed += len(batch)
            self.res.errors.append({"batch": True, "message": str(exc)})

    def _process_row(self, row: Any) -> None:
        self.res.scanned += 1
        natural_key = self._natural_key(row)
        try:
            self._validate_key(row)
            case = self._upsert_case(row)
            self._apply_dq_guards(case, row)
            if self.cfg.materialize_orders and case.court_id in ORDER_COURTS:
                self._materialize_orders(case)
            self.res.upserted += 1
        except _SkipRow as exc:
            self.res.skipped += 1
            logger.warning("skip %s: %s", natural_key, exc)
        except Exception as exc:  # noqa: BLE001 — per-record isolation
            self.res.failed += 1
            self.res.errors.append({"key": natural_key, "message": str(exc)})
            if self.cfg.strict:
                raise

    # ── natural key / validation ─────────────────────────────────────────────
    @staticmethod
    def _natural_key(row: Any) -> tuple[str, str]:
        if isinstance(row, CourtCase):
            return (row.court_id, row.case_number)
        return (row.get("court_identifier"), row.get("case_number"))

    def _validate_key(self, row: Any) -> None:
        """Reject a non-ASCII / malformed natural key (no colliding IRI minted)."""
        court_id, case_number = self._natural_key(row)
        try:
            build_courtcase_iri(court_id, best_effort_normalize(case_number or ""))
        except ValueError as exc:
            raise _SkipRow(f"unindexable natural key: {exc}") from exc

    # ── upsert ───────────────────────────────────────────────────────────────
    def _upsert_case(self, row: Any) -> CourtCase:
        if isinstance(row, CourtCase):
            return row  # inplace: the row already exists; we write only what we own
        if self._read_only:
            return self._transient_case(row)  # copy dry-run: no target write
        return self._upsert_case_copy(row)

    def _transient_case(self, row: dict[str, Any]) -> CourtCase:
        """Build an UNSAVED ``CourtCase`` (+ its ``Court``) from a source dict for
        a COPY dry-run, so transforms/shaping run with no target write. Columns the
        source lacks (e.g. ``nes_id``, absent on ngm_v1) are simply omitted →
        their ORM defaults."""
        court = Court(
            identifier=row["court_identifier"],
            court_type=row.get("court_type") or "",
            full_name_nepali=row.get("court_full_name_nepali") or "",
            full_name_english=row.get("court_full_name_english"),
        )
        fields = {f: row.get(f) for f in _COPY_CASE_FIELDS if f in row}
        return CourtCase(court=court, case_number=row["case_number"], **fields)

    def _upsert_case_copy(self, row: dict[str, Any]) -> CourtCase:
        court_id = row["court_identifier"]
        Court.objects.using("ngm").update_or_create(
            identifier=court_id,
            defaults={
                "court_type": row.get("court_type") or "",
                "full_name_nepali": row.get("court_full_name_nepali") or "",
                "full_name_english": row.get("court_full_name_english"),
            },
        )
        defaults = {f: row.get(f) for f in _COPY_CASE_FIELDS if f in row}
        case, _ = CourtCase.objects.using("ngm").update_or_create(
            court_id=court_id, case_number=row["case_number"], defaults=defaults
        )
        # Children: delete-then-insert by (court, case_number) — autoincrement PKs
        # have no composite uniqueness, so this is the idempotent pattern.
        CourtCaseHearing.objects.using("ngm").filter(
            court_id=court_id, case_number=row["case_number"]
        ).delete()
        CaseEntity.objects.using("ngm").filter(
            court_id=court_id, case_number=row["case_number"]
        ).delete()
        hearings = [
            CourtCaseHearing(
                court_id=court_id, case_number=row["case_number"],
                **{f: h.get(f) for f in _COPY_HEARING_FIELDS if f in h},
            )
            for h in row.get("hearings") or []
        ]
        if hearings:
            CourtCaseHearing.objects.using("ngm").bulk_create(hearings)
        # nes_id is intentionally left NULL (not imported — see module docstring).
        entities = [
            CaseEntity(
                court_id=court_id, case_number=row["case_number"],
                side=e.get("side") or "", name=e.get("name") or "",
                address=e.get("address"),
            )
            for e in row.get("entities") or []
        ]
        if entities:
            CaseEntity.objects.using("ngm").bulk_create(entities)
        return case

    # ── surgical writes (skipped in read-only mode) ──────────────────────────
    def _update_case(self, case: CourtCase, **fields: Any) -> None:
        """Write ORM-owned ``CourtCase`` fields (only what we own), or — in
        read-only mode — just reflect them in memory. NEVER a full ``save()`` (so
        scraper-owned columns like ``status``/``verdict_*`` are untouched). We DO
        bump ``updated_at`` explicitly (``QuerySet.update`` bypasses ``auto_now``)
        so a later incremental ``reindex_courtcases --since`` picks up DQ-only
        edits — the keyset paginates on ``(court, case_number)``, NOT
        ``updated_at``, so bumping it can't perturb iteration."""
        if not self._read_only:
            CourtCase.objects.using("ngm").filter(
                court_id=case.court_id, case_number=case.case_number
            ).update(updated_at=timezone.now(), **fields)
        for key, value in fields.items():
            setattr(case, key, value)

    # ── data-quality guards (§6) ─────────────────────────────────────────────
    def _apply_dq_guards(self, case: CourtCase, row: Any) -> None:
        # (1) verdict sentinel — count it; the column is left as the scraper wrote
        # it (the search/material shapers drop it from consumer output).
        if case.verdict_date_bs and is_verdict_sentinel(case.verdict_date_bs):
            self.res.dq_verdict_nulled += 1

        # (2) special-court truncated प्रतिवादीहरु — flag defendants as untrusted.
        if case.court_id == "special":
            self._flag_special_defendants(case, row)

        # (3) high-court fields buried under Devanagari extra_data keys — lift.
        if self._court_type(case, row) == "high":
            self._recover_high_court_fields(case)

    def _flag_special_defendants(self, case: CourtCase, row: Any) -> None:
        if self._read_only and not isinstance(row, CourtCase):
            has_defendant = any(
                e.get("side") == "defendant" for e in (row.get("entities") or [])
            )
        else:
            has_defendant = CaseEntity.objects.using("ngm").filter(
                court_id=case.court_id, case_number=case.case_number, side="defendant"
            ).exists()
        if not has_defendant:
            return
        extra = dict(case.extra_data or {})
        dq = dict(extra.get("_dq") or {})
        if dq.get("special_defendants_untrusted") is True:
            return  # idempotent
        dq["special_defendants_untrusted"] = True
        extra["_dq"] = dq
        self._update_case(case, extra_data=extra)
        self.res.dq_special_flagged += 1

    def _recover_high_court_fields(self, case: CourtCase) -> None:
        extra = case.extra_data or {}
        if not isinstance(extra, dict):
            return
        updates: dict[str, Any] = {}
        if not case.plaintiff and extra.get(_HC_PLAINTIFF_KEY):
            updates["plaintiff"] = extra[_HC_PLAINTIFF_KEY]
        if not case.defendant and extra.get(_HC_DEFENDANT_KEY):
            updates["defendant"] = extra[_HC_DEFENDANT_KEY]
        if not case.case_type and extra.get(_HC_CASE_TYPE_KEY):
            updates["case_type"] = extra[_HC_CASE_TYPE_KEY]
        if not case.case_status and extra.get(_HC_CASE_STATUS_KEY):
            updates["case_status"] = extra[_HC_CASE_STATUS_KEY]
        if not updates:
            return
        self._update_case(case, **updates)
        self.res.dq_hc_recovered += 1

    @staticmethod
    def _court_type(case: CourtCase, row: Any) -> str | None:
        # COPY rows are dicts (a CourtCase is never a dict); INPLACE rows resolve
        # court_type off the select_related Court.
        if isinstance(row, dict) and row.get("court_type"):
            return row["court_type"]
        court = getattr(case, "court", None)
        return getattr(court, "court_type", None)

    # ── order materialization (§4) ───────────────────────────────────────────
    def _materialize_orders(self, case: CourtCase) -> None:
        pairs = case_order_sources(case.document_sources)
        if not pairs:
            return
        for src, n in pairs:
            doc = court_order_to_jsonld(
                src, court_identifier=case.court_id, case_number=case.case_number, n=n
            )
            self._write_material(doc, MaterialType.COURT_ORDER)
            self.res.orders_materialized += 1
        # The case-record Material references the order Materials via hasPart.
        self._write_material(court_case_to_jsonld(case), MaterialType.COURT_CASE)

    def _write_material(self, doc: dict[str, Any], material_type: str) -> None:
        if self.cfg.dry_run:
            # Validate the shaping without writing (the batch tx is rolled back,
            # but skip the save outright so no post_save side effects fire).
            Material.from_jsonld(doc, material_type=material_type).full_clean(
                validate_unique=False
            )
            return
        upsert_single_source_material(doc, material_type=material_type)


def _chunked(iterable: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
