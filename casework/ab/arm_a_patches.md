# Arm A patches: deviations from donor commit `0321a85`

Arm A is the frozen donor enrichment code (`casework/enrich_{missing_bigo,tags,
allegations,timeline,related_entities}.py` + `casework/common.py` and the rest of
the `casework/` package) as it existed at donor commit `0321a85`
(`services/JawafdehiAPI`). It lives **only** as a gitignored scratch tree at
`work/2026-07-17-enricher-extraction/arm_a/` — it is never committed to this repo.
This file is the only artifact of Task 15 that *is* committed: the complete,
honest record of every edit needed to make that frozen donor code executable
against today's API and schema.

**Rule followed throughout:** patch only what prevents the donor code from
running against today's schema/endpoints. Never patch to fix a bug, improve
output, or nudge Arm A's results toward the ported code's results. Where a
donor check no longer matches today's data and fixing it would change *what
gets extracted* (as opposed to merely *where in the payload* the check looks),
the check was left broken and the resulting behavior is documented below and in
the runnability table, per the task's Rule 3.

## Patch table

All 10 patches below are classified **MECHANICAL**: each is a straight
field/module/transport rename forced by schema or backend evolution since
June 2026, with the donor's actual selection/extraction *semantics* left
untouched. Two of them carry a caveat (marked below) because, unlike a pure
import-path fix, they determine *which cases get selected at all* — flagged
loudly rather than silently filed under "mechanical, nothing to see here."

| # | File | Function | What changed | Why unavoidable | Class |
|---|------|----------|--------------|------------------|-------|
| 1 | `casework/common.py` | `bootstrap()` | `DJANGO_SETTINGS_MODULE` default `"config.settings_scripts"` → `"config.settings"` | The "R1 collapse" removed the separate DB-optional scripts settings module; `config.settings` is now the only settings module in the repo. Without this, `django.setup()` raises `ModuleNotFoundError` immediately — nothing runs. | MECHANICAL |
| 2 | `casework/common.py` | `CaseworkApi.__init__` | Donor sent `Authorization: Token <token>` and hard-`raise`d if no token was given. Patched to also accept HTTP Basic (`JAWAFDEHI_API_BASIC_USER`/`JAWAFDEHI_API_BASIC_PASS`) when no token is supplied, falling back to Basic instead of raising. | The backend has no DRF `TokenAuthentication` at all anymore (OIDC/Zitadel only, or the local-only `DEV_AUTH` Basic/Session fallback documented in `casework/ab/README.md`). A bearer/token header has nothing to authenticate against locally; Basic against the local `abgen` superuser is the only working local credential. | MECHANICAL |
| 3 | `casework/common.py` | `is_ciaa_special_court_case` | Also matches `.../courtcase/special/<number>` (case-insensitive), in addition to the donor's original colon-prefix `"special:..."` check (kept, not removed). | The canonical `court_cases` IRI shape changed from a colon-prefixed token to a full URL path segment. This is the *same predicate* ("does this case have a CIAA Special Court reference") re-expressed against the new encoding — no selection criterion was added, removed, or altered. **Caveat:** unlike #1/#2/#5–#10, this patch does change *which cases* `get_target_cases` yields (unpatched: zero, for every one of the 5 enrichers — verified empirically below). It changes selection, not per-case extraction; Task 16 should independently verify both arms select the same case set rather than taking this note on faith. | MECHANICAL (selection-affecting — see caveat) |
| 4 | `casework/common.py` | `_court_number` | Also extracts the trailing path segment from a `/courtcase/<court>/<number>` IRI, in addition to the donor's original colon-suffix handling (kept). | Same IRI-shape change as #3; used by `get_target_cases`'s `--court-case` resolution path. Same caveat as #3 (only matters when `--court-case` is passed; not exercised in this task's verification, which uses no explicit slug/court-case selector). | MECHANICAL (selection-affecting — see caveat) |
| 5 | `casework/common.py` | `content_from_evidence_entry` | `entry["source"]` → `entry["material"]`; `from sourcing import jds_client` → `from review import jds_client`; `from sourcing import converter` → `from review import converter`. | The evidence-entry field is now named `material`, not `source` (same shape: `display_name`/`material_type`/`urls`). The `sourcing` package no longer exists; `download_source_file(url) -> (bytes, content_type)` and `convert_source({"url": [...]}) -> {status, markdown, ...}` now live at `review.jds_client` / `review.converter` with **identical signatures** (verified by reading both modules) — a pure import-path move, not a logic change. | MECHANICAL |
| 6 | `casework/common.py` | `source_content` | `entry["source"]["source_type"]` → `entry["material"]["material_type"]` (field name only — the `source_types` value set passed in by callers is untouched). | Same field rename as #5. This function is not currently called by any of the 5 ported enrichers (each does its own type filtering — see #7–#10) but is patched for consistency since the brief names it explicitly. | MECHANICAL |
| 7 | `casework/enrich_missing_bigo.py` | `_get_source_content`, `_build_source_context_from_entry` | Same `entry["source"]`→`entry["material"]`, `source_type`→`material_type`, `sourcing.*`→`review.*` rename as #5, applied to this file's own local duplicate of the evidence-reading logic (it does not call the shared `common.py` helpers). | Same schema/module changes as #5; this file has its own copy of the logic. | MECHANICAL |
| 8 | `casework/enrich_allegations.py` | `_get_press_release_content` | Same rename as #5 (field names only). | Same as #5. | MECHANICAL |
| 9 | `casework/enrich_timeline.py` | `_get_source_parts` | Same rename as #5 (field names only). | Same as #5. | MECHANICAL |
| 10 | `casework/enrich_related_entities.py` | press-release / court-order lookup loops | Same rename as #5 (field names only, two call sites). | Same as #5. | MECHANICAL |

