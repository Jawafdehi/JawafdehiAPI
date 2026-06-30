# Platform Documentation

A program that unified the three Jawafdehi systems — **NES** (Nepal Entity Service),
**NGM** (governance/judicial data lake), and **Jawafdehi** (anti-corruption case
platform) — into **one accountability platform**, and is now deepening NES toward
~1M public entities.

This `docs/` tree holds the **planning, design, research, and per-bucket sourcing
artifacts** that span the platform and outlive any single PR. The live code is the
Django monolith in this repo (trunk `main`, runs at `:48000`).

## Start here

| If you want… | Read |
|---|---|
| **What the platform IS right now** | [`ARCHITECTURE.md`](./ARCHITECTURE.md) — single source of truth for the current state |
| **Which doc to trust vs. ignore** | [`DOC-STATUS.md`](./DOC-STATUS.md) — per-doc audit (CURRENT / STALE) |
| **What's actually been sourced into NES** | [`nes/sourcing/SOURCED-INDEX.md`](./nes/sourcing/SOURCED-INDEX.md) — live inventory + data-completeness gaps + areas to explore |
| **Which HELD entities can be promoted** | [`nes/sourcing/HELD-PROMOTION-ANALYSIS.md`](./nes/sourcing/HELD-PROMOTION-ANALYSIS.md) |

## The current platform in one breath

One Django monolith (`:48000`), three Postgres DBs via a router (`nes` / `ngm` /
`default`), **no REST between apps — in-process calls**. The canonical stored form
for NES entities AND NGM materials is **schema.org JSON-LD keyed by `@id` IRIs**
(`https://jawafdehi.org/entity|material|case|courtcase/…`). **Unified bilingual
(EN+Nepali) OpenSearch** replaces all per-app search (hard dependency: down = 503).
**OIDC/Zitadel only** (DRF tokens dropped); roles admin / moderator / caseworker /
readonly / public. Lakehouse = Cloudflare R2 + Apache Iceberg + DuckDB, all
free/OSS. Full detail in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

## How NGM, NES, and Jawafdehi relate

```
NGM (governance/corruption data)      NES (entity service)        Jawafdehi (case DB)
 court records + doc modality   ──→    public entities       ──→    accountability cases
 nes_id IRI resolution    ←─────────   shared resolution      ←───   entity links
```

NGM and NES share a **bilingual entity-resolution layer** and a **privacy boundary**:
NGM is the only doorway through which a private individual (a plaintiff/defendant)
may enter NES, with minimal PII.

## Directory layout

| Dir | Scope |
|---|---|
| `nes/sourcing/` | Live NES sourcing docs (`sourcing-plan.md`, `sourcing-methodology.md`, `sourcing-readiness-matrix.md`, `SOURCED-INDEX.md`, `HELD-PROMOTION-ANALYSIS.md`) + per-bucket `RESULTS.md` run records |
| `ngm/` | `ngm-source-inventory.md` (the source blueprint) + `r2-site-retirement-plan.md` |
| `jawafdehi/` | Jawafdehi-specific plans (NGM frontend integration, sources→NGM materials) |
| `shared/` | Cross-system: `source-acquisition-pipeline.md` (TLS fetch + likhit/OCR/LibreOffice), `entity-resolution-service.md` (bilingual matcher design), `research/` (deep-research outputs — tech findings) |

**Top-level docs:** `ARCHITECTURE.md`, `DOC-STATUS.md`, `README.md`,
`unified-search-plan.md`, `case-workflows-retirement-plan.md`.

## Program-wide hard rules (still binding)

- **PUBLIC entities only** in NES — incl. cooperatives, NGOs/INGOs, private
  contractors (public contracts), civil-service **leadership** (gazetted/secretary
  tier); EXCLUDE low-level civil servants. Private individuals enter NES **only** via
  the NGM plaintiff/defendant carve-out (minimal PII, no family fields).
- **≥2 independent-publisher sources** for every NES entity before it publishes —
  a mandatory gate, not a flag. Single-source entities are **HELD**. Enforced by the
  `bulk_ingest` count gate (keyed on source `authority`).
- **Official registries, not scraping** (the web/Wikipedia scraper was removed).
- **FREE / OSS only** — no paid licenses/tiers/managed SaaS; pay-per-use R2 OK. See
  [`shared/research/COST-AUDIT.md`](./shared/research/COST-AUDIT.md).
- **1M target is aspirational** — authoritative verified ceiling ≈ 250k–450k;
  1M only via the high-volume expansion buckets as access blockers unlock.

## Live sourcing status (in the running `nes` DB, 2026-06-29)

| Bucket | Count |
|---|---|
| Published persons / orgs / locations | 160,909 / 20,644 / 837 |
| **NES total (published)** | **182,390** (≥2-source, plus the ECN election-authority exception) |
| HELD (single/same-publisher, recoverable) | **~1,900** |
| NGM materials | 2,122 |

The six-figure inflection came from the 2026-06-29 ECN harvest (local candidates,
ward chairs, provincial/federal candidates). **Canonical per-bucket counts, gaps, and
held composition live in [`nes/sourcing/SOURCED-INDEX.md`](./nes/sourcing/SOURCED-INDEX.md)**;
held-promotion analysis in
[`nes/sourcing/HELD-PROMOTION-ANALYSIS.md`](./nes/sourcing/HELD-PROMOTION-ANALYSIS.md).

## Working model

Two repos under the `damodaha` GitHub account, **both with `main` as the single trunk**
(the old `v2` lines were retired 2026-06-29):
- **`damodaha/jawafdehi-platform`** — the Django monolith backend (this repo).
  Greenfield repo, no prod `main` to protect.
- **`damodaha/jawafdehi-frontend`** (`/damodaha-volunteer/frontend/`) — the Jawafdehi SPA
  (Vite/React), **forked** from the upstream org so we can unify the frontend too. Its
  `origin` = our fork; `upstream` = `jawafdehi/jawafdehi` (pull/sync only).

All work happens on `main`: new work goes on a feature branch → PR → `main`. **Do local
changes in git worktrees** (`git worktree add <path> main` or a feature branch), not by
checking out branches in the primary tree — `git checkout` with unmerged/conflicted paths
silently fails and tangles stashes. Commits authored `oopsy <oopsy@claudy.com>`.
