# `entities/sourcing/` — external-source ENTITY ingestion

One subpackage per external source that feeds **entities** (NES) into the platform
(`ocr/` — Nepal Office of the Company Registrar, the company registry). The direct
analog for **materials** is `materials/sourcing/` — read its README for the shared
philosophy; this file records where entities differ.

## Convention

Each source lives in `entities/sourcing/<source>/` and owns:

- **`shaper.py`** — a **pure, DB-free** projection from the scraped record to an
  entity *authoring payload* (`{prefix, slug, type, name, …}`) accepted by
  `entities.write_validation.validate_create_payload`. It mints its `@id` via
  `jawafdehi_shared.entities.ids.build_entity_iri`, converts BS↔AD dates via
  `jawafdehi_shared.dates`, and romanizes names for the slug via
  `jawafdehi_shared.search.transliterate`. Unit-test it like any pure function
  (see `entities/tests/test_ocr_shaper.py`).
- **crawl / parse helpers** as needed (e.g. `ocr/crawl.py`).

## Ingestion is via the API plane — the crawler is an HTTP client

Like the materials sourcing pipelines, an entity source pipeline is an **HTTP
client** of the platform's write API — it does **not** import models and write
rows. The OCR crawler `POST`s each shaped authoring payload to `POST /api/entities`
(Bearer-authenticated, `Caseworker`-role gated — see `entities/permissions.py`).

Two properties of the entity create path shape the crawler (they differ from the
materials upsert):

1. **Not idempotent.** `POST /api/entities` returns **409 CONFLICT** on a duplicate
   `@id` (it is a create, not an upsert — `entities/services/publication/service.py`
   raises "already exists"). The crawler therefore treats **409 as a success/skip**
   signal and checkpoints the id, so re-runs are safe. Use `PATCH /api/entities/{ref}`
   to change an existing entity.
2. **Ungated.** The `≥2-source` HELD gate lives only in the ORM `bulk_ingest`
   command, **not** on the HTTP create path — so a single-publisher source
   (`authority: ocr.gov.np`) publishes immediately. If corroboration is later wanted,
   the alternative is a 2nd source + `manage.py bulk_ingest`.

The runner is a **standalone control-plane API script** — `python -m
entities.sourcing.ocr.crawl` — not a management command, because the write path is
pure HTTP (the crawler is a client of `/api/entities`, exactly like the NKP
materials crawler is a client of `/api/materials/`). A k8s CronJob invokes the
module off-peak. Auth:

- **Production / cluster:** omit `--token` — a `Caseworker` bearer is minted from
  the `sa-ingestion` OIDC client-credentials env (else `CASEWORK_OIDC_*`) by
  `review.oidc_client_credentials.resolve_service_bearer`, so no static token is
  baked in. Or pass `--token` explicitly.
- **Local dev:** run the platform with `DEV_AUTH=true` (under `DEBUG`) and pass
  `--basic-auth USER:PASS` for a seeded `Caseworker` / superuser — a bearer-free
  path that skips Zitadel entirely.

```bash
# local end-to-end against a DEV_AUTH server on sqlite:
python -m entities.sourcing.ocr.crawl --cache /tmp/ocr.jsonl \
    --api-base http://127.0.0.1:8000 --basic-auth admin:secret --id-max 500
```