Total: **10 patches, 0 classified BEHAVIOURAL** (2 carry an explicit
selection-affecting caveat — see #3/#4 above; this is not the same as changing
per-case extraction results).

## Deliberately NOT patched (preserved donor behavior / dead code)

- **`enrich_timeline._get_ngm_data`** (colon-prefix `"special:"` selector for
  the NGM lookup) — confirmed dead: 0 of the case's `court_cases` entries are
  colon-prefixed under today's full-IRI encoding, so `special_ref` is always
  `None` and the (now-removed, 2026-07-01) `/ngm/court_case/` endpoint is never
  called. Verified empirically: running `enrich_timeline` against a case with
  real evidence produced no NGM-related log line or HTTP call. **Not patched**,
  per the task brief's explicit instruction to preserve this as dead code.
- **`enrich_tags._detect_court_context`** (identical colon-prefix bug) —
  confirmed dead the same way: "Special Court" / "Supreme Court" tags never
  fire. Verified empirically: three real special-court cases were rule-tagged
  (`Education`, `Gandaki`, `CIAA`, `Corruption`, etc.) and never once produced
  a "Special Court" tag. **Not patched.**
- **`enrich_related_entities.create_entity(display_name, nes_id="")`** +
  `{"entity": <int id>}` patch payload — the current
  `EntityPatchItemSerializer` requires a canonical NES `@id` IRI, not a bare
  integer id with an empty `nes_id`. **Not patched** (the port is
  extraction-only for exactly this reason). **Not observed this task**: the
  `create_entity`/patch call sits behind an `if dry_run: return` guard that
  fires *before* it, so a `--dry-run` run (all this task performs) never
  reaches it — the expected 400 can only be observed by Task 16's `--apply`
  run.
- **A third, previously unflagged dead path, discovered during this task's
  verification**: the donor's `stype == "CIAA_PRESS_RELEASE"` /
  `== "COURT_ORDER"` literal string comparisons — present, unchanged, in
  `enrich_missing_bigo._get_source_content`, `enrich_allegations._get_press_release_content`,
  `enrich_timeline._get_source_parts` (via `MILESTONE_SOURCE_TYPES`), and both
  loops in `enrich_related_entities` — **never match today's `material_type`
  taxonomy**, which is lowercase snake_case (`press_release`, `court_order`,
  `news`; confirmed via the local API and via
  `casework/common/pipeline.py`'s own `PRESS_TYPES = ("press_release",
  "ciaa_press_release", "charge_sheet")` comment, which records this exact
  taxonomy shift as an empirically-verified finding from the port work).
  Verified directly: `enrich_missing_bigo --slug
  chandra-singh-lama-embezzlement-080-cr-0067 --force --dry-run` against a
  case whose evidence genuinely includes a `press_release` material still
  reports "No press-release source content found" — 0 matches, every time.
  **Not patched.** Fixing the field *name* (`source`→`material`,
  `source_type`→`material_type`, patches #5–#10 above) was required just to
  read the right sub-object at all; changing the *value* being compared
  against (`"CIAA_PRESS_RELEASE"` → `"press_release"`) would be "porting a
  June fix forward" / aligning Arm A's results with the current taxonomy,
  which Rule 1 forbids. Left broken, per Rule 3: a missing datapoint here is
  honest, a reconstructed one would be misleading.
- **`casework/common.py`'s `matches_fiscal_year` and `special_court_number`**
  retain their original colon-based logic, unpatched. Not named in the brief's
  authorized IRI-selection category (which names only
  `is_ciaa_special_court_case` / `_court_number`), not exercised by any of the
  5 target enrichers' default (no `--fiscal-year`) runs in this task, and
  `special_court_number` is only used by the out-of-scope, unported
  `enrich_description.py` / `enrich_title.py`.
- **`enrich_missing_bigo._build_source_context_from_entry`**'s
  `source.get('title')` / `source.get('description')` — today's `material`
  dict carries `display_name`, not `title`/`description`, so these always
  render blank in the LLM prompt context. Not patched: cosmetic (doesn't block
  execution), and moot in practice since this code path is only reached after
  `_get_source_content` finds usable content, which (per the point above)
  never happens under today's data.

## Per-enricher runnability (`--dry-run`, local sqlite, `http://127.0.0.1:48010`)

