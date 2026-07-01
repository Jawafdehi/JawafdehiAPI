# DOC-STATUS — Documentation Audit & Orientation Index

_Last audited: 2026-06-30._

**Read this first.** This file is the orientation index for the `docs/` tree: it says,
doc by doc, what to trust and what to treat with caution. For *what the platform is now*
read [`ARCHITECTURE.md`](./ARCHITECTURE.md).

> **History note.** The program first designed **3 microservices over REST** (FastAPI
> NES/NGM, separate repos) and then **reversed to a single Django monolith**. Docs (or
> sections) describing the old shape have been removed or bannered. If a doc mentions
> FastAPI, separate repos, REST between services, or the `entity:<prefix>/<slug>` id
> scheme, it is describing a dead era — check the status table below.

## What the platform ACTUALLY is now (the ground truth)

- **Single Django MONOLITH** — one project (trunk `main`; `v2` retired 2026-06-29). Apps:
  `nes_service`, `ngm_service`, `cases`/`case_workflows`/`review`, plus monolith `search`
  + `discovery` apps. **3 Postgres DBs via a DB router** (`monolith/config/db_router.py`);
  **in-process inter-app calls — NO REST between services**. One image. **uv workspace**
  (NOT poetry). Runs at `:48000`.
- **schema.org JSON-LD is the canonical stored form** for NES entities AND NGM materials,
  keyed by **`@id` IRIs**: `https://jawafdehi.org/entity/<prefix>/<slug>`,
  `/material/<source>/<ident>`, `/case/<slug>`, `/courtcase/<court>/<case_number>`. **Clean
  slate — the legacy `entity:<prefix>/<slug>` id scheme is GONE.** Per-type Pydantic models
  DELETED in NES.
- **Unified OpenSearch search** (bilingual EN+Nepali, analysis-icu, indic-transliteration)
  replaced ALL per-app searches. **Hard dependency** (down = 503, no fallback). Drives
  ResourceSync + Sitemaps off the `@id` envelope. (The NES `EntityRepository.search_entities()`
  SQL path was retained to back the NES entity list endpoint `GET /api/entities` — it is not
  the platform unified search.)
- **OIDC/Zitadel only**; DRF tokens dropped. Roles: **admin / moderator / caseworker**
  (renamed from "contributor") **/ readonly / public**.
- **JawafEntity collapsed** into `CaseEntityRelationship` (holds `nes_id` IRI bind).
  `Case.case_id` field dropped (slug = internal+external id; migration 0038).
- The data-migration **runner was dropped** for a **bulk-ingest** path; ≥2-source HOLD gate
  is real; public-only with the NGM plaintiff/defendant privacy carve-out.
- Builds + boots + proven live; full unit suite ~1057 pass; integration tests repointed to
  one host `:48000`.
- **Real sourcing is underway** — current live-DB counts and per-bucket coverage are tracked
  canonically in [`nes/sourcing/SOURCED-INDEX.md`](./nes/sourcing/SOURCED-INDEX.md); held
  composition + promotability in [`nes/sourcing/HELD-PROMOTION-ANALYSIS.md`](./nes/sourcing/HELD-PROMOTION-ANALYSIS.md).

## Classification legend

- **CURRENT** — accurate against the ground truth above.
- **STALE** — partly outdated; specific wrong claims listed.
- **HISTORICAL** — a point-in-time record worth keeping as-is (run logs, sourcing results,
  executed plans).

A `>` status banner has been added in-place to the most-misleading STALE docs (marked
**[bannered]** below).

---

## Status table

### Top-level orientation

| Doc | Class | Notes | Disposition |
|---|---|---|---|
| `README.md` | CURRENT | Docs front door; points at ARCHITECTURE.md + here | Keep. |
| `ARCHITECTURE.md` | CURRENT | **Single source of truth** for the current platform (monolith, 3 DBs/router, schema.org/IRI, unified OpenSearch, OIDC) | Keep. |
| `DOC-STATUS.md` (this file) | CURRENT | The orientation index / per-doc audit | Keep. |
| `unified-search-plan.md` | CURRENT (BUILT & LIVE) | §0 = pre-build starting point (marked as such); §1-§8 reconciled to the live build (hard-fail no-fallback, four index constants, case_id done, no "internal" role, no `visibility` field) | Keep. |
| `case-workflows-retirement-plan.md` | CURRENT | Live/actionable plan to retire `case_workflows` + drop the langchain/langgraph/deepagents stack; uses live monolith + uv-workspace paths | Keep; execute. |

