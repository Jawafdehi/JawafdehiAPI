# Platform Architecture (current state)

_Last updated: 2026-06-28. This is the single source of truth for **what the
platform is now**. For per-doc status (what's current vs superseded) see
[`DOC-STATUS.md`](./DOC-STATUS.md)._

The program unifies three former systems — **NES** (Nepal Entity Service), **NGM**
(governance/judicial data lake), and **Jawafdehi** (anti-corruption case platform)
— into ONE platform. It went through a microservices design and then **reversed to
a monolith**; this doc describes the shipped result, not the journey.

## 1. Shape: one Django monolith, three databases

- **One Django project** at `/damodaha-volunteer/jawafdehi-platform/` (trunk `main`),
  one WSGI, one image, runs at **`:48000`**.
- **Apps:** `nes_service` (entities), `ngm_service` (courts + materials), the
  Jawafdehi apps (`cases`, `case_workflows`, `review`), and the monolith-level
  `search` + `discovery` apps. Project glue in `monolith/config/` (settings, urls,
  wsgi, `db_router.py`).
- **Database-per-service preserved** via a DB **router**: `entities` → `nes` DB,
  `courts`/`materials` → `ngm` DB, everything else → `default`. **No cross-DB FKs/
  joins** — query each DB, join in app. **Inter-app calls are in-process** (NOT REST;
  the microservices-over-REST design was reversed).
- **uv workspace** (not poetry). `shared/jawafdehi_shared/` holds the cross-app
  libs: `auth.oidc`, `entities.ids`, `search.opensearch` + `search.mappings` +
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

- IRI contract + validators live in `shared/jawafdehi_shared/entities/ids.py`
  (canonicalize-on-store, strict-validate, `MAX_IRI_LENGTH=300`).
- Validation is **minimal** (known `@type`, valid `@id`, `name` present) — see
  `nes_service/entities/validation.py` and `ngm_service/materials/jsonld.py`.
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
  `shared/jawafdehi_shared/search/mappings.py`; research in
  `shared/research/opensearch-bilingual-nepali.md`.
- **Hard dependency**: cluster down → **503**, no in-process fallback.
- Relevance tuning (field boosts + exact-phrase boost + `lang` re-rank + per-type
  `indices_boost`) and **`search_after` cursor deep-paging** are built.
- `analysis-icu` is **baked into a custom image** (`infra/opensearch/Dockerfile`).
- The `@id` envelope also drives **ResourceSync + Sitemaps** (`monolith/discovery/`).

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

Two repos under the `damodaha` account, **both with `main` as the single trunk** (the old
`v2` lines were retired 2026-06-29): **`damodaha/jawafdehi-platform`** (the Django monolith
backend, greenfield) and **`damodaha/jawafdehi-frontend`** (the Jawafdehi SPA — forked from
the upstream `jawafdehi/jawafdehi` org so we can unify the frontend too; `origin` = our
fork, `upstream` = the org for pull/sync). All work happens on `main` — new work on a
feature branch → PR → `main`. **Do local changes in git worktrees** (`git worktree add
<path> main`), not by checking out branches in the primary tree (a `git checkout` over
unmerged/conflicted paths silently fails and tangles stashes). Commits authored
`oopsy <oopsy@claudy.com>`. Planning/sourcing artifacts live in this repo's `docs/` tree.
