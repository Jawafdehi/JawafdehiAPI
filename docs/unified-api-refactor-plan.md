# Unified API Surface — Refactor Plan

**Status:** DRAFT for review · **Date:** 2026-07-01 · **Branch:** `feat/control-plane-design`
**Companion:** `docs/control-plane-api-design.md` (the target API contract). This doc is the *how* — what to refactor, in what order, with measured blast radius.

**Framing:** the goal is **one unified API surface** over the existing apps + 3 databases. We are *not* building or preserving a thing called "monolith" — the `monolith/` package name is itself a rename target (R2 below).

---

## 0. What is NOT changing (guardrails)

- **3 Postgres DBs stay** (`default`, `nes`, `ngm`) + the `ServiceDatabaseRouter`. No data migration.
  - The router keys on Django **`app_label`, not directory path** (`monolith/config/db_router.py:48`, labels `entities`→nes, `courts`/`materials`→ngm). Every move below **preserves app labels**, so the router keeps working untouched.
- **`app_name` URL namespaces stay** (`nes`, `ngm-courts`, `ngm-materials`, `search`). They decouple route *names* from *paths*, so we can rewrite paths with **zero `reverse()` breakage**. Confirmed 3 latent basename collisions (`case`, `entity`, `case-detail`) that only the namespaces keep apart — so keeping them is mandatory, not optional.
- **Cross-app calls stay in-process** (plain imports, e.g. `cases/api_views.py:43-45`). No REST between apps.
- **DB merge into one is explicitly OUT OF SCOPE** (separate ~182k-row project).

---

## 1. Target end-state

**One flat app layout, one API namespace, 3 DBs behind the router.**

```
<project>/                 (was monolith/ — renamed; R2)
  config/  settings, urls, wsgi, asgi, db_router
  search/  discovery/      (unchanged apps, just no longer under "monolith")
app/                       (was services/{nes,ngm,jawafdehi}/ — flattened; R1)
  entities/   (label=entities → nes DB)      was services/nes/nes_service/entities
  courts/     (label=courts   → ngm DB)      was services/ngm/ngm_service/courts
  materials/  (label=materials→ ngm DB)      was services/ngm/ngm_service/materials
  cases/      (label=cases    → default)     was services/jawafdehi/cases
  review/     (label=review   → default)     was services/jawafdehi/review
```

**Public API (R3):**
| Path | Resource | Backing app | Auth |
|---|---|---|---|
| `/api/entities` | things (people, orgs, **courts**, **firms**, locations) | entities | `nes_contributor` write |
| `/api/materials` | documents (file-bearing CreativeWork) | materials | NGM role write |
| `/api/courtcases` | court-case records (composite key) + hearings/parties | courts | NGM role write |
| `/api/cases` | Jawafdehi **corruption** cases (unchanged) | cases | caseworker |
| `/api/sources` | corruption-case document sources (unchanged) | cases | caseworker |
| `/api/query` | gated SELECT over court tables | courts | `HasNgmQueryAccess` |
| `/api/ingestion/*` | batch write (make the 501s real) | courts/materials | NGM role |
| `/api/search`, `/api/casework/*`, discovery | unchanged | search/review/discovery | mixed |

Old `/api/nes/*` and `/api/ngm/*` are **removed outright — HARD CUT, no 301 aliases**
(decided 2026-07-01). Consumers migrate in lockstep with R3/R4; there is no overlap
release. This makes R3 simpler (plain re-mount, no redirect layer) but means R4
consumer rewiring must land together with R3, not after.

---

## 2. Phases (ordered; each is one reviewable checkpoint, not many commits)

### Phase R1 — Flatten `services/{nes,ngm,jawafdehi}` → `app/`
> ⚠️ **RE-SCOPED 2026-07-01: this is NOT mechanical/low-risk.** The split is a **uv
> workspace with 4 installable member packages** (`shared`, `services/*`), each with its
> own `pyproject.toml` + build packaging + source roots, PLUS a deliberately intricate
> test-collision fix (pyproject.toml:82–110 + root conftest sys.path manipulation): the
> `__init__.py` placement making NES vs NGM `tests/test_api.py` resolve uniquely, and the
> Jawafdehi bare-`tests` package winning over a dependency's site-packages `tests`.
> Flattening = restructuring workspace membership + build config + source roots + that
> test scheme, on a PRODUCTIONIZED branch. **R1 is DEFERRED / optional** — it's cosmetic
> relative to the goal. The unified API surface is delivered by **R3, which does NOT
> depend on R1** (app_name namespaces already decouple URL paths from code layout).
> Do R1 only if the internal tidiness is worth the churn; otherwise skip to R3.

