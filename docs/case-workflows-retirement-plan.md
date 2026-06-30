# Phase 5c — Retire `case_workflows`, drop the LangChain/LangGraph/deepagents stack

**Goal (2026-06-28):** remove the agentic casework engine (`case_workflows`) from
Jawafdehi and delete the entire LangChain/LangGraph/deepagents/Gemini dependency
tree it is the sole reason for. End-state: the service boots, builds, and serves
with **zero** `langchain*` / `langgraph` / `deepagents` imports.

This mirrors the precedent already set in `cases/migrations/0027_drop_caseworker_tables.py`,
which removed the *previous* agent app (`caseworker`: Skills/Summaries/Drafts/MCP/
LLM providers) and cleaned up its orphaned tables. `case_workflows` is the
second-generation engine and is retired the same way.

## Why this is on the table
- `case_workflows` is the **only** mandatory consumer of the LLM/agent stack.
  The pyproject comment (`services/jawafdehi/pyproject.toml:42-47`) already records
  that these deps are direct-and-mandatory *solely* because `CaseWorkflowsConfig.ready()`
  → `registry.autodiscover()` imports the workflow + middleware at Django startup.
  Remove the app and that justification disappears.
- It's a maintenance + supply-chain liability: a fast-moving 7-package stack
  (`langchain` 1.x, `langchain-core`, `langchain-openai`, `langchain-google-genai`,
  `langchain-mcp-adapters`, `langgraph`, `deepagents`) carried for one workflow that
  runs as an offline batch job (`run_case_workflow` / `discover_and_draft_cases`),
  not on any request path.
- The workflow ships a `run_command` tool that **executes arbitrary shell** (see the
  security note in `workflows/ciaa_caseworker/workflow.py:1-19`). Retiring it removes that
  surface from the codebase entirely.

## Precise dependency map (verified this session)

### LangChain/LangGraph/deepagents — used ONLY inside `case_workflows`
| Package | Where | Drops with the app? |
|---|---|---|
| `langchain`, `langchain-core` | `workflow.py:442`, `unicode_repair_middleware.py`, `encoding_tool.py`, `workflows/ciaa_caseworker/workflow.py` | ✅ |
| `langchain-google-genai` | `workflow.py:78` (Gemini tool-schema monkeypatch) | ✅ |
| `langchain-mcp-adapters` | `workflow.py:444` (`MultiServerMCPClient`) | ✅ |
| `langgraph` | `unicode_repair_middleware.py:11-12` | ✅ |
| `deepagents` | `workflow.py:440` (`create_deep_agent`, `FilesystemBackend`) — **note: imported at runtime but NOT declared in pyproject; it's an undeclared/implicit dep today** | ✅ |

### Stragglers — NOT covered by retiring `case_workflows`
- **`langchain-openai`** — one user outside the app:
  `cases/management/commands/enrich_ciaa_tags.py:188` (`ChatOpenAI` in `_build_llm_client`).
  Must be handled separately (decision below) before `langchain-openai` can go.
- **`openai`** (raw SDK, not LangChain) — used by
  `cases/management/commands/enrich_missing_bigo.py:810`. **Stays regardless** —
  out of scope for this plan, keep the dep.

### Cross-app coupling (the one real snag)
`case_workflows/permissions.py` defines `IsAdminOrModerator` and
`IsAdminOrModeratorOrContributorReadOnly`. The viewset in the app uses them, **but**:
- `review/views.py` imports `IsAdminOrModerator` from **`review.permissions`**
  (its own copy, line 27-32) — *not* from `case_workflows`. So `review` is **not**
  coupled. ✅
- Only `tests/test_contributor_read_permissions.py:12` imports
  `IsAdminOrModeratorOrContributorReadOnly` from `case_workflows.permissions`.
  That test goes away with the app (the permission class is workflow-run-scoped).

### Wiring to unpick (all confirmed present)
- `monolith/config/settings.py:269` — `"case_workflows"` in `INSTALLED_APPS`.
- `monolith/config/settings.py:653-659` — `CASE_WORKFLOWS_WORK_DIR` setting + env.
- `monolith/config/urls.py:16,68` — `path("api/case-workflows/", include("case_workflows.urls"))`.
- `monolith/config/db_router.py:14` — comment only (routes to `default`); no real binding.
- DB: one model `CaseWorkflowRun` (table `case_workflows_caseworkflowrun`), migrations
  `0001_initial`, `0002_alter_caseworkflowrun_case_id`.
