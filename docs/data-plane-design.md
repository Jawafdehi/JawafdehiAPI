# Data Plane Design — Consolidation (Postgres-SoR, lakehouse-lite)

**Status:** ACCEPTED & BUILT · **Date:** 2026-07-01 · **Branch:** merged on `v2`. All
foundations merged: control-plane `80e2462`; jobs queue #262; **ADR cases-own-no-documents
#263** (`712dc2b`) — Materials is the sole document store; **material_convert FTS feed #264**
(`fd13c35`); **provenance on file-bearing MediaObjects #266**. Remaining work is the
material-specific R2 prefix + governance tables (§8).

**Goal.** One coherent data plane serving three domains (NES entities, NGM
courts/governance, Jawafdehi cases) at target scale, read-heavy via the website,
long-term-stable, **free-OSS-only** (R2 PAYG is the sole accepted spend). This doc
**decides the direction of truth** and **reconciles two prior docs that contradicted
each other** on it (see §7).

This is a *consolidation* doc, not a greenfield: the merged control plane and the
built jobs queue did most of the construction. What remains is small glue + one gap.

---

## 0. Decisions (locked in session)

1. **Postgres (3 DBs + router) is the system of record** for all *served* structured
   data — entities, court cases/hearings/parties, material metadata, corruption cases.
2. **No Iceberg / DuckDB / Lakekeeper lake right now.** "Lakehouse-lite." The
   `lakehouse/` module (`schema.py`, `engine.py`, `medallion.py`, `config.py`) is
   marked **DORMANT, not deleted** — it is the Iceberg-ready seam if an isolated
   analytical/SQL workload ever earns it (§6). This is consistent with the standing
   platform discipline ("no Iceberg for 10³–10⁵ rows", jobs-queue-design §0).
3. **Materials is the single universal document store** across all three domains
   (per [`jawafdehi/adr-cases-own-no-documents.md`](./jawafdehi/adr-cases-own-no-documents.md)).
   There is exactly one document resource — `/api/materials` — with exactly one owner.
4. **Full-text search over materials lives in OpenSearch**, fed from the OCR/likhit
   markdown. Never in DuckDB.
5. **Async heavy work (OCR/extraction, reindex, enrich) runs on the central `jobs`
   queue**, not in the request path or a broker.

---

## 1. The three planes (+ one cross-cutting)

Every access pattern the platform has maps to one plane, and **no plane sits on
another's critical path**:

```
  scrapers / ingestion (/api/ingestion/*) / upload (/api/materials/.../file)
        │                                   [ALL MERGED — R6, feat/jobs-queue]
        ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ ARCHIVE PLANE (R2)  — the object bytes (the bulk of storage)   │
  │   raw capture bytes                       [EXISTS: upload→R2]  │
  │   OCR/likhit markdown  (linkRole=MARKDOWN) [BUILT: #264]       │
  │   provenance on MediaObject (sha256, fetch_method, …) [BUILT]  │
  │   → immutable; system of record for RAW EVIDENCE               │
  └───────────────┬───────────────────────────────┬──────────────┘
     text → Material.data["text"]        bytes referenced by URL (one copy)
                  ▼                                 ▼
  ┌───────────────────────────┐        ┌───────────────────────────────┐
  │ SERVING PLANE — Postgres   │        │ SEARCH PLANE — OpenSearch       │
  │  SoR. 3 DBs + router:      │───────►│  4 indices, bilingual.          │
  │   nes: entities            │ index  │  materials `body` = full text   │
  │   ngm: courts, materials   │        │   [MACHINERY EXISTS; fed #264] │
  │   default: cases, JOBS     │        │  visibility-gated (draft≠public)│
  │  point + list reads (hot)  │        │  point + faceted + FTS (hot)    │
  └───────────────────────────┘        └───────────────────────────────┘
                  ▲
        website reads hit Postgres + OpenSearch ONLY (never the archive engine)

  JOBS QUEUE (default DB) — async plane cutting across ingestion→archive→search:
    kind=material_convert: bytes → OCR markdown → data["text"] → reindex   [BUILT: #264]
    kind=case_review (ported), reindex/enrich (opportunistic)         [BUILT]
```

