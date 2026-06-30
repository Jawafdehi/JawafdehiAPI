# Unified Search Across the Platform — Design Doc

Status: **BUILT & LIVE** (2026-06-28). This is the as-designed AND as-shipped doc for
unified search. §0 captures the **pre-build starting point** (now partly superseded —
see the note in §0); §1-§4 describe the live system; §5-§8 have been reconciled with
the locked decisions at the top (the dropped "internal" role and the reversed
graceful-degradation fallback that earlier drafts carried are removed below). Current
platform state: [`ARCHITECTURE.md`](./ARCHITECTURE.md) §3.
Author: platform
Date: 2026-06-28

## ROLE MODEL REDEFINED (2026-06-28) — replaces the "internal" role idea

Platform roles (Zitadel → Django groups; the "internal" role is NOT needed):
- **admin** = Admin **and** `is_superuser` (admins are Django superusers).
- **moderator** = Moderator.
- **caseworker** = the role formerly called **contributor**, RENAMED for casework
  purpose. (contributor → caseworker everywhere: role key, group, predicates.)
- **readonly** = all read-only permissions **including casework** (can view
  casework, incl. draft/in-review cases) — this is what covers the old "internal".
- **public** = readonly **except NO casework access** (public reads only; cannot
  see casework / draft / in-review).
Draft/in-review case visibility in the APP: readonly+ (readonly, caseworker,
moderator, admin); public cannot. Search index is all-public (drafts not indexed),
so this is purely an app-side detail-API concern.

Also locked: court-case IRI scheme = **`https://jawafdehi.org/courtcase/<court>/<case_number>`**;
transliteration = **single shared ownership** (one impl in `jawafdehi_shared`);
**relevance-weight tuning DEFERRED to future**; **case_id removal ran in PARALLEL
with the search build and is DONE** (migration `0038_remove_case_case_id`).

## DECISIONS LOCKED (2026-06-28) — supersede the original open questions

These user decisions are now binding; §8 open questions are resolved accordingly.

1. **Jawafdehi case IRI = `https://jawafdehi.org/case/<case-slug>`.** Cases get a
   first-class @id IRI built from the case **slug** (human-readable, already exists).
   **Drop the `case_id` field entirely** — internal references use the case **number**,
   external/public references use the case **slug**. The case-id becomes public (it's
   the IRI). IRI is **minted at PUBLISH** — drafts/in-review have no public IRI.
2. **Court cases get IRIs too.** Synthesize a court-case @id IRI (material-style, e.g.
   `https://jawafdehi.org/courtcase/<court>/<case_number>` or via the material IRI
   scheme) so they are first-class, indexable, and linkable.
3. **Split indices: `ngm-materials` + `ngm-courtcases`** (the doc already assumed this —
   now confirmed and shipped as the `MATERIAL_INDEX`/`COURTCASE_INDEX` constants).
4. **Court cases ARE in the unified result set** (type `courtcase`).
5. **OpenSearch is a ONE-WAY CUTOVER — NO fallback search.** Drop Option-C fallback
   and any degraded-mode in-process search. **OpenSearch degraded = service degraded**
   (search returns an error/unavailable, not a silent partial result). The Jawafdehi
   `UnifiedSearchView` in-process search and the NGM `SearchView` 501 stub were
   **removed** in the cutover. The NES `EntityRepository.search_entities()` SQL path
   was **intentionally RETAINED**, not deleted: it still backs the NES entity
   list/search endpoint (`GET /api/entities`) and is **not** the platform unified
   search. (Envelope/ACL *logic* is reused as a reference for the indexers; only the
   two redundant in-process *search* paths were removed.)
6. **Draft/in-review cases are NOT indexed at all.** Only PUBLISHED cases enter
   `jawafdehi-cases`. So the search ACL is simple: the index contains only public docs.
   Draft/in-review visibility is handled OUTSIDE search (the case detail API), via the
   **readonly+ roles** (readonly/caseworker/moderator/admin can view draft/in-review
   cases in the app; public cannot). No `visibility=DRAFT/IN_REVIEW` documents exist in
   the index → no ACL-leak surface in search.
