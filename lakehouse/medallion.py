"""Bronze → silver → gold medallion flow for the NGM lakehouse.

These functions document — in signatures + docstrings — the ingestion flow from
``ngm-data-lake-plan.md`` section 3:

    spiders ──► acquisition (TLS-tolerant fetch, likhit/OCR/LibreOffice normalize)
            ──► bronze   (raw + markdown + provenance to R2)
            ──► silver   (extract/transform ──► Iceberg upsert by natural key)
            ──► entity resolution (populate nes_id via the shared NES service)
            ──► gold     (refresh search index + API views + public export)

They are deliberate stubs: the real bodies write to R2 / the Iceberg REST
catalog, which needs live infra we don't have in CI. Each raises
``NotImplementedError`` with the precise contract it will fulfil, so the
structure and seams are complete and reviewable now. The schema they operate on
is fully defined in ``ngm.lakehouse.schema``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from lakehouse.config import LakehouseSettings, load_settings
from lakehouse.schema import SILVER_TABLES, TableSpec, get_table


@dataclass(frozen=True)
class BronzeObject:
    """A single raw capture landed in the bronze zone.

    Mirrors what the acquisition pipeline produces: the original bytes' R2 URI,
    the likhit-converted markdown URI (the new doc modality), and the provenance
    struct that travels with the row all the way into silver.
    """

    bronze_uri: str  # R2 URI of the immutable raw file (HTML/PDF/doc).
    markdown_uri: str | None  # R2 URI of the likhit/OCR markdown, when produced.
    source_type: str  # which silver table family this feeds (e.g. "court_cases").
    provenance: dict[
        str, Any
    ]  # source_url, fetch_method, tls_status, ocr_*, scraped_at.


def ingest_raw(
    objects: Iterable[BronzeObject],
    *,
    settings: LakehouseSettings | None = None,
) -> int:
    """Land raw captures in the bronze zone (append-only).

    Bronze is the immutable landing zone (R2 ``uploads/`` + likhit markdown +
    provenance). Nothing here is the source of truth except the raw capture —
    "ingest first, model later". This writes each :class:`BronzeObject`'s bytes
    and markdown under the bronze bucket and records a provenance manifest;
    re-landing the same source is idempotent on ``bronze_uri``.

    Returns the number of objects landed.

    TODO(bronze): implement R2 writes via boto3 (already a service dep) against
    ``settings.bronze_bucket``. The FastAPI index/publish path (not yet ported to
    this service) already speaks S3/R2 — reuse that client when ported. Largely
    exists today as ``uploads/``; this formalizes provenance + the markdown
    modality.
    """
    settings = settings or load_settings()
    raise NotImplementedError(
        "Bronze ingestion writes raw bytes + markdown + provenance to R2; "
        "needs a live bucket (boto3 against settings.bronze_bucket)."
    )


def promote_to_silver(
    table: str | TableSpec,
    *,
    settings: LakehouseSettings | None = None,
    since: str | None = None,
) -> int:
    """Extract/transform bronze for one source-type and upsert the silver table.

    For the given silver table (name or :class:`TableSpec`), read the relevant
    bronze captures, project them onto the table's typed columns (everything
    else into ``extra_data``), attach ``provenance`` + ``bronze_uri``, and
    **upsert by the table's natural key** (``TableSpec.natural_key``) so
    re-scraping a source is idempotent — implemented as an Iceberg ``MERGE INTO``
    (merge-on-read; copy-on-write is unsupported by DuckDB per the research doc).

    ``since`` optionally limits to bronze landed after a watermark (micro-batch).
    Returns the number of silver rows upserted.

    TODO(silver): implement the MERGE INTO against the attached REST catalog
    using ``ngm.lakehouse.engine.connect``. Backfill mode (for cutover) reads the
    current Postgres court tables instead of bronze — see plan section 6 step 2.
    """
    settings = settings or load_settings()
    spec = get_table(table) if isinstance(table, str) else table
    raise NotImplementedError(
        f"Silver promotion for '{spec.name}' (MERGE INTO on natural key "
        f"{spec.natural_key}) needs a live Iceberg catalog; "
        "use ngm.lakehouse.engine.connect once infra exists."
    )


def resolve_entities(
    table: str | TableSpec,
    *,
    settings: LakehouseSettings | None = None,
) -> int:
    """Populate ``nes_id`` columns via the shared NES resolution service.

    Runs after ``promote_to_silver``: for tables carrying entity refs (parties,
    contractors, officials, named entities in decisions), call the bilingual
    (Devanagari/Roman) NES resolution service and write back ``nes_id``. NGM
    remains the privacy doorway for plaintiff/defendant individuals entering NES.

    Clean-slate contract: ``nes_id`` is the canonical entity ``@id`` IRI
    (``https://<base>/entity/<prefix>/<slug>``), NOT the old opaque
    ``entity:<prefix>/<slug>`` form. The resolver MUST validate every value it
    writes with ``jawafdehi_shared.entities.ids.is_valid_entity_iri`` (the same
    contract the ``courts.models.validate_entity_iri`` field validator enforces),
    so the silver projection and the Postgres tables carry identical IRIs.

    Returns the number of rows whose ``nes_id`` was populated/updated.

    TODO(nes): call the shared resolution service (see NES PR #91 substrate),
    validate the returned IRIs, and MERGE the nes_id back into the silver table.
    """
    settings = settings or load_settings()
    spec = get_table(table) if isinstance(table, str) else table
    raise NotImplementedError(
        f"Entity resolution for '{spec.name}' needs the shared NES service + a "
        "live catalog to write entity @id IRIs back to nes_id."
    )


def refresh_gold(
    *,
    settings: LakehouseSettings | None = None,
    tables: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Rebuild the gold serving zone from silver.

    Gold is what consumers read (plan section 2.3):
    - **API views**: stable materialized projections the REST resources read
      from (so ``/cases`` etc. never bind to physical layout) — the court
      relational view is derived here.
    - **Text search**: (re)index the likhit markdown + structured fields into the
      shared OpenSearch substrate (bilingual Devanagari/Roman).
    - **Public exports**: regenerate the crawlable R2 JSON tree
      (``ngm.index``) from the lakehouse rather than hand-building it.

    ``tables`` optionally restricts the refresh; defaults to all
    :data:`SILVER_TABLES`. Returns a per-target summary
    (e.g. ``{"views": [...], "search_docs": N, "export_paths": [...]}``).

    TODO(gold): materialize views via the attached catalog, push to OpenSearch,
    and regenerate the R2 tree through ``ngm.index.build_index`` reading from the
    lake instead of Postgres.
    """
    settings = settings or load_settings()
    target_names = list(tables) if tables is not None else list(SILVER_TABLES)
    raise NotImplementedError(
        f"Gold refresh for {target_names} needs a live catalog + OpenSearch + R2 "
        "export target; structure is defined, infra is not."
    )
