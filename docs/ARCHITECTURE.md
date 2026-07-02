# Platform Architecture (current state)

_Last updated: 2026-07-02. This is the single source of truth for **what the
platform is now**. For per-doc status (what's current vs superseded) see
[`DOC-STATUS.md`](./DOC-STATUS.md)._

The program unifies three former systems — **NES** (Nepal Entity Service), **NGM**
(governance/judicial data lake), and **Jawafdehi** (anti-corruption case platform)
— into ONE platform. It went through a microservices design and then **reversed to
a monolith**; this doc describes the shipped result, not the journey.

## 1. Shape: one Django monolith, three databases

- **One Django project** in the `jawafdehi-api` repo (trunk `v2`), one WSGI, one
  image, runs at **`:48000`**. (The R1 collapse flattened the earlier
  `monolith/` + `services/{nes,ngm,jawafdehi}/` layout into top-level apps.)
- **Apps** (all top-level dirs, in `INSTALLED_APPS`): `entities` (NES), `courts`
  + `materials` (NGM), the Jawafdehi apps `cases` + `review`, plus the
  platform-level `search`, `discovery`, and `jobs` apps. Project glue lives in the
  top-level `config/` package (`settings.py`, `urls.py`, `wsgi.py`, `asgi.py`,
  `db_router.py`). _(The `case_workflows` app was dropped — migration
  `cases/migrations/0040_drop_case_workflows_tables.py`.)_
- **Database-per-service preserved** via a DB **router** (`config.db_router.
  ServiceDatabaseRouter`, wired at `config/settings.py:433`): `entities` → `nes`
  DB, `courts`/`materials` → `ngm` DB, everything else → `default`. **No cross-DB
  FKs/joins** — query each DB, join in app. **Inter-app calls are in-process**
  (NOT REST; the microservices-over-REST design was reversed). _Caveat:_ the async
  **review/jobs poller** is a separate process that talks HTTP (OIDC bearer) to
  `/api/…` by design — it can target a **remote** portal (see
  `review/ngm_client.py`, `review/jds_client.py`, `jobs-queue-design.md`); that is
  a poller, not a revived internal REST proxy.
- **uv workspace** (not poetry): one top-level `pyproject.toml` + `uv.lock`. The
  cross-app libs live in the top-level **`jawafdehi_shared/`** package:
  `auth.oidc`, `entities.ids`, `search.opensearch` + `search.mappings` +
  `search.transliterate`, `drf.base`.
- **Testing:** engine-agnostic; SQLite fallback per DB alias for the full suite
  (~1057 unit tests pass). Integration tests target the one host `:48000`.

## 2. Identity: schema.org JSON-LD keyed by `@id` IRIs

The canonical **stored** form for NES entities AND NGM materials is a raw
**schema.org JSON-LD** document, keyed by its `@id` **IRI** — the platform-wide
join key. Clean slate: the legacy `entity:<prefix>/<slug>` scheme is **gone**;
per-type Pydantic models were deleted in NES.

| Kind | IRI grammar |
|---|---|
| NES entity | `https://jawafdehi.org/entity/<prefix>/<slug>` |
| NGM material | `https://jawafdehi.org/material/<source>/<ident>` |
| Jawafdehi case | `https://jawafdehi.org/case/<slug>` (minted at PUBLISH) |
| Court case | `https://jawafdehi.org/courtcase/<court>/<case_number>` |

- IRI contract + validators live in `jawafdehi_shared/entities/ids.py`
  (canonicalize-on-store, strict-validate, `MAX_IRI_LENGTH=300`).
- Validation is **minimal** (known `@type`, valid `@id`, `name` present) — see
  `entities/validation.py` and `materials/jsonld.py`.
- Bilingual text is a **language map** `{"ne": …, "en": …}`. Nepal-specific types
  use the `jawafdehi:` extension namespace (`jawafdehi:Province`, `:District`,
  `:PoliticalParty`, `:CourtCase`, `:ChargeSheet`, …).
- **JawafEntity was collapsed** into `CaseEntityRelationship`, which holds the
  `nes_id` IRI bind directly. `Case.case_id` field was dropped (slug = internal +
  external id).

## 3. Search: unified OpenSearch, bilingual, hard dependency

- ONE OpenSearch query across 4 indices (`nes-entities`, `ngm-materials`,
  `ngm-courtcases`, `jawafdehi-cases`) → common envelope at **`GET /api/search/`**.
  Replaced ALL per-app searches.
- **Bilingual EN + Nepali**: `analysis-icu` (icu_normalizer/tokenizer/transform/
  folding) + `indic_normalization` + an ICU transliteration bridge, so a Latin
  query matches a Devanagari doc and vice-versa. Config in
  `jawafdehi_shared/search/mappings.py`; research in
  `shared/research/opensearch-bilingual-nepali.md`.
- **Hard dependency**: cluster down → **503**, no in-process fallback.
- Relevance tuning (field boosts + exact-phrase boost + `lang` re-rank + per-type
  `indices_boost`) and **`search_after` cursor deep-paging** are built.
- `analysis-icu` is **baked into a custom image** (`infra/opensearch/Dockerfile`).
- The `@id` envelope also drives **ResourceSync + Sitemaps** (the `discovery/` app).

## 3.5 Editorial CMS: headless Wagtail (updates/newsroom)

- Public **updates/news articles** are served by a **headless Wagtail CMS** (the
  `content/` app — "Jawafdehi Newsroom"), consumed by the SPA at
  **`GET /api/cms/v2/pages/`** (published pages) and `/api/cms/v2/page_preview/…`
  (signed draft preview). Editorial admin mounts at `/newsroom/`, documents at
  `/documents/`; the headless API router registers `pages`, `images`,
  `documents`, `page_preview` (`content/api.py`). Wagtail's built-in password
  login is retired in favour of OIDC SSO.
- Image renditions and document links are serialized as **absolute URLs** (built
  from the request) so the cross-origin headless SPA can load them.
- **History:** the `content/` app was dropped from `v2` during the R1 collapse
  (`4c39d8c`), which briefly 404'd the SPA's `/updates` calls; it was
  **forward-ported back into `v2`** (Wagtail 7.4, PR #270, adapted from
  `origin/main` which has no shared history) with the frontend contract preserved.

## 4. Auth: OIDC/Zitadel only

- DRF token auth is **fully dropped**. All clients (incl. service-to-service) use
  **OIDC/Zitadel** bearer tokens; local JWKS validation via PyJWT.
- **Roles:** `admin` (Admin group AND is_superuser) / `moderator` / `caseworker`
  (renamed from "contributor") / `readonly` (read incl. casework — covers draft/
  in-review viewing) / `public` (read excl. casework). The earlier "internal" role
  idea was dropped.
- Draft/in-review cases are **not indexed** and not publicly retrievable.

## 5. Storage / data plane (Postgres-SoR, lakehouse-lite)

The full design + decisions are in [`data-plane-design.md`](./data-plane-design.md);
the shape today:

- **Three planes, none on another's hot path.** **Serving** = the 3 Postgres DBs
  (SoR for everything served) + **Search** = unified OpenSearch (§3) + **Archive** =
  **Cloudflare R2** object store (raw bytes + OCR/likhit markdown + provenance;
  immutable SoR for raw evidence — this is where the storage bulk lives; local dev
  uses MinIO). Heavy async work runs on the central `jobs` queue (§ below).
- **Postgres is the system of record**, NOT a lake. The served structured corpus is
  only tens of GB (entity JSONB, court cases); the large storage figure is R2 object
  bytes, referenced by URL (one copy).
- **No Iceberg / DuckDB / Lakekeeper lake right now ("lakehouse-lite").** The
  `lakehouse/` module (Apache Iceberg + DuckDB + Lakekeeper, all free/OSS — see
  `shared/research/COST-AUDIT.md`) is a **DORMANT, tested seam**, kept Iceberg-ready
  but not the shipped storage layer. Its medallion framing ("silver is the source /
  Postgres derived from silver") is **superseded**: any future silver is derived
  *from* Postgres + the R2 archive, never the reverse. Revisit only when a real
  recurring cross-domain analytical query (or serving-Postgres latency from analyst
  SQL) earns it (`data-plane-design.md` §6).
- **Full-text search over materials** is fed by the async `material_convert` job kind:
  OCR/likhit → markdown in R2 (`linkRole=MARKDOWN`) → `Material.data["text"]` → unified
  search reindex (`data-plane-design.md` §5).

## 6. Data sourcing (the live program)

- **NES** target: deepen toward ~1M PUBLIC entities (realistic verified ceiling
  ~250k–450k; 1M needs the high-volume expansion buckets). **Public entities only**;
  private individuals enter only via the NGM plaintiff/defendant carve-out.
- **Hard rule:** every NES entity needs **≥2 independent-publisher sources** or it
  is **HELD** (not published). Enforced by the `bulk_ingest` path's count gate
  (keyed on source `authority`). The old per-entity migration runner was dropped.
- **Acquisition** uses TLS-tolerant fetch of `*.gov.np`, the existing Scrapy spiders
  in the `/damodaha-volunteer/ngm/` repo (CIAA, Kanun Patrika, courts, PPMO), and
  likhit/OCR normalization — see `shared/source-acquisition-pipeline.md`.
- **Ingest paths:** NES has `manage.py bulk_ingest` (JSON/JSONL of
  `{entity_prefix, entity_data, sources}` records → validate → ≥2-source gate →
  upsert + OpenSearch index). NGM materials ingest command is a pending follow-up.

### Live as of 2026-06-29 (in the running `nes` DB)
| Bucket | Count |
|---|---|
| Published persons | 160,909 |
| Published organizations | 20,644 |
| Published locations | 837 |
| **NES total (published)** | **182,390** (all ≥2-source except the ECN election-authority exception) |
| HELD (single/same-publisher, recoverable) | **~1,900** |
| NGM materials | 2,122 |

The ECN harvest (2026-06-29) drove the six-figure inflection (152,960 local
candidates + ward chairs + provincial/federal candidates). **Per-bucket counts,
data-completeness gaps, and the held-entity composition are tracked canonically in
[`nes/sourcing/SOURCED-INDEX.md`](./nes/sourcing/SOURCED-INDEX.md)** — that file (not
this table) is the source of truth for sourcing data. For why the held entities are
held and which can be promoted, see
[`nes/sourcing/HELD-PROMOTION-ANALYSIS.md`](./nes/sourcing/HELD-PROMOTION-ANALYSIS.md).

## 7. Working model

Two repos, **both with `v2` as the working trunk**:

- **`jawafdehi-api`** (this repo — the Django backend). Local clone at
  `/damodaha-volunteer/think-big/jawafdehi-api`. `origin` = the org
  (`Jawafdehi/JawafdehiAPI`), `fork` = the `damodaha` personal fork. PRs are filed
  on the org from the fork.
- **`jawafdehi-frontend`** (the Jawafdehi SPA — forked from the upstream
  `jawafdehi/jawafdehi` org so we can unify the frontend too). `origin` = our fork,
  `upstream` = the org for pull/sync.

New work goes on a feature branch → PR → `v2`. **Do local changes in git worktrees**
(`git worktree add <path> v2`), not by checking out branches in the primary tree (a
`git checkout` over unmerged/conflicted paths silently fails and tangles stashes).
Commits authored `oopsy <oopsy@claudy.com>`. Planning/sourcing artifacts live in this
repo's `docs/` tree.

_Note: an older `main` line still exists on the org remotes (it still carries the
Wagtail `content/` app — see §3.5). `v2` is the active trunk; `main` is not retired
and is the source for the CMS forward-port._