**Cross-cutting: the archive is the evidentiary backbone.** For an anti-corruption
platform, "show me the original `*.gov.np` capture, with when/how we fetched it" is a
first-class requirement, independent of any analytics ambition. That is why the archive
plane exists in *every* version of this design, lake or no lake.

---

## 2. Serving plane — Postgres is SoR (unchanged topology)

- **3 DBs + router, no cross-DB FKs/joins** (`monolith/config/db_router.py`). Entities →
  `nes`; courts + materials → `ngm`; cases + **jobs** → `default`. This is already built,
  proven, and matches "read-heavy + long-term stability" better than anything else here.
- **Storage form:** schema.org JSON-LD in a JSONB `data` column, keyed by `@id` IRI, with
  promoted typed columns for what we filter on (`entity_type`, `material_type`, `source`,
  `court_identifier`, dates…) + a GIN index on `data` for containment filters.
  `entities.StoredEntity`, `materials.Material`, `courts.CourtCase/*` already do this.
- **Cross-domain joins are forbidden here** (router `allow_relation`). Domains link by
  **string `@id` IRI**, never a cross-DB FK — the same rule the ADR applies to cases.
  This is the constraint that *would* have justified a lake (§6); for now cross-domain
  work is per-domain queries joined in app, which suffices at this scale.

**Scale check (why Postgres is comfortable):** the *structured* corpus is tens of GB
(1M entity JSONB docs ≈ a few GB; 10M court cases + ~10× hearings ≈ tens of GB; 10k CIAA
cases trivial). The large storage figure is **object bytes in the archive plane**, not
database. Postgres is not the scale risk; the archive-plane object count is.

---

## 3. Archive plane — R2, immutable, the document bulk

**Owner: Materials only.** Per the ADR, every document in the platform — case evidence,
court orders, charge sheets, reports, legal corpus — is a `Material` with an `@id`; there
is no `DocumentSource` and no per-domain document table. So the archive plane has one
writer path and one vocabulary.

**What's already built (R6, merged):** `POST /api/materials/<source>/<ident>/file`
streams the uploaded file to R2 (shared hashed-filename S3 mechanism) and appends a
`contentUrl` `MediaObject` with a `jawafdehi:linkRole` to the Material's `associatedMedia`
(`materials/views.py:248`). `/api/ingestion/{cases,entities/resolve,documents}` are real
batch writers (`courts/views.py:352+`).

**What consolidation adds (the two archive gaps):**

1. **OCR/likhit markdown as a roled link.** The extraction step (async, §5) produces
   Devanagari-aware markdown from the raw bytes and stores it in R2 as a
   **`linkRole=MARKDOWN`** MediaObject (the role already exists —
   `materials/jsonld.py:120`) *and* folds the text into `Material.data["text"]` (the field
   the search indexer already reads — §4). Standard recipe: `shared/ocr_bedrock.py`
   (PyMuPDF render + multimodal Claude on Bedrock).
2. **Provenance + content-hash idempotency — BUILT (`materials/provenance.py`).** Each
   file-bearing MediaObject carries a `jawafdehi:provenance` struct (`sha256`,
   `captured_at`, `fetch_method` [`upload`|`ocr`|`scrape`], `source_url`, `content_length`,
   `tls_status`, `ocr_engine`, `ocr_confidence` — `None`s dropped). Provenance is
   **embedded on the MediaObject in the JSON-LD**, NOT a separate `.prov.json` R2 object:
   the Postgres material row IS the record, it's queryable via the JSONB GIN index, and it
   travels with the material (the design's own guidance — "land it as Material provenance,
   not an Iceberg bronze row"; the dormant `lakehouse/medallion.BronzeObject` sketched the
   field shape). `attach_media_object` dedups on **`(role, sha256)`** — re-uploading the
   same bytes replaces that MediaObject instead of appending a duplicate. The upload
   endpoint sets `fetch_method=upload`; `material_convert`'s `on_result` sets
   `fetch_method=ocr`, `ocr_engine=likhit` on the MARKDOWN link.

**R2 object layout (bytes only — provenance rides in the JSON-LD, not on disk):**
```
<FILE_STORAGE_PREFIX><sha256-of-filename>.<ext>   raw capture + the .md markdown blob
```
Bytes are referenced by URL from the Material JSON-LD — **one copy**, no duplication of
the storage bulk into the DB or the index. NOTE: `HashedFilenameS3Boto3Storage` hashes the
*filename* (salted), and everything currently shares one `FILE_STORAGE_PREFIX`
(default `case_uploads/`); the material-specific `material/<source>/<ident>/…` prefix
layout is a remaining follow-up (it needs a storage-backend prefix override, orthogonal to
the provenance record which is done).

