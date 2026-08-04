# `materials/sourcing/` — external-source ingestion

One subpackage per external source that feeds Materials into the archive
(`nkp/` — Nepal Law Journal precedents; `ag/` — Attorney-General indictments;
`ciaa/` — CIAA press releases, `प्रेस विज्ञप्ति`; `bolpatra/` — PPMO e-GP tender
notices; `ppmo/` — PPMO publications: procurement bulletins + annual reports).

### Procurement sources (added 2026-08)

`bolpatra/` and `ppmo/` are deliberately **separate** sources even though both trace
to the Public Procurement Monitoring Office — they are different hosts, data shapes
and material types:

| | `bolpatra/` | `ppmo/` |
|---|---|---|
| host | `bolpatra.gov.np/egp` (legacy Java/Struts) | `ppmo.gov.np` (GIWMS CMS) |
| unit | one tender notice per `tenderId` | one publication per `/content/{id}/` |
| type | `PROCUREMENT_NOTICE` | `OFFICIAL_REPORT` |
| frontier | dense-ish integer id walk (~325k ids → ~205k real) | seeded + discovered content ids |
| gotchas | 403s the default `python-requests` UA (needs a browser UA); broken TLS chain (`verify=False`); the search pager is session-stateful and NOT exhaustive — the id walk owns completeness; a non-existent id returns an empty FORM SHELL that must not be parsed as data | mostly JS-rendered, but the initial HTML embeds the CDN PDF url; PDFs are **scanned images** |

**Awards / contract winners are not in a structured public feed.** e-GP publishes
notices, not award results. Award data lives inside the `ppmo/` bulletin and annual
report PDFs, which are scanned images — extracting it needs LLM-vision OCR
(`likhit` + `markitdown-ocr` with `OPENAI_API_KEY`/`GEMINI_API_KEY` +
`MARKITDOWN_OCR_MODEL`; the platform uses Bedrock in prod, see
`review/converter.py::_patch_likhit_ocr_dpi`). The `ppmo/` crawler therefore ingests
the **documents** (title + PDF as `associatedMedia`) so they are discoverable now,
and leaves the transcript to a **deferred enrichment** pass that PATCHes `text`. It
never fabricates an empty transcript.

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
