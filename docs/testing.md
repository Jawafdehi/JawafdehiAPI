# Testing & QA

Two tiers, plus a dedicated adversarial suite. All commands run from the repo root.

## Tier 1 — Unit gate (sqlite, no external services)

The whole `pytest` suite runs on sqlite with ZERO external services:
`config/settings_test.py` gives each DB-router alias (`default`/`nes`/`ngm`) its own
in-memory sqlite, so no Postgres/OpenSearch containers are needed. This is what CI
(`.github/workflows/ci.yml`) runs on every push/PR, and the fast local loop.

```bash
DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true uv run ruff check .
DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true uv run pytest -q --ignore=integration-tests
```

The unit gate includes `tests/mcp/`: the migrated MCP tool suite, a real ASGI
lifespan/initialize/tools-list exchange proving Django and MCP share
`config.asgi.application`, and a subprocess initialize/list/call test for the
retained stdio transport. Run that slice alone with:

```bash
DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true uv run pytest -q tests/mcp
```

- Lints with `ruff check` only (formatting is intentionally NOT gated).
- `integration-tests/` is excluded here — it is the live-stack suite (Tier 2).
- **Caveat (what the sqlite gate can hide):** a few behaviors differ from prod
  Postgres and are covered by targeted tests rather than the engine — the
  `cases` `tags` filter has a Postgres-only `tags__contains` branch
  (`tests/api/test_public_api.py::test_tag_filter_postgres_branch_uses_contains_lookup`),
  and DRF throttling is disabled under `TESTING`. The repo-root `conftest.py`
  auto-enrolls `databases="__all__"`, which relaxes router isolation — a
  narrowly-pinned counter-test lives in `tests/test_router_isolation.py`.

### Adversarial / penetration suite (`-m security`)

A subset tagged `@pytest.mark.security` exercises the platform from an attacker's
view — non-public case enumeration (IDOR), the role×endpoint escalation matrix,
gated-SQL injection, IRI traversal/SSRF, anonymous-submission privacy. Run it on
its own:

```bash
DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true uv run pytest -q -m security
```

The threat model it backs is [`security/threat-model.md`](./security/threat-model.md).

## Tier 2 — Live-stack E2E (real backend + frontend)

`integration-tests/` is a pytest + httpx suite that hits a RUNNING monolith over
HTTP (markers: `smoke` / `live` / `cross_service`). The frontend Playwright suite
(`jawafdehi-frontend/tests/e2e-pw/`) drives the SPA in a browser against that same
backend through the Vite proxy — the two together are the FE↔BE integration proof.

Both run against an isolated compose stack via the `docker-compose.e2e.yml` overlay
(distinct project name + ports so a running dev stack is untouched; Zitadel is
skipped because dev-login replaces OIDC for E2E). The strict boot order:

```bash
# from the API repo root
docker build -t jawafdehi-platform:e2e -f Dockerfile .
docker compose -p jawaf-e2e --env-file docker-compose.e2e.env \
  -f docker-compose.yml -f docker-compose.e2e.yml \
  up -d --wait postgres opensearch minio platform-migrate platform
docker compose -p jawaf-e2e exec -T platform uv run python manage.py seed_dev
docker compose -p jawaf-e2e exec -T platform uv run python manage.py reindex_all --rebuild

# API half:
cd integration-tests && PLATFORM_BASE_URL=http://localhost:48010 \
  SKIP_IF_STACK_DOWN=0 uv run pytest -v

# FE half (in the frontend repo):
VITE_API_PROXY_TARGET=http://127.0.0.1:48010 bunx playwright test

# tear down
docker compose -p jawaf-e2e --env-file docker-compose.e2e.env \
  -f docker-compose.yml -f docker-compose.e2e.yml down -v
```

- `reindex_all --rebuild` is MANDATORY after `seed_dev` and before any
  search-dependent assertion (a fresh index otherwise auto-maps fields wrong).
- API liveness is `GET /api/health` (no trailing slash).
- MCP is checked on the same host at `POST /mcp`; `GET /mcp/health` reports
  readiness after the MCP lifespan manager starts.
- E2E host ports bind to `127.0.0.1`. CI generates a fresh scope-only MCP query
  bearer; it is accepted only by `/api/query/` and maps to a no-group principal.
- OIDC-gated tests self-skip when no Zitadel is up (dev-login covers the
  authenticated surface).

### CI

`.github/workflows/integration.yml` runs this exact flow as a cross-repo gate
(it checks out the frontend repo alongside and runs both halves). Image
publication depends on both this live gate and the unit/security gate.
