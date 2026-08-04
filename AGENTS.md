# AGENTS.md — jawafdehi-api

Guidance for coding agents working in this repo. Human-facing docs stay where they
are: [`README.md`](./README.md) for quickstart,
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for the authoritative current
state, [`docs/DOC-STATUS.md`](./docs/DOC-STATUS.md) for per-doc trust status.
**Read DOC-STATUS before trusting any other doc** — several describe a dead
microservices era and are bannered as STALE.

This file records only what you cannot infer by reading the code, and the traps
that look like bugs but are load-bearing.

## Commands

```bash
uv sync                                    # install app + dev tools
uv run ruff check .                        # the lint gate (exactly what CI runs)
uv run python manage.py check
DJANGO_SETTINGS_MODULE=config.settings_test TESTING=true \
  uv run pytest -q --ignore=integration-tests    # full suite, ~2.3 min, no services
```

The whole suite runs on sqlite with **zero external services** — three in-memory
databases, one per router alias. Do not stand up Postgres/OpenSearch/MinIO to run
it. `--ignore=integration-tests` matters: `integration-tests/` is a separate live-stack
suite that *errors* rather than skips when the stack is down.

Run one test: `uv run pytest tests/api/test_public_api.py -k name`. Security suite
only: `uv run pytest -m security`.

`ruff format` is deliberately **not** gated — the tree predates it and running it
would reflag ~150 files. Don't reformat files you aren't otherwise editing.

## Traps — things that look wrong and are not

**Function-local cross-app imports: leave them alone, but they are not all
cycles.** `materials`↔`cases`, `review`↔`cases`, `jobs`↔`review` and
`courts`↔`materials` all import each other, and the reverse direction is usually
done inside a function body (`materials/visibility.py:90`,
`review/case_provider.py:35`, `review/serializers.py:146`) while the forward
direction is module-level (`cases/services/statistics.py:34`).

Measured 2026-08-04: hoisting `materials/visibility.py:90` and
`review/case_provider.py:35` to module level **does not** raise ImportError under
`django.setup()`. So these are defensive rather than strictly load-bearing.
Treat them as intentional anyway — they are cheap insurance against app-loading
order changing, and the 492 `PLC0415` findings are not a safe bulk-fix target. If
you do hoist one, prove it with a full `django.setup()` plus the suite, and do it
as its own change.

**No cross-database queries, ever.** `config/db_router.py` pins `entities`→`nes`,
`courts`/`materials`→`ngm`, everything else→`default`. No `ForeignKey` may cross a
database. Cross-app access is in-process but joins **in Python**, not in SQL — see
`cases.services.nes_resolver` for the pattern. A `select_related`/`join` spanning
two apps in different aliases will fail at runtime, and the sqlite test setup is
built specifically to catch it (distinct `TEST["NAME"]` per alias, so the aliases
cannot silently collapse into one database).

**`assert` in tests is correct here.** ~4.3k of them. Ruff's `S101` would flag
every one; that rule family is intentionally not enabled. Descriptive asserts are
the point.

**`@id` IRIs are the canonical stored form**, not integer PKs:
`https://jawafdehi.org/entity/<prefix>/<slug>`, `/material/<source>/<ident>`,
`/case/<slug>`, `/courtcase/<court>/<case_number>`. The legacy
`entity:<prefix>/<slug>` scheme is gone — do not reintroduce it.

**Deliberate `try/except/pass` sites are annotated.** Where swallowing is
intended it carries a `# noqa: BLE001` plus a reason (`review/runner.py:50`
progress reporting, `jobs/registry.py:76` consumer-import wiring). Don't add new
silent handlers, and don't "fix" the annotated ones without reading the note.

**Search is a hard dependency.** OpenSearch down = 503, by design. There is no
fallback path to add.

**Auth is OIDC/Zitadel only.** DRF token auth was dropped. Don't reach for
`TokenAuthentication`.

