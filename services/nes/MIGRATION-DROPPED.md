# NES data-migration runner: RETIRED

The FastAPI NES carried a **data-sourcing migration runner** — `nes/migrations/`
(the dated, per-source migration scripts) and `nes/services/migration/`
(`runner.py` / `manager.py` / `context.py` / `validation.py`, the engine that
executed them entity-by-entity). During the Django/DRF conversion that runner
was **deliberately NOT ported** (decision Q10).

## What replaces it

**Bulk ingest.** Large public-entity sources now land via the bulk-ingest path:

- `nes_service.services.bulk_ingest.BulkIngestService` — validates every record
  with the same rules as `PublicationService.create_entity`, enforces the
  ≥2-source HOLD gate (distinct-publisher independence, eTLD+1), keeps a version
  row per written entity, and writes the accepted set in one transaction.
- `manage.py bulk_ingest <file> --author author:<slug> [--dry-run] [--json]` —
  the operator entry point (mirrors the old `nes bulk-ingest` CLI).

This is set-based, idempotent (upsert by id), re-runnable, and gates unsourced
records instead of blindly applying a migration script.

## What is NOT dropped

**Django _schema_ migrations.** `nes_service/entities/migrations/` (the
`makemigrations`-generated CreateModel migration for the
entities/versions/authors/held_entities tables) is normal Django schema
management and is fully present. Only the *data-sourcing* migration runner is
gone.

## Why

- The runner applied sources one entity at a time with no shared ≥2-source
  verification gate; bulk-ingest makes sourcing a first-class, gated, batched
  operation.
- One sourcing path (bulk-ingest) instead of two (runner + ingest) removes drift
  between how singly-created and source-imported entities are validated/versioned.
