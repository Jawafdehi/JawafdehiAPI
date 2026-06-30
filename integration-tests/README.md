# Jawafdehi Integration Tests

Cross-service integration tests for the unified Jawafdehi platform — **NES** (entity
service), **NGM** (governance data lake), and **Jawafdehi** (case platform). These
tests exercise the three former services *together* over their real REST APIs, in a
way no single service's own test suite can.

The platform is now a **single monolith**: all three apps run in ONE process behind
ONE host/port, mounted under distinct URL prefixes (the pre-monolith per-service
ports `:8081`/`:8082`/`:8000` are gone). The assertions match the live monolith's
real responses — see "Monolith topology & contract diffs" below.

## What it covers (vs. per-service tests)
Per-service tests cover their own logic in isolation. This repo tests the **seams**:
- The canonical entity-id contract (the schema.org `@id` IRI
  `https://jawafdehi.org/entity/<prefix>/<slug>`) honored across services.
- **NGM → NES** entity resolution (`court_case_entities.nes_id` is an NES `@id` IRI).
- The **unified search** endpoint (`/api/search/`) — one query across entities,
  materials, court cases, and published cases; the common result envelope.
- The **public discovery surfaces** (`/sitemap.xml`, `/.well-known/resourcesync`,
  `/robots.txt`).
- The **privacy boundary**: in-review/draft cases are casework — not publicly
  retrievable (404 for anon).
- **OIDC/Zitadel** auth across writes (no DRF tokens — OIDC-only).

## How it runs
Targets the monolith docker-compose stack. **Everything is on ONE host**
(`PLATFORM_BASE_URL`, default `http://localhost:48000`), plus Zitadel `:48080` and
OpenSearch `:49200`. The former services are reached by URL prefix on that host:

| Former service | Prefix on the monolith host |
|---|---|
| Jawafdehi      | `/api/...` (cases/, sources/, search/) |
| NES (entities) | `/api/nes/...` (entities, entity_prefixes, health) |
| NGM (gov data) | `/api/ngm/...` (courts/, cases/, query/, ingestion/) |
| Unified search | `/api/search/` |
| Discovery      | `/sitemap.xml`, `/.well-known/resourcesync`, `/robots.txt` |

`.env` / `.env.example` set the single `PLATFORM_BASE_URL`; the legacy
`NES_API_BASE_URL` / `NGM_API_BASE_URL` / `JAWAFDEHI_API_BASE_URL` vars are kept for
back-compat but all point at the SAME monolith host now.

```bash
# from integration-tests/, with the monolith already up:
python3 -m venv .venv && .venv/bin/pip install pytest httpx pytest-dotenv
.venv/bin/python -m pytest -q          # full suite against the live monolith
.venv/bin/python -m pytest -m smoke    # fast per-prefix contract checks
.venv/bin/python -m pytest -m "not live"  # pure-contract tests, no stack
```

### Rate throttling
The live API applies a DRF `AnonRateThrottle`, with rates env-tunable in
`monolith/config/settings.py` (`THROTTLE_RATE_ANON` / `THROTTLE_RATE_USER`). The anon
bucket defaults to a **generous 1000/hour** — far above what this fast, ~60-test suite
consumes in one run, so the full live suite passes without tripping a 429. (Throttling
is fully disabled only under the Django test runner, `TESTING`.) As a safety net the
suite still carries a client-side throttle-retry transport
(`conftest._ThrottleRetryTransport`) that rides out a momentary 429 and, only if it
persists, converts it into a *skip* (never a false contract failure). To tighten or
loosen the live rate, override `THROTTLE_RATE_ANON` on the `platform` service and
`docker compose up -d platform` (read at process start, no rebuild).

Tests that hit the running monolith are marked `@pytest.mark.live` and are gated by
the `require_stack` fixture: with the stack down they fail-fast by default, or skip
when `SKIP_IF_STACK_DOWN=1`. Pure-contract tests (the entity-id IRI shape checks in
`tests/cross_service/test_entity_id_contract.py`, seeded from `fixtures/sample_data.py`)
carry no `live` marker and run with no stack at all.

