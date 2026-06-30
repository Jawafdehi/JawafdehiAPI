# Jawafdehi Platform (monorepo)

All three services on **Django/DRF**, in one **uv workspace**, sharing a common
library — the framework-consolidation end-state (migrated from FastAPI/Poetry).

```
jawafdehi-platform/
  pyproject.toml          uv workspace root (no runtime deps; declares members)
  uv.lock                 single lockfile for the whole workspace
  shared/                 jawafdehi-shared: OIDC auth, entity-id contract, OpenSearch, DRF bases
  services/
    ngm/                  Django project (courts/cases/hearings, gated query, lakehouse svc)
    nes/                  Django project (entities, bulk-ingest, write API, search)   [pending]
    jawafdehi/            Django project (cases/sources/entities, casework, review)    [pending]
```

## Principles (locked decisions)
- **One framework**: Django/DRF everywhere → one auth (`jawafdehi_shared.auth.oidc`),
  one admin/migrations/test pattern. No duplicate FastAPI OIDC code.
- **Per-service dependency isolation**: each `services/<x>/pyproject.toml` declares
  ONLY that service's deps + `jawafdehi-shared`. A service image installs just its
  own deps — e.g. DuckDB/boto3 live on NGM only.
- **Database-per-service**: each service has its own settings + `DATABASES` pointing
  at its own DB; no shared Django models; cross-service access is REST-only.
- **Independent deploy**: each service its own `Dockerfile` + wsgi + image.
- **uv** (not poetry) for dependency management.

## Common commands
```bash
uv sync                                     # install the monolith (all apps) + dev tools
uv run python manage.py check               # one manage.py / one settings (monolith.config.settings)
docker build -t jawafdehi .                 # single image, context = repo root
```

## Status
- [x] Workspace skeleton + `shared/` (OIDC auth, entity-id contract, OpenSearch, DRF bases).
- [x] **NGM** — Django: courts/cases/hearings/entities/firms read plane, gated `/api/query`
      (SELECT-only guard + OIDC role), ingestion stubs, search 501, lakehouse svc ported. 19 tests.
- [x] **NES** — Django: Pydantic core + publication + bulk-ingest (≥2-source HOLD) reused;
      JSONB persistence via Django ORM; read + OIDC-gated write API. **Data-migration runner DROPPED**
      (see `services/nes/MIGRATION-DROPPED.md`). 19 tests.
- [x] **Jawafdehi** — moved into the monorepo; local `oidc_auth.py` DELETED → uses shared.
      Poetry→uv. 747 tests collect; OIDC/permission tests pass.
- [x] **docker-compose** (per-service Dockerfiles, uv builds, migrate sidecars) + infra.
      All 3 `manage.py check` clean; NGM image builds via uv + boots live (health 200,
      /api/courts/ 200, /api/query unauth 401 via shared OIDC).
- [ ] Build NES + Jawafdehi images + full `compose up` e2e re-prove (NGM proven)
- [x] Integration-test suite carried over; repo pushed to `origin`.

> **Note:** sections above describe the *pre-monolith-collapse* shape (separate
> per-service images, REST-between-services). The services have since been collapsed
> into one Django project / one image / in-process calls — see `../think-big/ARCHITECTURE.md`
> for the current state.

## Working model
Trunk is **`main`** (pushed to `origin`; the old `v2` line was retired 2026-06-29). New
work goes on a feature branch → PR → `main`. **Do local changes in git worktrees**
(`git worktree add <path> main` or a feature branch), not by checking out branches in the
primary tree. Commits authored `oopsy <oopsy@claudy.com>`.

See `../think-big/django-consolidation-plan.md`.
