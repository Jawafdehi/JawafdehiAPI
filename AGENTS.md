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
uv run ty check                            # type check — ADVISORY, not a gate
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

**What the lint gate enforces** (`[tool.ruff.lint]` in `pyproject.toml`): the
defaults (`E4`/`E7`/`E9`/`F`) tree-wide, plus `BLE` tree-wide, plus `ANN` (minus
`ANN401`) **in `lakehouse/` only**. `ANN` is selected globally and switched off
per-directory in `per-file-ignores`, because ruff has no select-for-one-path
primitive — so a directory opts *in* by deleting its line there, and only once
its `ruff check <dir> --select ANN` count is already 0. Tests are exempt
permanently.

**`ty` is advisory, not a gate** — `uv run ty check`, and CI runs it with
`|| true`. It is pre-1.0 with no Django plugin, so it cannot see through the
model metaclass: at adoption it reported 1304 diagnostics of which ~800 were
`Model.objects` / DRF `force_authenticate` false positives.
`[tool.ty.rules]` silences those families, leaving **30 real ones** — worth
reading, and the list to drive to zero before making it a gate.

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

**The five enricher `main()`s are no longer a clone — do not "extract" them.**
`enrich_{allegations,missing_bigo,related_entities,tags,timeline}` once opened
with the same ~30-line prologue, and jscpd flagged it. That is fixed: the shared
selection step is `casework.common.select.select_for_run`, and jscpd now reports
**0 clones** across the five. What remains looks repetitive but is not
interchangeable — `enrich_missing_bigo` alone carries a `_die()` helper, a
pre-bootstrap `run/start` event, `SystemExit` handling for the
missing-credential path (`basic_auth_from_env` raises a BaseException, not an
Exception) and paged list progress. A prologue extraction that treats all five
as identical silently deletes those. Verified 2026-08-04 by measuring: jscpd over
just those five files reports 0 duplicated lines.

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

Trunk is **`main`**. `origin` = the org (`Jawafdehi/JawafdehiAPI`), `fork` =
`damodaha/jawafdehi-api` — note the fork's repo name does NOT match the org's
(`jawafdehi-api` vs `JawafdehiAPI`), so `gh repo fork` reports "already exists"
while `gh repo view damodaha/JawafdehiAPI` 404s. Push branches to `fork` and
file the PR on the org; never push a branch to `origin`.

**`main` goes stale fast.** Fetch and rebase before doing anything else: this
tree sat 27 commits behind for one session's work, which was enough for upstream
to independently refactor the same duplication (see the enricher note below).

**Do local changes in git worktrees** (`git worktree add <path> -b <branch> main`),
not by checking out branches in the primary tree — the primary tree holds `main`
and other worktrees are already checked out against it.

Commits are authored `oopsy <oopsy@claudy.com>`. Conventional-commit subjects with
the touched area in parens: `fix(courts): …`, `chore(materials): …`,
`feat(proposals): …`.

The deprecated `v2` branch still exists on the org remotes but is no longer trunk;
do not push to it.

## Known debt (measured 2026-08-04, don't rediscover)

- **No F-grade blocks remain.** All three were decomposed on 2026-08-04:
  `cases/api_views.py` `partial_update` F/50→C/17, `review/scorer.score_case`
  F/48→B/9, `cases/search_index.build_doc` F/41→B/8. Radon over the tree now:
  5246 A · 507 B · 190 C · 25 D · 4 E · 0 F.
- The 4 remaining **E-grade** blocks, in descending cost:
  `entities/management/commands/merge_persons.py:95` `handle` E/40,
  `courts/scraper/supreme.py:370` `parse_hearings_and_timeline` E/34,
  `tests/e2e/test_public_api_e2e.py:101` E/34 (a test, low value to split), and
  `casework/enrich_related_entities.py:364` `main` E/33.
  `cases/api_views.py` is still a 1989-line module even after the split.
- **`ty`'s 30 real diagnostics** (`uv run ty check`). Mostly Django-shaped and
  low-value (`__str__` returning `CharField` not `str`, `QuerySet.as_manager`),
  but read them before dismissing: two genuine bugs came out of the first pass —
  `case_scraper.py` called `len()` on an Optional `response.text` (TypeError on a
  blocked Gemini candidate), and `ciaa_draft_case_service.convert_bs_to_ad` was
  annotated `Optional[datetime]` while `bs_to_ad` returns `date`.
- **`ANN` is enforced in `lakehouse/` only.** Per-directory source-only counts,
  cheapest first (re-measured after the 2026-08-04 upstream rebase): `newsletter`
  5, `discovery` 31, `jobs` 34, `content` 57, `case_proposals` 78, `case_events`
  81, `llm` 163, `casework` 362. Annotate one, then delete its
  `per-file-ignores` line. `case_events` is new upstream (the NATS bus) and got
  the same opt-out on arrival.
- **Other ruff families worth enabling**, ~640 findings and largely auto-fixable
  (re-measured 2026-08-04 against upstream `b83e39b`): `UP` 295, `SIM` 106
  (112 on upstream `main`; this branch cleared 6), `I` 90, `PTH` 47, `C4` 40,
  `B` 33 (24 are `B904` raise-without-from), `DTZ` 9, plus `RUF100` 19 *unused*
  noqa — free deletion, and note 8 of those are `# noqa: BLE001` on handlers
  that log or re-raise, which BLE001 never flags. Measure `RUF100` with
  `--extend-select`, never `--select`: the latter turns the other families off,
  so every `# noqa: BLE001` in the tree then looks unused (121 false hits).
  Skip `S` (the overwhelming majority are test asserts; the rest are env-var
  names, not secrets) and `ARG` (Django signal/override signatures).
- **Coverage is never measured** — no `pytest-cov`, nothing in CI.
- `review.scorer.score_case` builds each rule dict with a trailing `**fields`
  spread, so the per-rule KEY ORDER differs from the pre-decomposition version
  (`score`/`confidence`/… now come after `description`/`good_examples`/
  `bad_examples`). Values are identical — verified by golden-output diff across
  8 scenarios. It only matters because the result lands in `ReviewRun.result`
  (a `JSONField`): don't add a hash/equality check over the serialized form
  without normalizing key order first.
- `TRY400` at `jawafdehi_shared/drf/throttling.py:93` and `newsletter/views.py:171`
  log `.error` where `.exception` would keep the stack trace.