### 3.1 How a Material links to its files (the field-level contract)

The URLs are **not** promoted columns. They live inside the Material's JSONB `data`
column as a list of schema.org **`MediaObject`** entries under **`associatedMedia`**.
Every URL variant — the R2-hosted PDF, an external permalink, the source HTML page, the
OCR markdown — is one MediaObject, shaped by `materials/jsonld._media_object`
(`jsonld.py:124`). The `Material` row itself promotes only routing/filter columns
(`iri` = the `@id` PK, `material_type`, `source`, `ident`); URLs stay in the JSON-LD.

**Two axes carry everything you asked about:**
- **Which variant a URL is → `jawafdehi:linkRole`** (an enum on the MediaObject, not
  separate fields): `RAW` | `ALTERNATE` | `PERMALINK` | `SOURCE_PAGE` | `MARKDOWN`. The
  upload endpoint takes this as the `role` multipart param (default `RAW`,
  `views.py:291`); `SOURCE_PAGE`+`MARKDOWN` were added by the ADR so no legacy
  `SourceLinkRole` value is lost.
- **Where the bytes live → the value of `contentUrl`.** A MediaObject does not care
  whether we host the file: an R2 object URL (written by `store_file_as_link` on upload)
  and an external `*.gov.np` permalink (straight from a scraper's roled link) are *both*
  just a `contentUrl` string with a role. One uniform representation, hosted or linked.

**Per-`MediaObject` fields:**

| Field | Meaning | Code |
|---|---|---|
| `@type` | always `"MediaObject"` | `jsonld.py:131` |
| `contentUrl` | the URL itself (R2 object, permalink, source page…) | `jsonld.py:132` |
| `jawafdehi:linkRole` | which variant (`RAW`/`ALTERNATE`/`PERMALINK`/`SOURCE_PAGE`/`MARKDOWN`) | `jsonld.py:133` |
| `encodingFormat` | MIME; auto for SOURCE_PAGE→`text/html`, MARKDOWN→`text/markdown` (`_ROLE_ENCODING_HINTS`), else guessed from filename | `jsonld.py:135-137`, `views.py:235-237` |
| `identifier` | optional — the logical `document_id`, so media trace back to their source doc | `jsonld.py:165-166` |

**Example** (a court order with the R2 scan, its OCR markdown, and an upstream permalink):
```json
{
  "@id": "https://jawafdehi.org/material/court_order/supreme.082-oa-0503",
  "@type": ["Manuscript", "DigitalDocument"],
  "name": {"ne": "..."},
  "text": {"ne": "<OCR full text — the FTS field, see §4>"},
  "associatedMedia": [
    {"@type": "MediaObject", "contentUrl": "https://<r2>/material/court_order/supreme.082-oa-0503/<sha256>.pdf",
     "jawafdehi:linkRole": "RAW", "encodingFormat": "application/pdf"},
    {"@type": "MediaObject", "contentUrl": "https://<r2>/material/court_order/supreme.082-oa-0503/<sha256>.md",
     "jawafdehi:linkRole": "MARKDOWN", "encodingFormat": "text/markdown"},
    {"@type": "MediaObject", "contentUrl": "https://supremecourt.gov.np/...",
     "jawafdehi:linkRole": "PERMALINK"}
  ]
}
```

**The OCR step writes two distinct fields — do not conflate them (see §5):**
1. `associatedMedia[linkRole=MARKDOWN]` with `contentUrl` = the R2 `.md` URL — "here is
   the markdown **file**" (a retrievable roled link).
2. `data["text"]` (language-mapped `{"ne": …}`) — "here is the **text**" — the field
   `materials/search_index.py:58` indexes into the OpenSearch `body` for FTS/snippets.

The same conversion produces both. Court orders reach `associatedMedia` via
`media_objects_from_document_sources` (reading the court-case `document_sources` JSONB
`{document_id, url:[{link, role}]}`). **PR #263 completes the convergence for cases:**
Jawafdehi's old `DocumentSource` rows are projected to full Materials by
`documentsource_to_jsonld` + `build_source_material_iri` (source segment `jawafdehi`), and
a case binds them via **`CaseMaterialReference`** (`material_iri` required + strict, optional
`additional_details`, `ordinal`) — never a local copy. So **one link representation —
`associatedMedia` MediaObjects on a Material — is now the single form everywhere**: upload,
NGM ingestion, the court `document_sources` converter, and the Jawafdehi source→Material
projection all feed it. The `MARKDOWN` + `SOURCE_PAGE` roles used above were added by #263
so no legacy `SourceLinkRole` value is lost.

---

## 4. Search plane — OpenSearch, bilingual FTS over materials

**The FTS machinery is already complete.** The only thing missing is the *feed*.

- **Mapping** (`shared/jawafdehi_shared/search/mappings.py`): the shared index doc has a
  bilingual **`body`** field — `mixed_script` analyzer + `ne`/`en`/`translit` subfields —
  purpose-built for mixed Devanagari/Roman OCR text, plus title/keywords/identifiers.
- **Indexer** (`materials/search_index.py:58-60`): `body` is populated from
  `data["text"]` (schema.org full-text) + `data["description"]`.
- **Query + snippets** (`search/service.py`): `body` is boosted, matched (`best_fields`),
  and **highlighted** for snippets in the `/api/search/` envelope.

**⇒ The one gap:** nothing populates `Material.data["text"]` today. The upload endpoint
stores bytes + a `contentUrl`, but never extracts text. So a material is findable by
title/description but **not by full text**. Closing this is exactly the `material_convert`
job (§5) writing `data["text"]`; everything downstream already indexes and searches it.

**Sizing (estimate).** 1M materials, Devanagari at 3 bytes/char, mixed doc lengths
(≈70% short orders/notices ~1–2pp, ~25% medium charge-sheets ~6pp, ~5% long reports
~50pp) ⇒ **~40 KB extracted text/material ⇒ ~40 GB raw text**. In OpenSearch with the
bilingual dual-analysis + stored `_source` for highlighting: **~60–100 GB primary,
~120–200 GB with one replica**. Court cases add ~10–20 GB. A modest 3-node cluster
handles this; FTS is a small, bounded serving workload — **not** a lake-scale problem.
*Knob:* store full text in `_source` (needed for highlighting; recommended at this size)
vs index-only — the difference between a ~100 GB and ~60 GB index.

**Visibility gate (LANDED in PR #263 — `materials/visibility.py`).** With `DocumentSource`
gone, a Material is the only row, and it lives in the public `ngm` DB. `Material.visibility`
(LISTED / UNLISTED / PRIVATE, default LISTED) is computed as the **MAX over the states of
all referring cases** — a draft case pulls its evidence Material down to PRIVATE. It is the
**draft-leak guard**, honored by three read surfaces: sitemaps/ResourceSync
(`discovery/corpus.py`), the unified-search signal, and the retrieve/list endpoints (anon
sees LISTED; +UNLISTED by direct IRI; PRIVATE authed-only). Recompute fires **live** on case
create / evidence-change / state-transition / soft-delete, over the **union of current +
removed** IRIs (`materials/signals.py` + the write paths); `recompute_all()` is the
init/reconciler backstop (run once post prod-migration). This is a data-plane invariant, not
just a casework detail.

---

## 5. Async plane — the `jobs` queue, and `material_convert` (IMPLEMENTED)

The FTS-feed gap and every other heavy step run as **kinds** on the central Postgres
`jobs` queue (#262, built: claim/lease/reaper/retry/dedup/priority + `/api/jobs/*` +
dashboard; `case_review` ported). No new async mechanism — we register handlers.

- **`material_convert` (the kind that closes the FTS gap) — IMPLEMENTED.** The three seams
  mirror `case_review` exactly and split the same way (server-side hooks in the API;
  worker-side conversion in the consumer, so the heavy conversion deps never import into
  API pods). **All code is in-repo — no dependency on anything outside the repo/worktree.**
  - **enqueue** — `materials/conversion.py::enqueue_material_convert(iri)` is called from
    the upload endpoint after a successful upsert (only for convertible roles
    RAW/ALTERNATE/PERMALINK — not our own MARKDOWN output or SOURCE_PAGE HTML), dedup on
    `material_convert:<iri>`, best-effort so a queue hiccup never fails the upload.
  - **build_payload** (server, at claim) — resolves the source document URL(s) from the
    Material's `associatedMedia` by role preference → hands the DB-free worker
    `{source_urls}`; fails fast if the material is gone or has no convertible source.
  - **worker handler** — `materials/job_handlers.py::handle_material_convert` reuses the
    **in-repo** `review.converter.convert_source` (likhit/MarkItDown — declared deps, with
    download + Devanagari OCR at a Bedrock-safe DPI + on-disk caching + a per-source
    timeout already built in), tries source URLs in order, `on_stage` pings progress,
    returns `{text, source_url}`.
  - **on_result** (server) — `apply_convert_result` stores the markdown in R2 as a
    `linkRole=MARKDOWN` MediaObject (replace-or-append, idempotent) and sets
    `data["text"]`; the `Material` `post_save` signal reindexes into unified search.
  - Registered in `jobs/consumers.py` with **lease 1800s / max_attempts 2**; the poller
    aggregates `materials.job_handlers.HANDLERS` (`review_poller --kinds material_convert`,
    or combined with `case_review`).
  - **No `on_failure`:** a failed convert just leaves `data["text"]` unset — the Material
    is still served + metadata-searchable, and a re-upload re-runs (dedup key freed on
    terminal state).
- **Why this shape:** the write path stays fast (OCR is off-request), and it reuses the
  exact `claim/stage/result` contract the review poller already speaks. `reindex_*` /
  `enrich_ciaa_*` become kinds opportunistically.
  - **Note — enqueue is NOT transactional with the material write.** `Job` lives on the
    `default` DB, `Material` on `ngm` (the router forbids a cross-DB transaction). So the
    enqueue is **best-effort after** the upsert: if it fails, the file is still stored and
    a re-upload re-enqueues (dedup makes that safe). This is *why* the convert is idempotent
    and re-runnable, rather than relying on atomic enqueue. A true exactly-once enqueue
    would need an outbox on the `ngm` DB — deliberately not built (the re-run path covers it).
- **Follow-up:** wire the same `enqueue_material_convert` call into the `/api/ingestion/*`
  batch document path (the interactive upload is done). Provenance on file-bearing
  Materials is now recorded (§3 item 2 / §8 item 2).

---

## 6. Why no lake now — and the seam that keeps it cheap later

The only workload that genuinely wants columnar-scan-over-object-storage is
**cross-domain investigative analytics** (e.g. "asset-declaration wealth jumps in the FY a
linked contractor won a tender", joining assets ↔ courts ↔ procurement ↔ blacklist) plus a
gated ad-hoc `SELECT` plane. That is real to the mission but has **no concrete first query
queued**, and the DB router forbids exactly those joins in Postgres today.

**Decision: defer.** Build archive + serving + search + async first. Keep the lake
*possible* by construction:
- `lakehouse/` stays as a **dormant, tested seam** (DDL/secret builders are real and
  unit-checked; only the live `ATTACH` is stubbed).
- The **`@id` IRI is the universal join key**, so a future lake can join across domains
  the router forbids — the lake becomes precisely "where cross-domain joins legally happen".
- The archive plane's immutable raw + markdown is exactly a **bronze zone** already; a
  future silver derivation reads from it + Postgres without re-scraping.

**Revisit trigger:** a real, recurring cross-domain analytical query, or serving-Postgres
latency pressure from ad-hoc analyst SQL. Until then, Iceberg/DuckDB/Lakekeeper +
compaction cron are operational cost with no serving benefit. (If analytics is later
wanted *cheaply* before a full lake, a Postgres **read replica** for analyst SQL is the
smaller intermediate step.)

---

## 7. Docs reconciled / amended by this doc

- **`ARCHITECTURE.md` §5 ("Storage / lakehouse")** — currently states the lakehouse *is*
  Iceberg + DuckDB + Lakekeeper. **Amend** to: Postgres-SoR serving + R2 archive +
  OpenSearch; Iceberg deferred (`lakehouse/` dormant). The medallion module is a seam, not
  the shipped lake.
- **`lakehouse/medallion.py` + `schema.py`** — their framing that "silver is the source /
  the relational Postgres view is *derived* from silver" is **superseded**: Postgres is
  SoR; if silver ever exists it is *derived from* Postgres + archive, never the reverse.
  The `schema.py` TableSpecs remain a useful **blueprint for future `ngm`-DB Django models**
  (procurement/budget/audit/assets/gazette) — as relational tables, not Iceberg tables.
- **`jawafdehi/adr-cases-own-no-documents.md`** — **incorporated, not amended**, and now
  **implemented + MERGED (PR #263, `712dc2b`)**: `Material.visibility` +
  `materials/visibility.py`, `CaseMaterialReference`, `cases/services/material_resolver.py`
  (`resolve_materials`) + `material_ingest.py`, the `documentsource_to_jsonld` projection,
  `MARKDOWN`/`SOURCE_PAGE` roles, and `news`/`social_media` types are all landed code (822
  unit tests green). It is the document-ownership foundation of this plane: Materials =
  universal doc store; `visibility` mandatory (§4); MARKDOWN linkRole (§3); one
  `material_type` vocabulary. **Follow-ups tracked by #263 (not this doc):** the ~799-row
  prod `DocumentSource`→Material data migration (runbook
  `docs/jawafdehi/sources-to-materials-prod-migration.md`, run separately + `recompute_all()`
  once after); the frontend + MCP "Document Source" terminology purge (ADR D-E, other repos);
  the CIAA content-enrichers left stubbed (`NotImplementedError`) as a separate effort.
- **`jobs-queue-design.md`** — **incorporated.** `material_convert` (its §5 step 4) is the
  data plane's async extraction kind (§5).

---

## 8. Work items (small — most heavy lifting already merged)

Ordered by value/risk. None is a rewrite.

1. ✅ **DONE — Close the FTS feed (highest value).** The `material_convert` kind
   (`materials/conversion.py` + `materials/job_handlers.py`, registered in
   `jobs/consumers.py`, enqueued from the upload endpoint) does bytes → OCR markdown → R2
   (`linkRole=MARKDOWN`) → `Material.data["text"]` → reindex. 14 unit tests + suite green;
   `manage.py check` clean; ruff clean. *Delivers full-text search over materials end to
   end.* Remaining thread: also enqueue from `/api/ingestion/documents` (§5 follow-up).
2. ✅ **DONE — Formalize archive provenance.** `materials/provenance.py` — a
   `jawafdehi:provenance` struct embedded on each file-bearing MediaObject +
   content-hash idempotency (`attach_media_object` dedups on `(role, sha256)`).
   Wired into the upload endpoint (`fetch_method=upload`) and `material_convert`
   `on_result` (`fetch_method=ocr`, `ocr_engine=likhit`). *Remaining thread:*
   material-specific R2 prefix (needs a storage-backend prefix override) + provenance
   on the `/api/ingestion/documents` court `document_sources` path (scraper-supplied).
3. ✅ **DONE — Reconcile the docs (§7).** `ARCHITECTURE.md §5` rewritten to Postgres-SoR /
   lakehouse-lite; `lakehouse/__init__.py` bannered DORMANT + blueprint (supersedes the
   "silver is SoR" framing in `medallion.py`/`schema.py`).
4. **Governance-domain tables (when sourced):** turn the `schema.py` specs
   (procurement/budget/audit/assets/gazette) into `ngm`-DB Django models — additive, only
   when data for them is actually acquired.

**Already landed elsewhere (was planned here — do not re-scope):**
- `Material.visibility` + the draft-leak gate across search/sitemaps/retrieve (PR #263, §4).
- The single link representation (`associatedMedia`) + `CaseMaterialReference` binds + the
  source→Material projection (PR #263, §3.1).
- The async queue itself — claim/lease/retry/dedup/`/api/jobs/*` (#262).

**Related follow-up owned elsewhere:** the ~799-row prod `DocumentSource`→Material data
migration (PR #263 runbook), and the frontend/MCP terminology purge (ADR D-E).

## 9. Out of scope

- Iceberg/DuckDB/Lakekeeper, compaction cron, silver/gold Iceberg zones (§6 — deferred).
- Merging the 3 Postgres DBs into 1 (separate project; router keeps them isolated).
- A broker for the async plane (jobs-queue-design §0 — Postgres queue, no Celery/Redis).
- Any second copy of the object bytes; the archive holds one copy, referenced by URL.
