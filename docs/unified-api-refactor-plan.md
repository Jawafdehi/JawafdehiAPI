# Unified API Surface — Refactor Plan

> **AMENDED 2026-07-01 by [`jawafdehi/adr-cases-own-no-documents.md`](./jawafdehi/adr-cases-own-no-documents.md).**
> `/api/sources` is removed and `DocumentSource` retires: R5a's "DocumentSource CRUD"
> and R6's separate source-upload path **invert** into *retiring sources into
> `/api/materials`*. Case evidence becomes a `CaseMaterialReference` join keyed by a
> required `material_iri`. See the ADR for the target model; rows below are annotated.

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

**ONE installable package, one flat app layout, one API namespace, 3 DBs behind the router.**
(No uv workspace, no 4 member packages — see R1.)

```
pyproject.toml             ONE project (all deps merged), no [tool.uv.workspace]
config/                    (was monolith/) settings, urls, wsgi, asgi, db_router
search/  discovery/        unchanged apps (no longer under "monolith")
jawafdehi_shared/          shared libs (auth/oidc, entities/ids, search, drf) — folded in from shared/
entities/   (label=entities → nes DB)      was services/nes/nes_service/entities
courts/     (label=courts   → ngm DB)      was services/ngm/ngm_service/courts
materials/  (label=materials→ ngm DB)      was services/ngm/ngm_service/materials
cases/      (label=cases    → default)     was services/jawafdehi/cases
review/     (label=review   → default)     was services/jawafdehi/review
ngm/                                       was services/jawafdehi/ngm (proxy app)
```
> **`config` collision — resolved (2026-07-01):** the Jawafdehi app-support package
> (`services/jawafdehi/config` = `auth.py` / `middleware.py` / `structlog_config.py`) is FOLDED
> INTO `jawafdehi_shared` — it's cross-cutting Django plumbing, which is exactly what shared is
> for. So project package keeps `config/`; app-support goes to `jawafdehi_shared.auth`,
> `jawafdehi_shared.middleware`, `jawafdehi_shared.logging` (was `.structlog_config`). No new
> top-level package. Fix the 7 `from config.…` import sites (`cases/api_views.py:35`,
> `monolith/config/settings.py:48`, etc.) → `from jawafdehi_shared.…`.

**Public API (R3):**
| Path | Resource | Backing app | Auth |
|---|---|---|---|
| `/api/entities` | things (people, orgs, **courts**, **firms**, locations) | entities | content-role write |
| `/api/materials` | documents (file-bearing CreativeWork) | materials | NGM role write |
| `/api/courtcases` | court-case records (composite key) + hearings/parties | courts | NGM role write |
| `/api/cases` | Jawafdehi **corruption** cases (unchanged) | cases | caseworker |
| ~~`/api/sources`~~ | **REMOVED (ADR 2026-07-01)** — sources collapse into `/api/materials`; evidence links by `material_iri` | — | — |
| `/api/query` | guarded SELECT over court tables | courts | `IsAuthenticated` (no role) |
| `/api/ingestion/*` | batch write (make the 501s real) | courts/materials | NGM role |
| `/api/search`, `/api/casework/*`, discovery | unchanged | search/review/discovery | mixed |

Old `/api/nes/*` and `/api/ngm/*` are **removed outright — HARD CUT, no 301 aliases**
(decided 2026-07-01). Consumers migrate in lockstep with R3/R4; there is no overlap
release. This makes R3 simpler (plain re-mount, no redirect layer) but means R4
consumer rewiring must land together with R3, not after.

---

## 2. Phases (ordered; each is one reviewable checkpoint, not many commits)

### Phase R1 — Collapse to ONE installable package (decided 2026-07-01)
**The 4-member uv workspace is vestigial and gets removed.** The root pyproject already
admits it: *"the DEPLOYABLE is this root project… one install pulls every app"*, and the
per-service-isolation rationale (*"a service image installs just these + shared"*) is DEAD —
there are no per-service images; the Dockerfile builds ONE image (`COPY services/ ./services/`,
one `uv sync`, one gunicorn `monolith.config.wsgi`). So the split buys nothing.

**Target:** one `pyproject.toml`, one project, apps as plain top-level subpackages.
1. **Merge deps** — union the 4 members' `dependencies` into the root `[project].dependencies`
   (they already all install together; NGM's duckdb/boto3, Jawafdehi's big list, NES's jsonpatch,
   shared's pyjwt — all become root deps; keep the `bigo-enrichment` + `search` extras + the
   `likhit` git source at root). Delete `[tool.uv.workspace]`, `[tool.uv.sources]`, and the 3
   `services/*/pyproject.toml` + `shared/pyproject.toml`.