7. **Self-hosted OpenSearch**, creds via **env vars** (assume provided: URL + auth).
   No managed-service assumption.
8. **The unified @id envelope DRIVES ResourceSync + Sitemaps** — replace the existing
   custom sitemap logic; the same IRI/JSON-LD projection feeds search, sitemaps, and
   ResourceSync.
9. **Bilingual (English + Nepali) is required** — a dedicated deep-research run
   (`wf_37e93f4d-9ed`) is finding the "golden" OpenSearch bilingual config; its result
   updates §3.4 when it lands.

## 0. Context

The three former services are now ONE Django monolith
(`/damodaha-volunteer/jawafdehi-platform`, project `monolith/config`) with three
apps over three databases through a router (`monolith/config/db_router.py`):

| Domain | Apps | DB | Canonical record | @id IRI |
|---|---|---|---|---|
| NES entities | `nes_service.entities` | `nes` | `StoredEntity` (schema.org JSON-LD in `data` JSONB, PK `iri`) | `https://jawafdehi.org/entity/<prefix>/<slug>` |
| NGM materials | `ngm_service.materials` | `ngm` | `Material` (schema.org JSON-LD in `data`, PK `iri`) | `https://jawafdehi.org/material/<source>/<ident>` |
| NGM court data | `ngm_service.courts` | `ngm` | `CourtCase` (composite PK, `nes_id` link), `CaseEntity`, `Court`, `CourtCaseHearing`, `BlacklistedFirm` | links via `nes_id` |
| Jawafdehi cases | `cases`, `case_workflows`, `review` | `default` | `Case`, `DocumentSource` (`related_entities` = array of IRIs) | n/a (cases are local) |

There is no cross-DB SQL: the router pins each app's queries to its own DB, and
cross-app reads are done in-process in Python (e.g. resolve an entity IRI by
calling NES). The canonical join key everywhere is the schema.org `@id` IRI
(entity + material), defined/validated in
`shared/jawafdehi_shared/entities/ids.py`.

### Existing search building blocks (the pre-build starting point)

> NOTE: this section captures the **pre-build starting point** as it stood when
> this doc was first written; several of these blocks have since been removed or
> superseded by the shipped build (past-tense claims below mark what changed).

- **Jawafdehi unified-ish search** — *was* `services/jawafdehi/cases/services/search.py`,
  `UnifiedSearchService.search()`, exposed at `GET /api/search/`
  (`cases.api_views.UnifiedSearchView`, mounted in `cases/urls.py`). It already
  searched cases + entities + documents *within Jawafdehi scope*, returning a
  paginated envelope with `results[]`, `counts`, and `facets`; all ORM + Python
  scoring, no OpenSearch. It was the closest thing to a model envelope. **REMOVED
  in the cutover** — the file is gone and `cases/api_views.py` only carries a note
  recording its removal; the platform unified search now lives in the `search` app.
- **NES entity search** — `services/nes/nes_service/entities/persistence.py`,
  `EntityRepository.search_entities(query, entity_type, prefix, keywords, limit, offset)`.
  SQL filters on promoted columns (`entity_type`, `prefix`) + JSONB `keywords`
  containment, then Python relevance scoring over `name`/`alternateName`/`keywords`
  on a bounded candidate window (`MAX_SEARCH_CANDIDATES=5000`). No transliteration.
  (This path was intentionally **RETAINED** — it still serves the NES entity
  list/search endpoint; see §2 and §7.)
