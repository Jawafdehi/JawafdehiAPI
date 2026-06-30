# NES + NGM Control-Plane API Design

**Status:** DRAFT for review · **Date:** 2026-06-30 · **Branch:** `feat/control-plane-design` (off monolith `v2` @ `734b401`)

**Goal:** a fully-RESTful control plane for NES and NGM on the monolith, including **material file uploads** (the one genuinely-missing capability). Everything else — MCP, casework, integration tests, frontend — rewires into these endpoints.

This doc is **grounded in the code on `v2` as it stands today** (audited 2026-06-30). Where the audit found contract issues, they are called out as **DECISIONS** below rather than assumed.

---

## 1. Scope & shape (decided)

- **Home:** the monolith (`JawafdehiAPI@v2`, layout `services/{nes,ngm,jawafdehi}` + `monolith/` + `shared/`). This *is* the productionized branch.
- **Shape:** **two namespaced REST planes** under one deployment + one OIDC/Zitadel auth: `/api/nes/*` and `/api/ngm/*`. NES entities (schema.org JSON-LD keyed by `@id` IRI) and NGM cases (composite-key relational) + materials (JSON-LD) keep their distinct resource models. No forced unification — they are genuinely different shapes and share the search plane (`/api/search/`) already.
- **Upload mechanism:** **multipart through the API** (decided). The client POSTs the file to the monolith; the monolith streams it to R2 and upserts the Material JSON-LD in one call. This mirrors the proven case-evidence upload path.

---

## 2. Current surface (audited, ground truth)

### 2.1 NES `/api/nes/*`
| Route | Method | Auth | Status |
|---|---|---|---|
| `/entities` | GET | public | ✅ list/search; batch via `?ids=` (≤25) |
| `/entities/{ref}` | GET | public | ✅ detail; `ref` = url-encoded IRI **or** `prefix/slug` |
| `/entities/{ref}/versions` | GET | public | ✅ |
| `/entities/tags`, `/entity_prefixes` | GET | public | ✅ |
| `/entities` | POST | `nes_contributor` | ✅ create (JSON-LD or authoring shape) → 201 |
| `/entities/{ref}` | PATCH | `nes_contributor` | ✅ RFC-6902; blocks `/@id /@type /@context /jawafdehi:version` |
| `/admin/reindex` | POST | `nes_admin` | ⚠️ no-op stub (no search backend on this plane) |

### 2.2 NGM `/api/ngm/*`
| Route | Method | Auth | Status |
|---|---|---|---|
| `/courts/`, `/courts/{id}/` | GET | public | ✅ |
| `/courts/` `/courts/{id}/` | POST/PUT/PATCH | `HasNgmRole` | ✅ |
| `/cases/` | GET | public | ✅ filters: court/type/status/date_from/date_to |
| `/cases/` | POST | `HasNgmRole` | ✅ |
| `/cases/{court}/{case_number}/` | GET / PUT / PATCH | public / `HasNgmRole` | ✅ composite key (`update_composite`) |
| `/cases/{court}/{case_number}/{hearings,entities,documents}/` | GET | public | ✅ |
| `/entities/`, `/firms/` | GET | public | ✅ |
| `/firms/`, `/firms/{id}/` | POST/PUT/PATCH | `HasNgmRole` | ✅ |
| `/query/` | POST | `HasNgmQueryAccess` (scope **or** role) | ✅ SELECT-only guard |
| `/materials/?iri=` | GET / POST | public / `HasNgmRole` | ✅ JSON-LD upsert |
| `/materials/{source}/{ident}/` | GET / PUT | public / `HasNgmRole` | ✅ JSON-LD upsert; GET falls back to on-the-fly court-case materialization |
| `/ingestion/cases/` | POST | `HasNgmRole` | ⛔ **501** |
| `/ingestion/entities/resolve/` | POST | `HasNgmRole` | ⛔ **501** (pre-validates `nes_id` IRIs → 400) |
| `/ingestion/documents/` | POST | `HasNgmRole` | ⛔ **501** |