### `shared/`

| Doc | Class | Notes | Disposition |
|---|---|---|---|
| `shared/source-acquisition-pipeline.md` | CURRENT | Provider-agnostic TLS-tolerant fetch + likhit/OCR/LibreOffice conversion + provenance; underpins live sourcing | Keep. |
| `shared/entity-resolution-service.md` **[bannered]** | STALE (framing) | Matcher design (PR-91 candidate-gen + scoring + decision bands) sound; banner flags the REST-microservice framing AND the fictional `nes/search/*` paths + class names as design sketches (live search = function-based `shared/jawafdehi_shared/search/`) | Keep as design reference. |

### `shared/research/` (deep-research outputs — tech findings mostly sound)

| Doc | Class | Notes | Disposition |
|---|---|---|---|
| `shared/research/COST-AUDIT.md` | CURRENT | Free-OSS-only audit binding; FastAPI/"database-per-service" rows footnoted as pre-monolith framing (only license verdicts load-bearing) | Keep. |
| `shared/research/iceberg-catalog-options.md` | CURRENT | Lakekeeper + R2 + DuckDB recommendation, topology-agnostic | Keep. |
| `shared/research/duckdb-iceberg-r2-wiring.md` | CURRENT | DuckDB↔Iceberg↔R2 wiring; load-bearing for the lakehouse module | Keep. |
| `shared/research/entity-resolution-tech.md` | CURRENT | Splink + Fellegi-Sunter + DuckDB + bilingual normalization; topology-agnostic | Keep. |
| `shared/research/nes-sourcing-feasibility.md` | CURRENT | 1M-not-reachable, ~250k-450k realistic ceiling | Keep. |
| `shared/research/opensearch-bilingual-nepali.md` | CURRENT | analysis-icu + icu_transform + indic-transliteration — the adopted unified-search approach | Keep (load-bearing). |
| `shared/research/nes-schema-org.md` **[bannered]** | STALE | Documents the **rejected** "serialization layer, not a remodel" approach + the retired `entity:` id; banner notes the full remodel shipped instead. Field→property mappings + bilingual/extension content still useful for authoring | Keep (authoring reference, banner-scoped). |
| `shared/research/oidc-zitadel-integration.md` **[bannered]** | STALE | Core PyJWT/JWKS/Zitadel-roles path valid; "three services (Django/FastAPI/FastAPI)" + M2M-between-services emphasis wrong (now one monolith, in-process) | Keep; banner-scoped. |
| `shared/research/entity-harmonization.md` **[bannered]** | STALE | NES↔Jawafdehi↔NGM mapping sound; banner flags separate-repo paths + "three services"; body id-scheme replaced with `@id` IRIs | Keep; banner-scoped. |

### `nes/sourcing/` (live program)

| Doc | Class | Notes | Disposition |
|---|---|---|---|
| `nes/sourcing/SOURCED-INDEX.md` | CURRENT | **Canonical** live inventory: per-prefix counts, data-completeness gaps, held composition, access-gated buckets | Keep (source of truth for sourcing data). |
| `nes/sourcing/HELD-PROMOTION-ANALYSIS.md` | CURRENT | Per-bucket HELD promotability audit (2026-06-30) + ranked promotion work | Keep. |
| `nes/sourcing/sourcing-plan.md` | CURRENT | Plan/tiers/≥2-source/buckets all current | Keep. |
| `nes/sourcing/sourcing-methodology.md` | CURRENT | 10-stage pipeline + ≥2-source gate; id-scheme = `@id` IRI; validation = minimal JSON-LD; `manage.py bulk_ingest` | Keep (live procedure). |
| `nes/sourcing/sourcing-readiness-matrix.md` | CURRENT (data caveats) | RAG over buckets; conceptual entity-type column predates the schema.org `@type` vocab; some bucket states pre-date later ingests | Keep. |
| `nes/sourcing/<bucket>/RESULTS.md` (×11) | HISTORICAL | Per-bucket sourcing run records (education-colleges, federal-candidates, justices-historical, local-candidates, local-heads, local-officials, media-full, ministers, monarchs, provincial-candidates, ward-chairs) | Keep as run trail. |