- **NGM search** — *was* a `SearchView` 501 stub in
  `services/ngm/ngm_service/courts/views.py` ("No search substrate wired yet
  (planned: shared OpenSearch across NES + NGM)"). **REMOVED in the cutover** —
  `courts/views.py` now only carries a note recording the removal.
- **Shared OpenSearch helper** — `shared/jawafdehi_shared/search/opensearch.py`:
  `get_opensearch_url()` (env `OPENSEARCH_URL`, default `http://localhost:9200`),
  `make_client()` (lazy `opensearch-py`), and canonical index-name constants
  `ENTITY_INDEX = "nes-entities"`, `MATERIAL_INDEX = "ngm-materials"`,
  `COURTCASE_INDEX = "ngm-courtcases"`, `CASE_INDEX = "jawafdehi-cases"`. The
  bilingual document-builder + EN<->Devanagari transliteration was noted as
  living with NES but **had not yet been written** at this point — it now **EXISTS**
  at `shared/jawafdehi_shared/search/transliterate.py`.
- **OpenSearch in the stack** — `docker-compose.yml`:
  `opensearchproject/opensearch:2.13.0`, single-node, security disabled (dev),
  the platform service already receives `OPENSEARCH_URL=http://opensearch:9200`.
- **No index/reindex management commands existed yet** at this point. (Five now
  ship: `reindex_entities`, `reindex_materials`, `reindex_courtcases`,
  `reindex_cases`, and the umbrella `reindex_all`.)

### The prior decisions this doc inherits

- Shared search substrate = **OpenSearch**, **bilingual** (Devanagari + Roman) via
  **index-time transliteration** (the NES PR-91 approach).
- Canonical cross-domain join key = schema.org `@id` IRIs.
- A just-completed standards deep-research report
  (`/tmp/claude-22285952/-damodaha-volunteer/621d7695-.../tasks/wjopjajsd.output`):
  adopt schema.org/JSON-LD + Sitemaps + ResourceSync; IIIF is overkill for a
  PDF/OCR repo; schema.org `CollectionPage` has **no native pagination**;
  bilingual tagging, Bikram Sambat dates, roled links, and OCR exposure have **no
  standard answer** and stay custom. The NGM tree-based R2 index is KEPT
  (re-expressed as JSON-LD), and the frontend does not crawl it.

---

## 1. Goal & Scope

**Goal:** one platform search endpoint that, for a single query, returns a single
ranked, typed, bilingual result set drawn from all three domains:

- **entities** (NES — people, organizations, locations),
- **materials / court-cases** (NGM — court orders, CIAA reports, laws/bills, OCR'd
  documents, and `CourtCase` records),
- **accountability cases** (Jawafdehi).

**What "unified" means here:**

- *One query in* (`?q=`), *mixed result types out*, under **one common result
  envelope** — every hit carries `type`, `@id` (IRI where one exists), bilingual
  `title`, `snippet`, `source_app`, and `score`, regardless of which domain it
  came from.
- Results are interleaved and ranked across types, with per-type counts/facets so
  a caller can show "23 entities, 11 cases, 140 materials."
- Bilingual: a Devanagari query matches Roman records and vice versa (index-time
  transliteration), and bilingual titles/snippets are returned per language.

**What "unified" is NOT:**

- **Not** a cross-DB SQL join. The three DBs stay separate; we never JOIN across
  `nes`/`ngm`/`default`. Unification happens in the **search index** (and in a thin
  merge/serialize layer for live reads), not in Postgres.
- **Not** a replacement for the per-app *detail* APIs. Search returns lightweight
  hits; clients follow `@id` / `api_url` to the owning app for the full record.
- **Not** a new source of truth. The three DBs remain authoritative; the index is a
  derived, best-effort projection.

---

## 2. Architecture Options & Recommendation

### Option A — OpenSearch as the unified index (RECOMMENDED)

Each app projects its records into shared OpenSearch indices
(`nes-entities`, `ngm-materials`, `ngm-courtcases`, and `jawafdehi-cases`)
using a common, schema.org-flavored mapping with bilingual analyzers + index-time
transliteration. One **search service** (in a new `search` app) queries across the
indices with a single multi-index query, merges and re-ranks, and serializes the
common envelope.

- **Pros:** aligns with the prior OpenSearch + bilingual-transliteration decision
  and the wired `opensearch.py`/compose stack; real relevance scoring (BM25)
  across heterogeneous types; bilingual analysis done once at index time;
  highlighting/snippets for free; scales as NGM materials grow; cross-type ranking
  is a single query, not N Python merges.
- **Cons:** index must be kept in sync (drift risk); new indexing pipeline + ops
  surface (cluster health, reindex); relevance tuning across very different doc
  shapes is non-trivial.

### Option B — In-process fan-out, no OpenSearch

The `search` service calls each app's existing search in-process
(`UnifiedSearchService`-style for cases, `search_entities()` for NES, a new ORM
search for NGM), then merges and re-ranks in Python.

- **Pros:** no index to sync, no new infra, DB is the only source of truth, fastest
  to ship.
- **Cons:** no real cross-type relevance (each app scores on its own scale, so the
  Python merge is heuristic); bilingual matching would have to be re-implemented per
  app at query time (expensive, inconsistent); poor at scale for NGM materials +
  OCR text; duplicates the per-app scoring logic; contradicts the prior substrate
  decision (the 501 stub literally says it's waiting for OpenSearch).

### Option C — Hybrid (OpenSearch for entities+materials, in-process for cases)

OpenSearch for the large, bilingual, mostly-public corpora (NES entities, NGM
materials/court-data); cases stay in the in-process Jawafdehi search and are merged
in at query time.

- **Pros:** smaller initial indexing scope; cases (lower volume, permission-heavy
  with DRAFT/IN_REVIEW visibility) keep their existing tested DB-side ACL logic.
- **Cons:** two ranking systems to reconcile (OpenSearch BM25 vs Jawafdehi's Python
  weights), so cross-type ordering between cases and the rest is approximate; two
  code paths to maintain; bilingual query behaves differently for cases vs the rest.

### Recommendation: **Option A — OpenSearch, one-way cutover (no fallback).**

Adopt OpenSearch as THE unified search for all three domains. Per decision #5 this
is a **one-way cutoff**: there is **no fallback search** and **no degraded in-process
mode**. If OpenSearch is unavailable, search is unavailable (`503`), full stop —
"OpenSearch degraded = service degraded." This is simpler and more honest than a
half-working Python fallback that ranks differently.

Consequences:
- Option B and Option C (and the "phased like C" migration) are **rejected** — there
  is no in-process merge path to keep.
- The two redundant per-app *search* paths are **removed** once cut over: the
  Jawafdehi `UnifiedSearchView` in-process search and the NGM 501 stub both go away
  (replaced by the OpenSearch-backed `search` app). The NES
  `EntityRepository.search_entities()` SQL/relevance path is **retained** — it still
  backs the NES entity list/search endpoint (`GET /api/entities`), which is a
  per-app detail/list API, not the platform unified search. Their *envelope/field*
  logic is reused as a reference when writing the indexers, but the two redundant
  search paths are deleted, not kept as fallback.
- Indexing order is still incremental for delivery (entities + materials first, then
  courtcases, then published cases), but the API only goes live once its indices are
  populated — it never silently falls back to a DB search.

---

## 3. Index Design

### 3.1 Indices

| Index | Owner app | Source records | Index-name constant |
|---|---|---|---|
| `nes-entities` | `nes_service.entities` | `StoredEntity` (JSON-LD) | `ENTITY_INDEX` (exists) |
| `ngm-materials` | `ngm_service.materials` | `Material` (JSON-LD) | `MATERIAL_INDEX` (exists) |
| `ngm-courtcases` | `ngm_service.courts` | `CourtCase` (+ parties) | `COURTCASE_INDEX` (exists) |
| `jawafdehi-cases` | `cases` | `Case` (published only) | `CASE_INDEX` (exists) |

Keep one index per modality (matching the existing constants) rather than a single
mega-index: mappings differ, lifecycle differs (cases have ACL/visibility, NGM is
mostly public), and per-index reindex is cheaper. The search service queries all of
them in one multi-index request.

NGM court cases and materials are split into two indices because court cases have no
`@id` IRI of their own (composite PK `(case_number, court)` + `nes_id` links),
whereas materials are IRI-keyed JSON-LD; mixing them would muddy the mapping.

### 3.2 Common result envelope (returned by the API, produced from any index)

```
{
  "type":        "entity" | "material" | "courtcase" | "case",
  "id":          "<@id IRI for entity/material; local id/slug for courtcase/case>",
  "source_app":  "nes" | "ngm" | "jawafdehi",
  "title":       { "ne": "...", "en": "..." },   # bilingual; may have one side
  "snippet":     { "ne": "...", "en": "..." },   # highlighted excerpt
  "score":       <float>,                          # normalized 0..1 for merge
  "url":         "<frontend URL>",
  "api_url":     "<owning-app detail API>",
  "matched_fields": ["name", "keywords", ...],
  "extra":       { ...type-specific (entity_type, case_type, material_type, date) }
}
```

This is a superset-compatible evolution of the shape `UnifiedSearchService` already
returns (`result_type`, `id`, `title`, `description`, `url`, `api_url`,
`matched_fields`, `score`), so the Jawafdehi frontend's expectations are preserved;
the additions are `source_app`, bilingual `title`/`snippet` objects, and `type`
values for the new domains.

### 3.3 Common indexed document (the schema.org JSON-LD -> index mapping)

Every index shares a common core set of fields so a single query can hit them all.
Each app's indexer maps its JSON-LD/record into:

| Index field | Source (JSON-LD / model) | Mapping / analyzer |
|---|---|---|
| `iri` | `@id` (entity/material); synthesized `app:type:id` for courtcase/case | `keyword` (exact join/dedup) |
| `type` / `schema_type` | `@type` | `keyword` |
| `source_app` | constant per indexer | `keyword` |
| `title_ne`, `title_en` | `name` language map (entity/material), `Case.title` | `text`, bilingual analyzer |
| `title_translit` | transliteration of title (both directions) | `text`, transliteration analyzer |
| `body` | `text`/`description` (material), `description`+`key_allegations` (case), relationship details (entity) | `text`, bilingual analyzer |
| `keywords` | `keywords[]` (JSON-LD), `tags` (case) | `keyword` + `text` |
| `identifiers` | court `case_number`, material `ident`, `nes_id` links | `keyword` |
| `created_at`, `updated_at`, `date` | promoted columns / JSON-LD dates (+ Bikram Sambat string carried verbatim, see 6) | `date` for ISO; `keyword` for BS |
| `raw` | the full JSON-LD / serialized record | `object`, `enabled:false` (return-only) |

### 3.4 Bilingual analyzer / transliteration (reuse PR-91)

- Two named analyzers configured at index creation: a **Devanagari** analyzer and a
  **Roman** analyzer; titles/body are indexed under both via multi-fields.
- **Index-time transliteration** (the PR-91 approach owned by NES): each Devanagari
  token is also emitted as its Roman transliteration into `*_translit`, and Roman
  input likewise gets a Devanagari form, so a query in either script matches records
  stored in the other. The transliteration utility lives in NES (per the
  `opensearch.py` docstring) and is shared with the NGM and case indexers.
- Query-time: the query string is analyzed with the same bilingual chain and run as a
  `multi_match` across `title_ne`, `title_en`, `title_translit`, `body`, `keywords`,
  with field boosts (title > keywords > body).
- Keyword/tag/identifier search: exact-match `term`/`terms` queries on `keywords`,
  `identifiers`, `type` (so `case_number`, IRI fragments, and tags are findable
  precisely, mirroring NES's current `keywords` containment behavior).

---

## 4. Indexing Pipeline

Principle: **DB is source of truth; the index is a best-effort derived projection.**
Indexing failures must never break a write.

### 4.1 Live (on-write) indexing

- Hook into the existing publication/ingest paths and Django **signals**
  (`post_save`/`post_delete`) for `StoredEntity`, `Material`, `CourtCase`, and
  `Case`. The NGM ingest pipeline and NES change-submission path already have
  natural commit points to enqueue an index update.
- Each indexer builds the index doc from the record's JSON-LD (entities/materials
  already store JSON-LD verbatim; courtcases/cases are serialized by the indexer)
  and upserts it by `iri`/synthesized id.
- **Best-effort:** index calls are wrapped so any OpenSearch error is logged and
  swallowed (or pushed to a retry queue) — the originating DB transaction commits
  regardless. Prefer `transaction.on_commit` so we never index a row that rolled
  back.
- Case state transitions are handled by add/remove, not a `visibility` field: the
  case indexer upserts a case only while it is PUBLISHED and removes it from the
  index when it leaves PUBLISHED state (the index is all-public; see §5).

### 4.2 Bulk reindex (management commands)

- New per-app management commands (none exist today), e.g.
  `reindex_entities`, `reindex_materials`, `reindex_courtcases`, `reindex_cases`,
  plus an umbrella `reindex_all`. Each:
  - creates the index with the bilingual mapping/analyzers if missing,
  - bulk-streams records from its DB (respecting the router) through its indexer,
  - supports `--since` (incremental) and full rebuild into a new index with an
    alias swap (zero-downtime reindex).
- Used for first population, mapping/analyzer changes, and drift repair.

### 4.3 Per-app indexers

One indexer module per owning app (so each app owns its own projection logic and the
`search` app stays domain-agnostic): `nes_service.entities` -> `nes-entities`,
`ngm_service.materials` -> `ngm-materials`, `ngm_service.courts` -> `ngm-courtcases`,
`cases` -> `jawafdehi-cases`. They share the bilingual transliteration helper and the
common-field contract from section 3.3.

### 4.4 Source of truth & dedup

The `iri`/synthesized-id is the document `_id`, so re-indexing is idempotent and the
same logical record can't appear twice. Cross-domain dedup (e.g. an entity that is
also a court party via `nes_id`) is left as distinct typed hits — they are different
result *types*, intentionally.

---

## 5. The API

**Endpoint:** `GET /api/search?q=&type=&lang=&page=&page_size=` — one platform-wide
endpoint.

**Where it is mounted:** search spans all three apps, so it belongs to neither NES,
NGM, nor cases. Create a **new `search` app** in the monolith
(`monolith/config/INSTALLED_APPS`) that owns the unified query/merge service and the
view, mounted at `/api/search/` in `monolith/config/urls.py`. This supersedes the
Jawafdehi-scoped `/api/search/` (`cases.UnifiedSearchView`) and the NGM 501 stub at
`/ngm/api/search/` (Phase 5 retires both). The `search` app depends on the per-app
indexers but not vice versa.

**Parameters:**
- `q` — query string (required; bilingual, analyzed with the transliteration chain).
- `type` — optional filter: `entity`, `material`, `courtcase`, `case` (repeatable).
- `lang` — `ne` | `en` | `both` (default `both`); controls which side of the
  bilingual title/snippet is emphasized and returned.
- `page`, `page_size` — pagination (see below; `page_size` capped, e.g. 50, matching
  the existing service's cap).

**Auth (simplified by decision #6):** the index contains ONLY public documents —
entities, materials, court cases, and **PUBLISHED** cases. Draft/in-review cases are
**never indexed**, so search has **no ACL/visibility filter and no leak surface**:
everything in the index is public, search is fully public-read. Draft/in-review case
visibility is an **app concern, not a search concern** — handled in the case detail
API via the **readonly+ roles** (readonly/caseworker/moderator/admin can view
draft/in-review cases in the app; public cannot). The `visibility` index field is therefore dropped
(unnecessary — all indexed cases are PUBLISHED); the case indexer simply skips
non-PUBLISHED cases and removes a case from the index if it leaves PUBLISHED state.

**Pagination:** the research is explicit that schema.org `CollectionPage` has **no
native pagination** — so pagination is **custom glue**, not a standard. Use
offset/page for the UI (`page`/`page_size`) and expose an optional **cursor**
(`search_after` on `(score, iri)`) for deep, stable paging over large NGM result
sets. The envelope carries `count`, `counts` per type, and `next`/`prev` cursor
tokens.

**Ranking / merge across types:** a single multi-index `multi_match` query returns
hits already scored by BM25 on a comparable scale (one analyzer chain, shared field
boosts), so cross-type ordering is the OpenSearch score — no Python re-merge of N
separately-scored lists (Option B's weakness). Light per-type boosts
(configurable, e.g. nudge exact identifier/IRI matches and entities up) are applied
as query-time `function_score`/boosts, then results are serialized into the common
envelope. Facets (`counts` by `type`, plus `entity_type`/`case_type`/`material_type`
aggregations) come from OpenSearch aggregations, replacing the Python facet counting
in the current service.

---

## 6. Standards Alignment

From the deep-research report (sources: Google Search Central, schema.org, IIIF/W3C/
NISO specs; current 2024-2026):

**What we lean on / expose as standards:**
- **schema.org JSON-LD** is the cataloguing + discoverability layer and is already
  how NES entities and NGM materials are stored (`StoredEntity.data`,
  `Material.data`). Search **results** expose the schema.org JSON-LD of each hit
  (the `raw` return-only field / `@id`), so the same vocabulary the corpus is
  modeled in is what search returns. (Confirmed: schema.org structured data is the
  sanctioned content-understanding/SEO layer; `Dataset`/`DataCatalog`/`DataDownload`
  for the repository, `CreativeWork`/`Manuscript` for materials.)
- **Sitemaps + ResourceSync** remain the crawl/discovery + harvest layer
  (complementary to schema.org, not replaced by it) — orthogonal to this search
  endpoint but the same JSON-LD projection feeds both. Search does **not** try to be
  the catalogue/harvest surface; the NGM JSON-LD tree (the re-expressed R2 index) is
  kept for that and the frontend does not crawl it.

**What stays CUSTOM (no standards answer — per the research caveats):**
- **Bilingual Devanagari/Roman** parallel records: beyond generic BCP-47 `@language`
  tags, no standard models parallel bilingual catalogue records or cross-script
  matching. Our index-time transliteration + dual analyzers (section 3.4) are
  bespoke. (Research: bilingual tagging has no first-class standard answer.)
- **Bikram Sambat (non-Gregorian) dates:** schema.org/IIIF date fields assume ISO
  8601/Gregorian. BS is carried as a custom verbatim string field in the index
  alongside the ISO date; we never coerce BS into a `date` mapping. (Research:
  confirmed no provision for BS; offset isn't even constant.)
- **Roled source-links** (RAW/ALTERNATE/PERMALINK/...): schema.org `sameAs`/`url` and
  IIIF `rendering` are flat and role-less. The roled-links model stays custom and is
  carried as structured `extra`/`raw` data, not flattened to a standard link.
- **OCR / full-text exposure:** no reviewed standard specifies OCR/full-text delivery
  (IIIF Content Search is image-region oriented + needs a live server). Full-text /
  OCR `body` is indexed into OpenSearch (our own substrate) — that's the custom
  answer, and it's a positive reason to own the index.
- **Deep-tree pagination:** `CollectionPage` has no pagination; our cursor/offset
  glue (section 5) is custom by necessity.

**Explicitly NOT adopting:** IIIF — overkill for a PDF/OCR-download repository with no
page-image tiles (research: IIIF viewers can't render born-digital files natively and
the tooling presupposes image tiles). Search has no IIIF surface.

---

## 7. Phasing & Risks

**Phasing:**

1. **Index infra** — extend `shared/jawafdehi_shared/search/opensearch.py` with the
   bilingual analyzer/mapping definitions + index-create helpers; the four shipped
   index-name constants (`ENTITY_INDEX`, `MATERIAL_INDEX`, `COURTCASE_INDEX`,
   `CASE_INDEX`); add OpenSearch settings keys to `monolith/config/settings.py`.
   (Stack + client already exist.)
2. **Per-app indexers + bulk reindex commands** — build the four indexers and
   `reindex_*` commands; populate `nes-entities` and `ngm-materials` first (stable
   IRIs + JSON-LD already), then `ngm-courtcases`, then `jawafdehi-cases` (PUBLISHED
   only). Verify against NES's current `search_entities` results.
3. **Unified query/merge service** — new `search` app: multi-index query, bilingual
   query analysis, ACL filter, ranking/merge, common-envelope serializer, facets via
   aggregations.
4. **The API** — mount `GET /api/search/` from the `search` app; wire live
   (signal-based, best-effort) indexing into the ingest/publication paths.
5. **Retire the redundant per-app searches** — once the indices are trusted, the two
   redundant *search* paths are **deleted** (per decision #5, no fallback kept): the
   Jawafdehi `UnifiedSearchView` in-process search and the NGM 501 stub go away,
   replaced by the OpenSearch-backed `search` app at `/api/search/`. The NES
   `EntityRepository.search_entities()` SQL/relevance path is **retained** (it still
   backs the per-app entity list/search endpoint `GET /api/entities`, not the platform
   unified search). The removed paths' envelope/field logic is reused as a reference
   for the indexers, not kept as a runtime path.

**Risks:**
- **Index drift** — DB and index diverge if live indexing silently fails. Mitigate:
  `on_commit` indexing, dead-letter/retry on errors, scheduled incremental
  `reindex_* --since`, and a periodic count/checksum reconciliation per index.
- **Bilingual quality** — transliteration is heuristic; bad transliteration -> missed
  or noisy matches. Mitigate: reuse the validated PR-91 NES transliteration, build a
  bilingual relevance test corpus, tune analyzers against it before retiring per-app
  search.
- **Relevance tuning across heterogeneous types** — an entity name, a 40-page OCR'd
  court order, and a case summary score very differently. Mitigate: per-field boosts,
  per-type `function_score` boosts, normalized envelope `score`, and an evaluation
  harness on representative queries before changing defaults.
- **ACL correctness for cases** — a stale `visibility` field could leak a DRAFT.
  Mitigate: re-index on every state transition; apply the ACL filter at query time as
  a hard `filter` (not a boost); mirror the exact Jawafdehi predicates.
- **Operational surface** — OpenSearch becomes a **hard** prod dependency (per
  decision #5 there is NO fallback: cluster down = `503`, search unavailable). Needs
  prod sizing, auth/TLS, and backups so the cluster stays up — the mitigation is
  availability, not a degraded in-process mode (that path was deliberately removed).

---

## 8. Open Questions

RESOLVED by the locked decisions (see top): index split (#3), court cases in scope +
IRIs (#2,#1-courtcase), case ACL/draft handling (#6), degraded mode = hard-fail (#5),
prod posture = self-hosted + env creds (#7), envelope drives ResourceSync/Sitemaps
(#8), bilingual config (deep research in flight, #9). Also **DONE** (not just locked):
**`Case.case_id` removal** (decision #1) — the legacy `case_id` column was dropped via
migration `0038_remove_case_case_id`; `cases/models.py` records the column as DROPPED.
Genuinely remaining:

1. **Court-case IRI scheme:** `https://jawafdehi.org/courtcase/<court>/<case_number>`
   vs reusing the material IRI scheme (`/material/court/<court>/<case_number>`). Pick
   one and add to `shared/jawafdehi_shared/entities/ids.py`. (Court cases still have a
   composite PK in Postgres; the IRI is an added stable external identifier.)
2. **Transliteration ownership:** the PR-91 transliteration lives in NES — generalize
   it into `shared/jawafdehi_shared/search/` so all indexers use one implementation?
   (Pending the bilingual deep-research result, which may change the approach entirely.)
3. **Relevance weights:** initial cross-type boost defaults (entities-first?
   identifier-exact-first?) and who owns tuning them.
4. ~~**The "internal" role mechanics.**~~ RESOLVED — the "internal" role idea was
   dropped. Draft/in-review view is granted to the **readonly+ roles** (readonly/
   caseworker/moderator/admin) via the shared role→group map
   (`jawafdehi_shared/auth/oidc.py` `DEFAULT_ROLE_TO_GROUP`) and the case detail API
   predicate. (App-side, not search.)
5. ~~**case_id field removal.**~~ DONE — `Case.case_id` was dropped via migration
   `0038_remove_case_case_id` (see the resolved list above); internal refs use the case
   number, external refs use the slug.