## Conventions

- **uv, not poetry.** One top-level `pyproject.toml` + `uv.lock`. There is no uv
  workspace and no per-service packages: every app is a plain top-level import
  package.
- **In-process inter-app calls, not REST.** The one exception is the async
  review/jobs poller (`review/ngm_client.py`, `review/jds_client.py`), which is an
  OIDC HTTP client by design and can target a remote portal.
- **Cross-cutting libs live in `jawafdehi_shared/`** (auth/oidc, identity,
  middleware, logging_config, entities/ids, search, drf). Prefer extending it over
  a second copy. If you deliberately leave a duplicate, document why at both
  sites — `jawafdehi_shared/jsonpatch_ops.py` does this for the
  `normalize_patch_ops` copy still in `entities/write_validation.py` (converging
  it needs NES patch-path test coverage first).
- **Each app owns its own `tests` package** (`entities/tests/`, `courts/tests/`,
  …) so module names stay unique. The Jawafdehi suite lives at the repo-root
  `tests/` package.
- Wrap new `models.URLField` as `cases.fields.HttpsURLField`, or pass
  `assume_scheme="https"` — a bare one emits `RemovedInDjango60Warning`. Do **not**
  set `FORMS_URLFIELD_ASSUME_HTTPS`; that transitional setting is itself deprecated
  and warns on assignment (verified: `django/conf/__init__.py:239`). Swapping the
  field class needs a state-only `AlterField` migration (see `cases/migrations/0055`).
- **The suite must stay at zero warnings.** It was 916 before 2026-08-04 (98% one
  repeated whitenoise `UserWarning`) and is 0 now. A new warning is a real signal;
  keep it that way rather than letting volume rebuild.

## Working model

Trunk is **`main`**. `origin` = the org (`Jawafdehi/JawafdehiAPI`), `fork` = the
`damodaha` personal fork; PRs are filed on the org from the fork.

**Do local changes in git worktrees** (`git worktree add <path> -b <branch> main`),
not by checking out branches in the primary tree — the primary tree holds `main`
and other worktrees are already checked out against it.

Commits are authored `oopsy <oopsy@claudy.com>`. Conventional-commit subjects with
the touched area in parens: `fix(courts): …`, `chore(materials): …`,
`feat(proposals): …`.

The deprecated `v2` branch still exists on the org remotes but is no longer trunk;
do not push to it.

## Known debt (measured 2026-08-04, don't rediscover)

- `cases/api_views.py:801` — `partial_update` is a ~400-line method, cyclomatic
  complexity 31, inside a 1895-line module. The hottest maintenance risk in the
  tree and the biggest obstacle to working on the case-write path.
- **No type checker configured.** `mypy --check-untyped-defs` over source (tests
  excluded) reports 128 errors / 358 files, but a large share are false positives
  from missing `django-stubs` — mypy reads `models.TextChoices` as
  `tuple[str, str]`. Add `django-stubs` + `djangorestframework-stubs` and
  re-baseline *before* concluding anything from that number. Ruff `ANN` over the
  same scope: 2317 findings across ~1500 functions.
- **No `[tool.ruff]` section**, so the gate is defaults only (`E4`/`E7`/`E9`/`F`).
  Measured candidates worth enabling, ~500 findings and largely auto-fixable:
  `UP` 253, `I` 81, `SIM` 74, `PTH` 43, `C4` 32, `B` 30 (24 are `B904`
  raise-without-from), `DTZ` 7, plus `RUF100` (105 *unused* noqa — free deletion).
  Skip `S` (4358 of 4514 are test asserts; the rest are env-var names, not secrets)
  and `ARG` (Django signal/override signatures).
- **Coverage is never measured** — no `pytest-cov`, nothing in CI.
- `TRY400` at `jawafdehi_shared/drf/throttling.py:93` and `newsletter/views.py:171`
  log `.error` where `.exception` would keep the stack trace.