Verified by running each script directly:
`DEBUG=True JAWAFDEHI_API_BASIC_USER=abgen JAWAFDEHI_API_BASIC_PASS=<local-dev-only>
PYTHONPATH=<arm_a>:<worktree> uv run --project <worktree> python casework/<script>.py
--api-base-url http://127.0.0.1:48010 --dry-run --verbose [--limit 3]`.

| Enricher | Runs to completion? | Selects cases? | Produces real output? |
|---|---|---|---|
| `enrich_missing_bigo` | Yes | Yes (3/3 requested, non-zero — confirms the IRI patch) | No — 0/3 cases had usable press-release content (`CIAA_PRESS_RELEASE` value mismatch, see above); completes with `cases_no_content=3`, LLM extraction never invoked. |
| `enrich_tags` | Yes | Yes (non-zero) | **Yes** — genuine rule-based tags per case (e.g. `Education`, `Gandaki`/`Madhesh`, `Local Government`, `Procurement`, `Forged Documents`, `Witness Tampering`, `CIAA`, `Corruption`), the only one of the 5 whose extraction logic is actually exercised against real local data. "Special Court"/"Supreme Court" context tags confirmed always absent (known dead code). Optional `metadata_llm` assist (tested with `--provider claude_cli --model haiku`) hit a `claude_cli` subprocess error (`rc=1`) on one case; caught per-case (`cases_llm_error`), run still completed normally — not a blocker, unrelated to any Arm A patch. |
| `enrich_allegations` | Yes | Yes (non-zero) | No — 0 usable press-release content (same value mismatch); completes with `cases_no_content=3`. |
| `enrich_timeline` | Yes | Yes (non-zero) | No — 0 usable source content (same value mismatch); completes with `cases_no_content`. NGM dead-path confirmed inert: no HTTP call attempted, no crash, as predicted. |
| `enrich_related_entities` | Yes | Yes (non-zero) | No — 0 usable press-release/court-order content (same value mismatch); completes with `cases_no_content`. The entity-create 400 (expected per the current `EntityPatchItemSerializer`) is unreachable under `--dry-run` (gated behind the dry-run check before the POST) — not observed this task.

**All five enrichers run to completion under `--dry-run` with no crash and no
unhandled exception.** None required a behavioural patch to reach that point.

## What Task 16 can and cannot validly compare

- **Can compare (selection):** for all 5 enrichers, WHICH cases each arm
  selects — the IRI-selector patch (#3/#4) is a pure encoding adaptation of
  the identical predicate, so a same-input, same-output selection-set
  comparison between Arm A and the port is valid. Task 16 should still verify
  this directly (compare selected slug sets) rather than assume it from this
  document, given the selection-affecting caveat on those two patches.
- **Can compare (extraction, with real output):** `enrich_tags`' rule-based
  tag classification — the only Arm A stage that produces genuine,
  non-trivial output against today's local data. This is a fair, load-bearing
  A/B point.
- **Cannot validly compare (extraction quality):** `enrich_missing_bigo`,
  `enrich_allegations`, `enrich_timeline`, and `enrich_related_entities`.
  Arm A structurally finds **zero** source text for all four under today's
  `material_type` taxonomy (the donor's `CIAA_PRESS_RELEASE`/`COURT_ORDER`
  literal checks, deliberately left unpatched, never match today's lowercase
  values). Any Task 16 "diff" on these four's *extracted values* is Port vs.
  Nothing, not Port vs. Donor-logic — a discovery this task made beyond the
  brief's two named blockers (NGM dead path, tags court-context dead path).
  If Task 16 wants a genuine extraction-logic A/B for these four, it would
  need Arm A's donor to actually see press-release/court-order text, which
  this task's rules forbid reconstructing.
- **Cannot observe (this task):** `enrich_related_entities`'s entity-creation
  400. Expected once Task 16 runs `--apply` and it reaches a case with real
  content (which, per the point above, would first require the
  `material_type`-value gap above to somehow be crossed) — record it when it
  actually happens rather than assuming it from this document.