2. **Flatten dirs** so each is a top-level import package, `apps.py` `label=` UNCHANGED (router
   keys on label, not path → no migration): `entities` (was nes_service.entities), `courts`,
   `materials` (was ngm_service.*), `cases`, `review`, `ngm` (Jawafdehi proxy), `jawafdehi_shared`.
3. **Fold Jawafdehi `config/` app-support INTO `jawafdehi_shared`** (resolves the `config` name
   collision — decided): `config/auth.py` (`resolve_or_create_identity`, a Jawafdehi identity
   helper layered on the existing shared OIDC — keep DISTINCT from `jawafdehi_shared/auth/`) →
   **`jawafdehi_shared/identity.py`**; `config/structlog_config.py` (`configure_structlog`) →
   **`jawafdehi_shared/logging_config.py`** (NOT `logging` — avoid stdlib shadow); `config/middleware.py`
   → **`jawafdehi_shared/middleware.py`**. The project package (`monolith/config`) then safely
   becomes top-level `config/`.
4. **Rewrite imports** — `nes_service.entities`→`entities`, `ngm_service.courts`→`courts`, etc.
   across the 43 files; plus the 7 `from config.…` sites (`cases/api_views.py:35` `config.auth`→
   `jawafdehi_shared.identity`; `settings.py:48` `config.structlog_config`→`jawafdehi_shared.logging_config`;
   `MIDDLEWARE` string paths `config.middleware.*`→`jawafdehi_shared.middleware.*`). Fix INSTALLED_APPS
   (5) + `include()`s (7). (scripted sed + full test run).
5. **The test-collision hack DISSOLVES** — once apps are flat, `services/nes/tests` vs
   `services/ngm/tests` become `entities/tests` vs `courts/tests` (unique names), so the
   `services/{,nes/,ngm/}__init__.py` + root-conftest `sys.path` juggling (pyproject.toml:82–110)
   can be deleted. Verify pytest still collects cleanly WITHOUT it.
6. **hatch packaging** — one `[tool.hatch.build.targets.wheel] packages = [...]` listing the flat
   app packages + `config` + `jawafdehi_shared`. Update Dockerfile COPY lines to the flat layout.

**Also folds in the `monolith/` → `config/` rename.** ~8 imports + 9 Django string bindings + 2
entry points (`manage.py:20`, settings ROOT_URLCONF/WSGI/DATABASE_ROUTERS/INSTALLED_APPS, urls,
pyproject, settings_test).
**Scope:** 43 files w/ cross-app imports + ~135 import stmts + 4 pyproject deletions + Dockerfile
+ workspace/hatch config + test-scheme removal. **Risk:** MEDIUM — touches packaging + boot +
test collection, on the productionized branch. Do as its OWN checkpoint, verified end-to-end.
**Verify:** `uv sync` resolves; `manage.py check`; `makemigrations --check --dry-run` NO-OP
(proves labels/models unchanged); FULL test suite green; `docker build` succeeds; gunicorn boots.
**Independence:** R1 still does NOT gate the unified API surface (R3 delivers that). It can run
before or after R3; sequencing below runs it first so later phases target the flat layout once.

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
| Frontend `admin-api.ts` (~19 call sites) | `jawafdehi-frontend` | `/api/nes/*`,`/api/ngm/*` → new paths — ✅ DONE (frontend `v2`, R4+R5b) |
| Frontend `ngm-api.ts` | `jawafdehi-frontend` | `/api/ngm` base → `/api/courtcases` + `/api/materials` — ✅ DONE |
| MCP tools (nes.py, ngm_proxy.py, jawafdehi_cases.py) | `_quarantine/jawafdehi-mcp` | drop `/nes` prefix; `query_judicial`→`query` + `timeout`→`timeout_seconds`; **ADD_NAME → PATCH+jsonpatch** (D3) — ⚠️ TODO |
| Review `ngm_client.py`, `jds_client.py` | in-repo | new base paths (covered in R3.3) — ✅ DONE with R3 |
| Integration tests | in-repo `integration-tests/` (the MAINTAINED copy — already one-host) | drop `/api/nes`+`/api/ngm` prefixes, `cases`→`courtcases`, flat `entities`→`courtcase-entities`, unify health to `/api/health`, docs/README — ✅ DONE (2026-07-01) |
**Note:** the OLD pre-monolith 3-host original at `_quarantine/jawafdehi-integration-tests`
(still `:8081`/`:8082`, `{items,next_cursor}`) is DEAD — not migrated; the in-repo copy
supersedes it. **Remaining R4 work: the MCP tools only.**
**Note:** hard cut — no 301 aliases; consumers migrate in lockstep with R3, not after.

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
| ~~sources~~ | **SUPERSEDED (ADR 2026-07-01)** — instead of adding source CRUD, `DocumentSource` is removed; evidence becomes a `CaseMaterialReference` join (`material_iri` required) and the material-upload path (R6) is the one source-authoring surface |
NES PATCH stays the JSON-LD update path (no PUT needed). Verify each with tests.

