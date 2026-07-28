# `materials/sourcing/` — external-source ingestion

One subpackage per external source that feeds Materials into the archive
(`nkp/` — Nepal Law Journal precedents; `ag/` — Attorney-General indictments;
`ciaa/` — CIAA press releases, `प्रेस विज्ञप्ति`).

## Convention

Each source lives in `materials/sourcing/<source>/` and owns:

- **`shaper.py`** — a **pure, DB-free** projection from the scraped record to
  Material JSON-LD, returning `(doc, material_type)`. It calls the shared
  contract in `materials.jsonld` (`MATERIAL_CONTEXT`, `type_for`,
  `media_objects_from_document_sources`, `MaterialType`) and mints its `@id` via
  `jawafdehi_shared.entities.ids.build_material_iri` under a source-specific IRI
  segment. Unit-test it like any pure function (see
  `materials/tests/test_*_shaper.py`).
- **crawl / parse / normalize** helpers as needed (e.g. `nkp/crawl.py`).

The shaper's `(doc, material_type)` tuple is the single shape across sources — a
generic dict shaped for `POST /api/materials/` plus the promoted-column token.

## Ingestion is via the API plane — never ORM-direct

Sourcing pipelines are **HTTP clients** of the material API; they do not reach
into the ORM. A pipeline `POST`s the shaped doc to `/api/materials/` (or uploads
files to `/api/materials/<source>/<ident>/file` then `PUT`s the doc). Server-side
every write funnels through the one upsert primitive
(`materials.single_source_ingest.upsert_single_source_material`): idempotent by
`@id`, `created_at`-preserving, and revive-on-re-upsert. Keeping ingestion on the
API plane respects the service boundary (the scrape→OCR→shape loop lives outside
the Django app) and means there is exactly one write path to reason about.

The rule is about the ORM, not about *where* the client lives. A recurring
in-repo `manage.py` command (e.g. `scrape_ciaa_press_releases`) is fine **as long
as it is only an HTTP client** — it fetches, shapes, and calls the material API
exactly like an external pipeline; it must not import models and write rows. This
keeps the acquisition on a CronJob against the platform's own API (one auth-gated,
audited write path) while the source-specific scrape/shape code lives here beside
the other sources.