### `ngm/`

| Doc | Class | Notes | Disposition |
|---|---|---|---|
| `ngm/ngm-source-inventory.md` | CURRENT | Authoritative source inventory + silver-table-family mapping; authoritative silver-family → schema.org `@type` crosswalk lives in `ngm_service/materials/jsonld.py` (`MATERIAL_TYPES`) | Keep (blueprint). |
| `ngm/r2-site-retirement-plan.md` | CURRENT | Plan to retire the standalone R2 site | Keep. |

### `jawafdehi/`

| Doc | Class | Notes | Disposition |
|---|---|---|---|
| `jawafdehi/ngm-frontend-integration-plan.md` | CURRENT | Plan to fold NGM into the Jawafdehi SPA | Keep. |
| `jawafdehi/sources-into-ngm-materials-plan.md` | CURRENT (partly superseded) | Plan to land document sources as NGM materials. **Phase 2 thin-row + `contributors` retention + §4 collapse-to-`DOCUMENT` superseded by `adr-cases-own-no-documents.md`**; Phase 0/0b/1/3 + the `visibility` design stand | Keep; read alongside the ADR. |
| `jawafdehi/adr-cases-own-no-documents.md` | CURRENT | ADR (2026-07-01): `cases` owns no entities/documents; both link out by required IRI. Removes `DocumentSource`/`/api/sources`, adds `CaseMaterialReference` (`material_iri` required), Materials = universal store, one `material_type` vocab, frontend "Document Source" purge. Amends control-plane + refactor + sources plan | Keep (load-bearing). |

---

## Resolved contradictions (the architecture decision record)

These were live cross-doc contradictions; all are now resolved — the **right** state is
the live ground truth. Kept here as a record of what was reconciled.

1. **Microservices/REST vs. monolith/in-process** → Monolith wins.
2. **Separate Django projects (Option B) vs. one monolith** → Monolith (Option C) wins.
3. **FastAPI vs. Django for NES/NGM** → Django apps win.
4. **poetry vs. uv** → uv workspace wins.
5. **`entity:<prefix>/<slug>` ids vs. `@id` IRIs** → IRI wins (clean slate).
6. **DRF tokens "migrate later" vs. dropped** → Dropped; OIDC/Zitadel only.
7. **The "internal" role vs. dropped/renamed** → readonly/no-"internal" wins; "contributor"→"caseworker".
8. **Search graceful-degradation fallback vs. hard-fail** → Hard-fail/no-fallback (503) wins.
9. **`JawafEntity` vs. collapsed** → Collapsed into `CaseEntityRelationship`.
10. **Migration runner vs. bulk-ingest** → Bulk-ingest wins.
11. **schema.org serialization-layer vs. full remodel** → Full remodel shipped (canonical stored JSON-LD keyed by `@id`).

---

## Open data-reconciliation items (sourcing in active flux)

Sourcing waves are still moving; these data/coverage items are parked here to revisit once
they settle (do not "fix" piecemeal — the numbers keep moving):

- **Live-count narrative vs. acquired-but-not-ingested.** Some RESULTS docs report
  validator-passing waves acquired/validated on disk but not yet ingested into the live DB.
  The SOURCED-INDEX total is accurate as a DB count; the narrative should later distinguish
  *validated-on-disk* from *ingested-into-DB*.
- **`sourcing-readiness-matrix.md` staleness vs. later ingests** (e.g. health facilities
  marked AMBER though hospitals were later sourced) and its conceptual `INSTITUTION`
  entity-type column (predates the schema.org `@type` vocab).
- **Cross-doc count clashes** (e.g. leaders "223 HoR" vs hor-275 "225") — reconcile once the
  rosters are final.
- **`ngm/ngm-source-inventory.md` NPBMIS access tag** ("API/HTML" vs auth-gated/401) — a
  data-accuracy nit to fold in with the next NGM sourcing pass.
- **SOURCED-INDEX §2.1/§2.2 percentages** are pre-ECN snapshots (bilingual ~46%, person
  Wikidata ~31%); recompute against the live 182,390-entity DB.