- No frontend consumer (grep for `case-workflows` / `CaseWorkflowRun` in TS/TSX = none).
- No MCP/poller consumer of `/api/case-workflows/runs/` (grep = none).
- `docker-compose.yml` has no workflow worker/service entry.

## The one open decision — `enrich_ciaa_tags`
`enrich_ciaa_tags` is a `cases` management command (CIAA tag enrichment) that is
**independent** of `case_workflows` but uses `langchain_openai.ChatOpenAI` via an
OpenAI-compatible `base_url`. It's not scheduled anywhere we can find (no compose/
cron/CI reference). To fully drop `langchain-openai`, pick one:

- **(A) Port to the raw `openai` SDK** *(recommended)* — `openai` is already a
  mandatory dep (kept for `enrich_missing_bigo`). `_build_llm_client` becomes an
  `openai.OpenAI(base_url=…, api_key=…)` chat-completions call. Net: kills
  `langchain-openai` with ~one function rewritten, no capability lost.
- **(B) Retire the command too** — if CIAA tag enrichment is dead/superseded,
  delete it alongside the app. Cleanest, but only if confirmed unused.
- **(C) Keep `langchain-openai`** — leave the command as-is; drop the other 6
  packages but retain this one. Smallest blast radius, incomplete cleanup.

**Recommendation: (A).** It achieves the full "drop langchain" goal with the
already-present `openai` dep and doesn't gamble on the command being dead.

## Execution (on a feature branch off `main`, worked in a git worktree)

Ordering is dependency-driven; steps 1–2 are independent of 3.

1. **Resolve the straggler** (`enrich_ciaa_tags`): apply decision A/B/C above.
   If A: rewrite `_build_llm_client` onto `openai.OpenAI`; keep behavior + the
   graceful-ImportError UX. If B: delete the command + its test.
2. **Delete the app**: remove the `case_workflows/` package (models, views, urls,
   serializers, admin, permissions, registry, workflow engine, middleware,
   encoding_tool, output, storage_utils, the `ciaa_caseworker` template, tests,
   management commands `run_case_workflow` + `discover_and_draft_cases`).
3. **Unwire Django**:
   - drop `"case_workflows"` from `INSTALLED_APPS` (settings.py:269);
   - remove the `api/case-workflows/` include (urls.py:16,68);
   - remove `CASE_WORKFLOWS_WORK_DIR` + its env block (settings.py:653-659) and the
     db_router comment (db_router.py:14);
   - remove `tests/test_contributor_read_permissions.py` (its sole import target is gone).
4. **Drop the dependencies** from `services/jawafdehi/pyproject.toml`: delete
   `langchain`, `langchain-core`, `langchain-google-genai`, `langchain-mcp-adapters`,
   `langgraph`, and (pending step 1) `langchain-openai`. **Keep `openai`.** Delete
   the now-obsolete `llm-all`-rationale comment block (lines 42-47). `pyproject.toml`
   is the single uv-workspace member for the service, so this is the only dependency
   manifest to edit — re-lock the uv workspace after the edit so the lockfile matches.
5. **Drop the tables** (new migration in a surviving app, exactly like 0027):
   `cases/migrations/00XX_drop_case_workflows_tables.py` →
   `DROP TABLE IF EXISTS case_workflows_caseworkflowrun;` +
   `DELETE FROM django_migrations WHERE app = 'case_workflows';`
   (idempotent on fresh DBs, irreversible reverse = no-op — copy 0027's shape).
6. **Docker**: the image build that did `--extras llm-all` no longer needs it;
   confirm no Dockerfile stage references the extra or the workflow work dir.

## Exit criteria
- `grep -rn "langchain\|langgraph\|deepagents" services/jawafdehi --include=*.py`
  returns **nothing** (and the pyproject blocks are gone).
- `grep -rn "case_workflows" services/jawafdehi` returns nothing outside the new
  drop-tables migration's string literals.
- Service boots (`ready()` no longer autodiscovers a missing app), full test suite
  green, image builds without `llm-all`.
- `openai` retained and `enrich_missing_bigo` still works.

## Risks / call-outs
- **`deepagents` is an undeclared runtime dep today** — verify how it's actually
  installed before assuming the build is reproducible; either way it leaves with the app.
- **Capability loss is intentional**: automated CIAA case discovery + drafting
  (`discover_and_draft_cases`, `run_case_workflow`) goes away. Confirm no operational
  process depends on these batch jobs before deleting. If the *capability* must
  survive in some form, that's a separate build — this plan only retires the
  current LangChain implementation.
- Sequence migrations after the latest `cases` migration on `main` to avoid a merge node.