**Pagination:** NGM lists use `PlatformCursorPagination` → wire shape `{results, next}`. (Casework `ngm_client` already consumes this shape; the quarantined integration tests' `{items, next_cursor}` expectation is stale.)

### 2.3 The actual gaps
1. **No material file upload** anywhere. A Material is JSON-LD that *references* a URL already placed in R2 by a Scrapy `FilesPipeline`. There is no way to POST a PDF.
2. **`/ingestion/*` are 501** — the batch write path (replacing Scrapy direct-DB writes).

---

## 3. New endpoints

### 3.1 Material file upload (multipart) — THE new capability
```
POST /api/ngm/materials/{source}/{ident}/file        # auth: HasNgmRole
Content-Type: multipart/form-data
  file:      <binary>                  (required)
  role:      RAW|ALTERNATE|PERMALINK   (default RAW)
  material_type: court_order|...       (optional; else inferred/required if material is new)

Behavior:
  1. Stream `file` to R2 via a HashedFilenameS3Boto3Storage subclass with a
     material-specific prefix (FILE_STORAGE_PREFIX → "material_uploads/").
  2. default_storage.url(name) → absolute_media_url() → public URL.
  3. Upsert the Material at @id = https://<base>/material/{source}/{ident}:
     append {contentUrl, jawafdehi:linkRole: role, encodingFormat} as a
     schema.org MediaObject in associatedMedia (mirrors media_objects_from_document_sources).
  4. Return the Material JSON-LD (201 if material created, 200 if updated).

Reuse: services/jawafdehi/cases/{storage.py, services/source_files.py,
services/storage_links.py} — the proven case-evidence upload→R2→roled-link path.
Lift the pattern into shared/ so NGM materials and cases share one storage helper.
```

### 3.2 `/ingestion/*` — make the 501 stubs real (batch siblings)
- `POST /ingestion/documents/` — batch register/upload document sources; the multipart single-file endpoint above is the interactive sibling.
- `POST /ingestion/cases/` — idempotent upsert by composite natural key (court, case_number); replaces Scrapy direct-DB writes.
- `POST /ingestion/entities/resolve/` — NES write-back (IRI-validated; pre-validation already in place).

---

## 4. Contract issues found in the audit — DECISIONS NEEDED

These are **behavioral**, so they are *not* silently patched on a productionized branch. Each needs a ruling before implementation.

- **D1 — NES list `total` is wrong on text search.** `GET /api/nes/entities?query=…` returns `total = len(page)` (current page size), not the true match count (`views.py:150-151`). Clients can't tell if more results exist. *Options:* (a) compute a real count for the query path; (b) document it as `page_total` and rely on `limit`/`offset`; (c) move all search to `/api/search/` and make this list filter-only. **Recommend (a).**
- **D2 — NES batch vs list response-shape drift.** Same endpoint returns `{entities, total, limit, offset}` for list but `{entities, total, requested, not_found}` for `?ids=` (`views.py:191-197`). *Recommend:* keep both but document explicitly (now noted in the docstring), OR split batch to `/entities:batchGet`.
- **D3 — NES ADD_NAME endpoint removed.** MCP `submit_nes_change` still calls `POST /api/entities/{id}/names`, which **does not exist** on v2. *Recommend:* MCP migrates to `PATCH /api/nes/entities/{ref}` with an RFC-6902 `add` op to `/name`. (Consumer-side change; see §5.)
- **D4 — ≥2-source gate bypassed on HTTP writes.** The held-vs-published gate runs only in bulk-ingest, not on `POST/PATCH /entities` (NES) or material upsert (NGM). Direct API writes publish immediately. *Decision:* intended (privileged contributors) or should the gate apply to the API too? **Recommend: intended — document it; bulk path keeps the gate.**

---

## 5. Consumers to rewire (verified)

| Consumer | Today | Monolith v2 | Action |
|---|---|---|---|
| MCP `ngm_query_judicial` | `POST /api/ngm/query_judicial`, param `timeout` | `POST /api/ngm/query/`, param `timeout_seconds` | rename path + param |
| MCP NES read tools | `GET {base}/api/entities…` | `GET /api/nes/entities…` | add `/nes` prefix (base URL) |
| MCP `submit_nes_change` ADD_NAME | `POST /api/entities/{id}/names` | (removed) | switch to PATCH + jsonpatch (D3) |
| MCP `submit_nes_change` CREATE/UPDATE | `/api/entities…` | `/api/nes/entities…` | add `/nes` prefix |
| MCP `upload_document_source` | `POST /api/sources/` | unchanged | none |
| Casework `ngm_client` | `GET /api/ngm/cases/…`, `{results,next}` | same | none (already correct) |
| Integration tests | 3 hosts (:8081/:8082/:8000), `{items,next_cursor}` | one host, `/api/{nes,ngm}`, `{results,next}` | rewrite topology |

---

## 6. Fixes already applied on this branch (docs-only, no behavior change)
- NGM `courts/views.py` + `urls.py`: removed stale `GET /search` 501-stub references (search moved to `/api/search/` in the unified-search cutover).
- NGM `urls.py`: dropped `search` from the stale basename-collision note.
- NES `entities/views.py`: corrected docstring paths `/api/…` → `/api/nes/…`; documented the batch-by-ids response shape and the admin/reindex route; fixed `ReindexView` docstring path.

---

## 7. Open implementation questions
1. Material upload: single-file only, or also accept multi-file (one call → several MediaObjects)?
2. R2 layout for uploads: `material_uploads/<hash>.<ext>` (flat, mirrors `case_uploads/`) vs `material_uploads/<source>/<ident>/…` (path-structured like the Scrapy `court-orders/…` tree)?
3. Should `/ingestion/documents` and the single-file upload share one serializer/validator (extension/size/MIME) lifted into `shared/`?
4. Do uploaded materials go through the ≥2-source gate or the single-source exemption (`upsert_single_source_material`)? Court orders are inherently single-source today.