**Scope (measured):** 43 files carry `from nes_service` / `from ngm_service` imports; ~135 cross-app import statements; 5 INSTALLED_APPS entries; 7 `include()` lines; PLUS 4 member `pyproject.toml`, workspace `members`/`sources`, hatch wheel packages, and the conftest/`__init__.py` test scheme.
**Steps:**
1. `git mv` each app dir to its flat home; **keep each `apps.py` `label=` exactly as-is** (router depends on labels, not paths).
2. Decide the import root: rename the Python import packages `nes_service.entities`→`app.entities`, `ngm_service.courts`→`app.courts`, etc. (or drop the `_service` package and expose `entities`, `courts`, … directly).
3. Rewrite imports across the 43 files (mechanical; a scripted `sed` + a full test run).
4. Update INSTALLED_APPS (5 entries), the 7 `include()`s, and `db_router.py`'s label sets **only if labels changed** (they should not).
**Risk:** low (mechanical). **Verify:** `manage.py check`, `makemigrations --check --dry-run` (must be NO-OP — proves labels/models unchanged), full test suite.
**Gotcha:** NES + NGM both have a `tests/` dir; the collapse must preserve the `services/{,nes/,ngm/}__init__` test-collision fix (see think-big memory). Confirm pytest still collects cleanly.

### Phase R2 — Rename the `monolith/` package
**Scope (measured):** 8 Python imports + 9 Django string bindings + 2 entry points ≈ 17 files. Bindings: `manage.py:20`, `settings.py` (ROOT_URLCONF:287, WSGI:288, DATABASE_ROUTERS:435, INSTALLED_APPS:268/270), `urls.py:52/74`, `pyproject.toml:73`, `settings_test.py:40`.
**Decision needed:** target name — `config/` + top-level apps, or `platform/`, or fold `search`/`discovery` into `app/`. (Pick in review.)
**Risk:** low but touches boot path — do it as its own checkpoint, not mixed with R1.
**Verify:** app boots (`manage.py check`), test settings import, WSGI/ASGI import.

### Phase R3 — Unify the URL surface (drop prefixes, rename courtcases)
**Steps:**
1. Re-mount in `config/urls.py`: `/api/entities` (was `/api/nes/…`), `/api/materials`, `/api/courtcases` (was `/api/ngm/cases`), `/api/query`, `/api/ingestion/*`. Keep `app_name` namespaces intact.
2. **No redirects — hard cut.** Delete the old `/api/nes/` + `/api/ngm/` mounts outright; R4 consumer rewiring lands in the same checkpoint.
3. Fix the 3 hardcoded path builders → derive from `reverse()` or a single base helper:
   - `monolith/discovery/corpus.py:82–148` (`jsonld_url=f"/api/nes/entities/…"`, `/api/ngm/materials/…`) — **public-facing** (sitemaps/ResourceSync), must emit new paths.
   - `services/jawafdehi/review/ngm_client.py:49–61` (`_ngm_base()` derives `/api/ngm`) → point at `/api/courtcases`.
4. Update in-repo docstrings (NES/NGM views/urls) to the new paths.
5. `SPECTACULAR_SETTINGS["SCHEMA_PATH_PREFIX"]` stays `/api/` (settings.py:603) — regenerate schema, eyeball operationIds.
**Risk:** medium (public URLs change, hard cut). Namespace-preserved `reverse()` contains the internal breakage; consumer rewiring (R4) MUST ship in the same checkpoint since there's no alias grace period.
**Verify:** schema diff, discovery/sitemap output shows new paths, search result URLs correct, no lingering `/api/nes`/`/api/ngm` references.

### Phase R4 — Consumer rewiring (ships WITH R3 — hard cut, no grace period)
Enumerated consumers (all hardcode old paths):
| Consumer | Location | Change |
|---|---|---|
| Frontend `admin-api.ts` (~19 call sites) | `jawafdehi-frontend` | `/api/nes/*`,`/api/ngm/*` → new paths |
| Frontend `ngm-api.ts` | `jawafdehi-frontend` | `/api/ngm` base → `/api/courtcases` + `/api/materials` |
| MCP tools (nes.py, ngm_proxy.py, jawafdehi_cases.py) | `_quarantine/jawafdehi-mcp` | drop `/nes` prefix; `query_judicial`→`query` + `timeout`→`timeout_seconds`; **ADD_NAME → PATCH+jsonpatch** (D3) |
| Review `ngm_client.py`, `jds_client.py` | in-repo | new base paths (covered in R3.3) |
| Integration tests | `_quarantine/jawafdehi-integration-tests` | rewrite topology: one host, new paths, `{results,next}` shape |
**Note:** the 301 aliases mean consumers can migrate *after* R3 ships, not in lockstep.

