# Jawafdehi API (backend)

One **Django/DRF** project that unifies three former systems — **NES** (entities),
**NGM** (courts + materials / governance data lake), and **Jawafdehi** (anti-corruption
case platform) — into a single app, in one **uv workspace**. It went through a
microservices design and then reversed to this monolith (the "R1 collapse"), so the
old `monolith/` + `services/{nes,ngm,jawafdehi}/` layout is gone.

**For the authoritative current-state description read
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md); for per-doc trust status read
[`docs/DOC-STATUS.md`](./docs/DOC-STATUS.md).** This README is just a quickstart.

## Layout

```
jawafdehi-api/
  pyproject.toml          uv workspace root + deps
  uv.lock                 single lockfile
  config/                 project glue: settings.py, urls.py, wsgi/asgi, db_router.py
  jawafdehi_shared/       cross-app libs: auth.oidc, entities.ids, search.*, drf.base
  entities/               NES — entities, bulk-ingest, write API      (→ nes DB)
  courts/                 NGM — court cases, firms, ingestion          (→ ngm DB)
  materials/              NGM — universal material store, conversion   (→ ngm DB)
  cases/                  Jawafdehi — cases (owns no docs; links by IRI) (→ default)
  review/                 Jawafdehi — casework review + poller          (→ default)
  jobs/                   central Postgres job queue                    (→ default)
  search/                 unified OpenSearch query plane (all 4 types)
  discovery/              ResourceSync + Sitemaps off the @id envelope
  lakehouse/              DORMANT Iceberg/DuckDB seam (not a live path)
  content/                headless Wagtail CMS (Newsroom) — NOTE: on `main`,
                          being forward-ported to `v2`; see ARCHITECTURE §3.5
  docs/                   design docs + sourcing artifacts
```

## Principles (locked decisions)
- **One Django project**, one image, in-process inter-app calls (NOT REST between
  apps). Exception: the async review/jobs poller is a separate OIDC HTTP client by
  design and can target a remote portal.
- **Database-per-service preserved** via `config.db_router.ServiceDatabaseRouter`:
  `entities`→`nes`, `courts`/`materials`→`ngm`, everything else→`default`. No
  cross-DB FKs/joins — join in app.
- **OIDC/Zitadel only** for auth (DRF token auth dropped); local JWKS via PyJWT.
- **uv** (not poetry) — one top-level `pyproject.toml` + `uv.lock`.
- **schema.org JSON-LD keyed by `@id` IRIs** is the canonical stored form.

## Common commands
```bash
uv sync                                      # install app + dev tools
uv run python manage.py check                # one manage.py / one settings (config.settings)
uv run python manage.py runserver 0.0.0.0:48000
docker build -t jawafdehi .                  # single image, context = repo root
```

For the local dev stack (Postgres ×3, OpenSearch, MinIO) see `docker-compose.yml`.

## Working model
Trunk is **`v2`**. `origin` = the org (`Jawafdehi/JawafdehiAPI`), `fork` = the
`damodaha` personal fork; PRs are filed on the org from the fork. New work goes on a
feature branch → PR → `v2`. **Do local changes in git worktrees**
(`git worktree add <path> v2`), not by checking out branches in the primary tree.
Commits authored `oopsy <oopsy@claudy.com>`.

_An older `main` line still exists on the org remotes and still carries the Wagtail
`content/` app; `v2` is the active trunk and the CMS is being ported forward from
`main` — see `docs/ARCHITECTURE.md` §3.5._