## Layout
```
tests/
  nes/            # NES API contract smoke tests (/api/nes/)
  ngm/            # NGM API contract smoke tests (/api/ngm/, gated /query/)
  jawafdehi/      # Jawafdehi API contract smoke tests (/api/)
  cross_service/  # the real prize: multi-service flows, unified search, discovery
fixtures/
  sample_data.py  # canonical sample records (NES entity, NGM case+party, doc source)
conftest.py       # one-host clients, OIDC token acquisition, live-test stack gating
Makefile          # up / down / test / test-smoke / test-contract
```

## Monolith topology & contract diffs
Reconciled against the live monolith (verified by curl + the live suite, 2026-06):

- **One host, prefixed mounts.** NES/NGM clients moved from `<service-host>/api/...`
  to `<host>/api/nes/...` and `<host>/api/ngm/...`. Jawafdehi keeps `/api/...`.
- **Entity ids are `@id` IRIs.** The join key is `https://jawafdehi.org/entity/<prefix>/<slug>`
  (clean-slate; the old `entity:<prefix>/<slug>` form is gone). Case IRIs
  (`https://jawafdehi.org/case/<slug>`) and court-case IRIs
  (`https://jawafdehi.org/courtcase/<court>/<case_number>`) also exist. NES list
  items carry the id under `@id`; the detail body is the raw schema.org JSON-LD doc.
- **NES health is slashless** — `GET /api/nes/health` (the `/health/` variant 404s).
  `{"status":"ok","service":"nes-api"}`.
- **NGM is uniformly trailing-slashed**, including the gated POSTs:
  `GET /api/ngm/courts/` (bare list `[...]`), `GET /api/ngm/cases/` (full DRF page
  with `count`), `GET /api/ngm/health/`, `POST /api/ngm/query/`, `POST
  /api/ngm/ingestion/{cases,entities/resolve,documents}/` — all OIDC-gated → 401
  unauthenticated, 401 "Invalid token" on a forged bearer. No-slash variants 301.
- **The NGM 501 `/api/ngm/search` stub is REMOVED** → 404. Search is unified.
- **Unified search** at `GET /api/search/` (public read) replaces the old
  cases-scoped `/api/search` AND the NGM 501 stub. Envelope:
  `{query, lang, page, page_size, count, counts, results}`. `q` is required
  (missing → 400). No-slash `/api/search` → 301. OpenSearch is a **hard
  dependency**: 200 when the cluster is up, 503 only if it is down.
- **Jawafdehi.** `/api/` root advertises `{cases, sources}` only — the old Jawafdehi
  `/api/entities/` router is GONE (404); entities are NES-owned at `/api/nes/`.
  `/api/cases/` is DRF-paginated (`count`). In-review/draft cases are casework →
  **404 for anon** (PUBLISHED-only public queryset). Legacy DRF `Authorization:
  Token …` is IGNORED (read → 200 as anonymous; write → 401), not parsed-and-rejected.
  `/admin/login/` → 200. `/api/cases` (no slash) → 301.
- **Roles.** caseworker / public (plus Admin/Moderator/ReadOnly); the public role is
  read-only and never sees casework states.

## Status
Repointed to the monolith (60 tests collected). Against the live stack the suite is
**green** — every live contract check passes, with only genuine PENDING-DATA
skips/xfails remaining (the NES entity store, NGM court tables, and the search index
are EMPTY today). Each carries a clear reason and contract-correct paths/shapes so it
flips green automatically once data lands. The two xfails are the NGM→NES `nes_id`
resolution joins (empty court tables); the remaining skips are the empty-corpus
round-trip / query / Phase-4 sourcing cases.

Last measured live run (1000/hour anon throttle): **52 passed, 6 skipped, 2 xfailed in
~0.9s** — zero 429-throttle skips.