### Phase R5 — Backend CRUD completion + React admin full CRUD (decided 2026-07-01)
"React admin supports all CRUD" for the **4 unified resources** (entities, materials,
courtcases, corruption-cases). The fan-out proved this is backend-blocked — no
resource has a DELETE today, and materials has no LIST. Two decisions locked:
- **Soft-delete everywhere** (NOT hard delete). DELETE endpoints archive/tombstone
  (keep row + version history), fitting the accountability/audit platform. Resources
  already have the pattern: `cases`/`sources` use an `is_deleted` field. Add the same
  (field + queryset filter + `DELETE` verb that sets it) to the resources that lack it.
- **Do R5 AFTER R3** so the frontend binds to the final `/api/{entities,materials,courtcases,cases}`
  paths ONCE (no double-rewire). Courts/firms fold into `entities` — no separate forms.

**R5a — Backend CRUD gaps (own checkpoint):**
| Resource | Add |
|---|---|
| entities | soft `DELETE /api/entities/{ref}` (+ is_deleted on StoredEntity + filter) |
| materials | **LIST** `GET /api/materials` (admin table) + soft `DELETE` + optional PATCH |
| courtcases | soft `DELETE` (add DestroyModelMixin → soft) |
| corruption-cases | expose soft `DELETE` (is_deleted exists; wire the verb) |
| sources | expose soft `DELETE` (is_deleted exists) |
NES PATCH stays the JSON-LD update path (no PUT needed). Verify each with tests.

**R5b — React admin (frontend `jawafdehi-frontend`, bespoke React + react-hook-form + shadcn, NOT react-admin):**
Per resource reach full L/G/C/U/D. Gaps today (from fan-out):
- entities: has L/G/C/U — add **delete** (confirm dialog).
- materials: has G/C/U — add **list page** (needs R5a list endpoint) + **delete**.
- courtcases: has L/G/C/U — add **delete**.
- corruption-cases + sources: **build create/edit forms** (currently read-only lists) + delete.
Follow the existing add-a-resource pattern: `src/services/admin-api.ts` client fns →
`src/pages/admin/{section}/{List,Create,Edit}.tsx` → routes in `App.tsx` → nav in
`AdminLayout.tsx`. Delete = button + confirm dialog calling `client.delete(...)`.

### Phase R6 — Feature: material file upload + real `/ingestion/*`
Net-new, isolated (see design doc §3). `POST /api/materials/{source}/{ident}/file` (multipart→R2→MediaObject), reusing `cases/storage.py` `HashedFilenameS3Boto3Storage` lifted into `shared/`. Then make `/api/ingestion/{cases,documents,entities/resolve}` real (replace Scrapy direct-DB writes).
**LAST** so it lands on the already-unified surface (no rework).

---

## 3. Behavioral decisions still open (block R5, not R1–R4)
Carried from the design doc — need a ruling:
- **D1** NES list `total` wrong on `?query=` (returns page size). *Rec: real count.*
- **D2** NES batch vs list response-shape drift. *Rec: keep + document.*
- **D3** NES ADD_NAME endpoint gone → MCP uses PATCH+jsonpatch. *Rec: yes.*
- **D4** ≥2-source gate bypassed on HTTP writes (bulk-only). *Rec: intended; document.*
- **D5** firm-blacklist facts home once `/firms` is gone. *Rec: slim record table behind the Organization entity.*

---

## 4. Sequencing (decided, revised 2026-07-01)
**R3 (URL unify, HARD cut) + R4 (consumers, same checkpoint) → R5a (backend CRUD gaps) →
R5b (React admin full CRUD) → R6 (upload feature).** R1 (flatten) and R2 (rename `monolith/`)
are **DEFERRED as optional cleanup** — they're internal cosmetics that do NOT gate the
unified API surface (R3 delivers that; app_name namespaces decouple paths from code layout).
R1 in particular is a workspace/packaging restructure, not a mechanical move — not worth its
risk on the productionized branch unless explicitly wanted.
Rationale: R3 is the actual "one unified surface." Everything downstream (admin CRUD, upload)
binds to the final `/api/{entities,materials,courtcases,cases}` paths exactly once. R3+R4 must
land together — hard cut, no alias grace period.

**Baseline verify command** (works in this env):
`SECRET_KEY=dev-check-only ALLOWED_HOSTS='*' DJANGO_SETTINGS_MODULE=monolith.config.settings_test uv run python manage.py check` — currently green.

---

## 5. Open questions for review
1. R1 import root: `app.entities` etc., or expose `entities`/`courts`/`materials` at top level (drop the `_service` packages)?
2. R2 target name for the `monolith/` package: `config` + top-level apps, `platform/`, or something else?
3. R3: keep `/api/query` and `/api/ingestion` unprefixed, or namespace them (`/api/courtcases/query`)? They're court-table operations.
4. Sequencing: upload last (clean surface) vs. upload first (earlier delivery)?
5. Do we rename `label=entities` etc. at all, or leave labels permanently as the router contract? (Leaving them is zero-risk; renaming would force migration-state churn for no gain.)
