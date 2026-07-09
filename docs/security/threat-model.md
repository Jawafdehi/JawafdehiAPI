# Jawafdehi — Threat Model & Security Posture

_Living document. Companion to the adversarial test suite (`pytest -m security`) and the
live-stack E2E suite (`integration-tests/`). Last updated 2026-07-09._

Jawafdehi is a **public accountability archive** documenting corruption cases. What makes its
security posture unusual is the sensitivity of its *pre-publication* and *anonymous* data: an
allegation that has not cleared the verification queue, or the identity of an anonymous submitter,
must never leak. This document names the threat actors, the assets they'd target, the controls in
place, and the residual/accepted risks.

## Assets (what an attacker wants)

| Asset | Why it's sensitive |
|---|---|
| **Non-public cases** (DRAFT / IN_REVIEW / CLOSED) | Un-verified allegations; publishing early is defamation risk + tips off the accused. |
| **Anonymous submissions** | The platform promises anonymous reporters that NO identifying data is stored. A leak endangers a source. |
| **Personal identifiers** (phone, national ID, family info) | Privacy boundary — collected for verification, never published. |
| **Evidence / materials** bound to non-public cases | Same exposure as the case itself. |
| **Editorial controls** (publish, global review thresholds, entity `@type`) | Whoever controls these controls what the public sees. |
| **The gated SQL surface** (`/api/query/`) | Direct read access to the NGM data lake. |

## Threat actors

1. **Anonymous visitor / scraper** — unauthenticated; tries to enumerate or guess non-public
   records, or DoS the public API.
2. **The accused party** — motivated to find/alter/suppress what's filed against them; may create a
   low-privilege account.
3. **Low-privilege insider** (caseworker / readonly) — tries to escalate to editorial/admin power.
4. **Injection / abuse attacker** — SQL/query injection, upload abuse, stored XSS, cache poisoning.

## Controls in place (and their tests)

### Access control
- **Case-only-published rule**: only `state=PUBLISHED` cases enter the public API, search index,
  sitemap, and ResourceSync. Non-public detail → 404 for anon. _Tests: `cases/`,
  `search/tests/test_indexer_index_calls.py::test_case_index_non_published_deletes`,
  `discovery/tests/test_corpus.py`._
- **Soft-delete tombstones** (`is_deleted`) are honored on EVERY read/discovery surface — a deleted
  entity/court case no longer appears as a live sitemap `<loc>`. _Fixed F1; tests in
  `discovery/tests/test_corpus.py`._
- **Material visibility** derives from the MAX referring-case state (LISTED/UNLISTED/PRIVATE) and is
  recomputed on the `Case` model signal, so a demotion via ANY write path (admin/shell/command)
  can't leave a draft/closed case's evidence public. A PRIVATE court-material 404s for anon rather
  than falling back to derived JSON-LD. _Fixed F2/F3; tests in `materials/tests/test_visibility.py`._
- **Role gating**: admin / moderator / caseworker / readonly / public. Write endpoints are
  role-gated with a consistent 401-(unauth) vs 403-(authed-without-role) contract across the
  courts/entities/materials write planes. The review-config PUT (global thresholds) is
  Admin/Moderator-only even though any read-role may GET it. _Tests:
  `tests/api/test_review_config_gate.py`, `tests/test_role_based_permissions.py`,
  `courts/tests/test_write_api.py`, and the `-m security` role-matrix suite._
- **DB-router isolation**: cross-alias relations are refused (`allow_relation`); there are no
  cross-service FKs. _Tests: `tests/test_router_isolation.py`._

### Auth
- OIDC/Zitadel only in production; legacy DRF `Token` headers are IGNORED, not honored.
- `dev-login` (username/password → Django session) is mounted ONLY when `DEV_AUTH` is on
  (`DEBUG or TESTING`); it HARD-404s in production. _Tests: `tests/api/test_dev_login.py`._

### Input / injection
- Gated SQL (`/api/query/`) is SELECT-only, allowlist-guarded, timeout-bounded, and runs on the
  `ngm` connection only.
- Entity/material IRIs are length-bounded (`MAX_IRI_LENGTH=300`) and host-canonicalized on write, so
  a foreign-host or over-length join key can't be stored. _Tests:
  `entities/tests/test_schemaorg.py`, `jawafdehi_shared/entities/tests/test_ids.py`._
- Search query params (`q`/`sort`/`type`/`case_type`/cursor) validate to 400, never 500/execute.

## Residual / accepted risks (tracked, not yet closed)

- **F5 — court-order material IRI hyphen/underscore fork** (MEDIUM, deferred). Two IRI minters
  disagree on hyphen handling for a COURT_ORDER whose `document_id` lacks the `court-order` marker,
  which can create a duplicate Material row. Fixing it re-keys already-synced rows, so it needs a
  coordinated re-sync/migration. Pinned by a characterization test
  (`materials/tests/test_sync_materials.py`) so the fork stays visible.
- **F14 — throttle counter is per-worker** (LOW-MEDIUM). DRF throttling uses the default
  `LocMemCache`, which is per-gunicorn-worker, so the anon "1000/hour" cap is effectively
  `rate × worker_count` and resets on worker recycle. A `CACHE_URL` env seam now allows pointing the
  cache at a shared backend (Redis) to make the cap global; until one is provisioned the limit is a
  soft abuse-dampener, not a hard quota. Throttling is also fully disabled under `TESTING`, so CI
  does not exercise it — the live-stack E2E suite is the place to smoke it.
- **Write-gate implementation drift** (informational). The public-read/role-gated-write contract is
  implemented three ways (courts mixin `get_permissions`, entities `get_permissions`, materials
  function-view manual 401/403). They are behavior-equivalent and each independently tested; a
  structural unification was assessed as net-negative churn (it risks the exact contract it would
  preserve) and deferred.

## The adversarial suite

`pytest -m security` runs the penetration-oriented tests: IDOR / non-public enumeration across ALL
read surfaces, the full role×endpoint escalation matrix, injection against the gated SQL and search,
IRI traversal/SSRF re-keying, and anonymous-submission privacy. The live-stack half (browser + real
backend) lives in the Playwright E2E suite and `integration-tests/` (draft-leak-in-search,
DEBUG-off/no-traceback, Host-header sitemap poisoning).