**R5b — React admin (frontend `jawafdehi-frontend`, bespoke React + react-hook-form + shadcn, NOT react-admin):**
Per resource reach full L/G/C/U/D. Gaps today (from fan-out):
- entities: has L/G/C/U — add **delete** (confirm dialog).
- materials: has G/C/U — add **list page** (needs R5a list endpoint) + **delete**.
- courtcases: has L/G/C/U — add **delete**.
- corruption-cases: **build create/edit forms** (currently read-only lists) + delete.
- ~~sources~~: **SUPERSEDED (ADR 2026-07-01)** — do NOT build source forms. Retire the
  "Document Source" concept from the frontend entirely (types, components, i18n): source
  admin folds into the NGM material admin (port its friendlier fields onto the material
  form); public `CaseDetail` resolves evidence via `getMaterial(material_iri)` and re-tiers
  on the unified `material_type` vocab; evidence editing uses `{material_iri, additional_details}`.
Follow the existing add-a-resource pattern: `src/services/admin-api.ts` client fns →
`src/pages/admin/{section}/{List,Create,Edit}.tsx` → routes in `App.tsx` → nav in
`AdminLayout.tsx`. Delete = button + confirm dialog calling `client.delete(...)`.

### Phase R6 — Feature: material file upload + real `/ingestion/*`
Net-new, isolated (see design doc §3). `POST /api/materials/{source}/{ident}/file` (multipart→R2→MediaObject), reusing `cases/storage.py` `HashedFilenameS3Boto3Storage` lifted into `shared/`. Then make `/api/ingestion/{cases,documents,entities/resolve}` real (replace Scrapy direct-DB writes).
**LAST** so it lands on the already-unified surface (no rework).
> **ADR 2026-07-01:** this upload path **is** the source-authoring surface — the old
> `DocumentSourceCreateSerializer.uploaded_file` path converges here (one upload path,
> not two). linkRole vocab includes `MARKDOWN` + `SOURCE_PAGE` (ADR D-D).

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
**R1 (collapse to one package) → R3 (URL unify, HARD cut) + R4 (consumers, same checkpoint) →
R5a (backend CRUD gaps) → R5b (React admin full CRUD) → R6 (upload feature).**
R1 now runs FIRST (user wants one installable package, not 4) so every later phase targets the
flat layout once. R1 is its own verified checkpoint (packaging + boot + test collection). It
still doesn't strictly *gate* the unified surface (R3 does), but doing it first avoids editing
URLs/consumers against a layout that's about to move. (R2 "rename monolith/" is folded INTO R1.)
Rationale: R3 is the actual "one unified surface"; downstream work (admin CRUD, upload) binds to
the final `/api/{entities,materials,courtcases,cases}` paths exactly once. R3+R4 land together —
hard cut, no alias grace period.

**Baseline verify command** (works in this env):
`SECRET_KEY=dev-check-only ALLOWED_HOSTS='*' DJANGO_SETTINGS_MODULE=monolith.config.settings_test uv run python manage.py check` — currently green.

---

## 5. Open questions for review
1. ✅ RESOLVED — R1 import root: apps at top level (`entities`, `courts`, `materials`, `cases`, …), dropping `nes_service`/`ngm_service`.
2. ✅ RESOLVED — `monolith/` → `config/`; Jawafdehi `config/` app-support folded into `jawafdehi_shared` (`identity`, `logging_config`, `middleware`). No new top-level package.
3. R3: keep `/api/query` and `/api/ingestion` unprefixed, or namespace them (`/api/courtcases/query`)? They're court-table operations.
4. Sequencing: upload last (clean surface) vs. upload first (earlier delivery)?
5. Do we rename `label=entities` etc. at all, or leave labels permanently as the router contract? (Leaving them is zero-risk; renaming would force migration-state churn for no gain.)
