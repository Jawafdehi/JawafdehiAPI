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
output, or nudge Arm A's results toward the ported code's results — **with one
authorized exception**: an explicit, documented input adapter that reconnects
the donor's input pipe (see "The adapter" below). That one exception is
classified BEHAVIOURAL and called out loudly rather than folded quietly into
the mechanical patch count.

## Revision note

An earlier version of this document (and of the Arm A scratch tree) patched
`entry["source"]` → `entry["material"]` / `source_type` → `material_type`
directly inside `common.py` and four of the five enrichers, while leaving the
`"CIAA_PRESS_RELEASE"`/`"COURT_ORDER"` literal *value* comparisons untouched.
That patch made the field lookups succeed, but the value comparisons still
never matched today's lowercase `material_type` vocabulary — so
`enrich_missing_bigo`, `enrich_allegations`, `enrich_timeline`, and
`enrich_related_entities` all completed but found **zero** usable evidence
text for every case (verified directly, including against a case with real
attached evidence). Independent verification confirmed this precisely: 0 of
139 evidence entries in the sample carry a `source` key at all; the vocabulary
changed together with the field name (the `DocumentSource` → `Material`
rename), not just the field name alone.

**Decision (made by the user, relayed by the task coordinator):** add a single,
documented input adapter rather than leave those four enrichers structurally
inert. The field-rename patches described in the superseded version of this
document have been **reverted** in four of the five files (see the patch
table below) — `enrich_allegations.py`, `enrich_timeline.py`, and
`enrich_related_entities.py` are now **byte-identical to donor `0321a85`**;
`enrich_missing_bigo.py` keeps only the (unrelated, still-necessary)
`sourcing.*` → `review.*` import-path fix. The adapter is applied at a single
choke point instead: `CaseworkApi.get_case()`.

## The adapter

**File:** `casework/arm_a_adapter.py` (new, Arm-A-scratch-tree only).
**Wired in at:** `casework/common.py`'s `CaseworkApi.get_case()` — the one
method every one of the 5 enrichers' case-detail fetches converges on
(directly, or via `get_target_cases`'s detail-fetch calls).

**What it does:** for each evidence entry, if `entry["material"]["material_type"]`
has a donor-recognised equivalent, synthesise `entry["source"] =
{"source_type": <mapped>, "title": <material.display_name>, "description": "",
"urls": entry["material"]["urls"]}` alongside the existing (untouched)
`material` key. (`title` was added in Revision 2 below — omitting it silently
confounded the first Task 16 `bigo` comparison.) This restores exactly the shape and
vocabulary the donor's `entry.get("source")` / `.get("source_type")` /
`.get("urls")` accessors were written to read — nothing downstream of
`get_case()` needed to change.

**Mapping table** (`_MATERIAL_TO_SOURCE_TYPE` in `arm_a_adapter.py`):

| `material_type` (today) | `source_type` (donor) | Rationale |
|---|---|---|
| `press_release` | `CIAA_PRESS_RELEASE` | Same document category; per `casework/common/pipeline.py`'s own comment, `press_release` is today's dominant (near-100%) form of what the donor called `CIAA_PRESS_RELEASE`. |
| `ciaa_press_release` | `CIAA_PRESS_RELEASE` | Same, the less-common spelling variant. |
| `court_order` | `COURT_ORDER` | Same document category, renamed field value only. |
| `charge_sheet` | **unmapped — deliberately** | See below. |

**Field mapping** (added in Revision 2, see below):

| donor `source` field | source in today's schema | notes |
|---|---|---|
| `source_type` | `material.material_type` (mapped, table above) | |
| `title` | `material.display_name` | Same mapping `review/jds_client.py:113` uses. Populated 36/36 in the Task 16 sample. |
| `description` | **no analog exists** | `Material` has only `display_name`/`material_type`/`urls`. Left empty; see Revision 2. |
| `urls` | `material.urls` (passed through unchanged) | |

**Why `charge_sheet` is NOT mapped to `CIAA_PRESS_RELEASE`:** checked the
donor's own code first, as instructed, rather than assuming. `enrich_timeline.py`'s
`MILESTONE_SOURCE_TYPES` (line 58) treats a charge sheet as a **separate,
higher-priority** type from `CIAA_PRESS_RELEASE`:
```python
MILESTONE_SOURCE_TYPES = (
    "AG_ABHIYOG_PATRA",  # charge sheet — richest factual detail
    "COURT_ORDER",       # granular incident chain + verdict outcome (fed before PR)
    "CIAA_PRESS_RELEASE", # complaint / investigation / chargesheet dates
    "COURT_FILING_OTHER",
)
```
with the donor's own comment: *"the charge sheet is richer still [than
COURT_ORDER] but is unavailable for ~all priority cases."* Folding today's
`charge_sheet` into `CIAA_PRESS_RELEASE` would misrepresent the document type
the donor's own logic distinguishes on — exactly the kind of "change what the
donor sees" the adapter must not do. `charge_sheet` is therefore left
unmapped in this task.

**Open question, flagged rather than decided unilaterally:** today's
`charge_sheet` `material_type` is plausibly the same underlying concept as the
donor's `AG_ABHIYOG_PATRA` (both mean "the CIAA's formal charge-filing
document"), and `enrich_timeline.py` and `enrich_description.py` (the latter
out of scope/unported) both have donor-side logic that specifically wants
that type. Mapping `charge_sheet` → `AG_ABHIYOG_PATRA` was outside the
explicit scope given for this task (which named only the press-release/
court-order mapping) and touches `enrich_timeline`'s *highest*-priority
source type, so it was **not added**. If Task 16 wants `enrich_timeline` to
see charge-sheet content too, that mapping needs an explicit decision, not an
assumption — `COURT_FILING_OTHER` (also in `MILESTONE_SOURCE_TYPES`, also
unmapped) is the same kind of open question.

### Revision 2 (Task 16): `source.title` IS populated

An earlier version of this document stated that the adapter "deliberately
does not populate `source.title` / `source.description`". **For `title` that
was wrong, and it confounded the first Task 16 `bigo` run.**

The donor's `_build_source_context_from_entry`
(`enrich_missing_bigo.py:409`) reads `source.title` and feeds it to the bigo
prompt, so the real June donor **did** receive the document title. Leaving it
empty did not keep Arm A neutral — it handicapped Arm A on information the
donor actually had, while the port sends today's analog
(`material.display_name`) into its own prompt. Populating it therefore
*completes* the adapter's stated purpose (reconnecting the donor's input
pipe) rather than improving Arm A; leaving it empty was the deviation.

`display_name` is the established analog of `DocumentSource.title` — the
same mapping `review/jds_client.py:113` already encodes
(`"title": mat.get("display_name") or ""`). It is populated on 36/36 mapped
evidence entries in the Task 16 sample, and per CIAA drafting convention it
frequently states the बिगो amount itself.

**`source.description` remains empty, and that is now an evidenced decision
rather than an assumption.** Today's `Material` object carries exactly three
keys — `display_name`, `material_type`, `urls` — so the schema has no
document-description field at all. The only candidate,
`evidence.additional_details`, is (a) evidence-level annotation about why a
document is attached rather than a description *of* the document, which is
why `review/jds_client.py` gives it a separate `evidence_description` slot,
and (b) empty on 36/36 mapped entries in the sample. Synthesising a value
would be manufacturing donor input.

**What the adapter still deliberately does NOT do:**
- does not touch any prompt, truncation limit, threshold, or model tier
  inside any enricher.
- does not change how `urls` are selected/prioritized among roles — it
  passes `material["urls"]` through unchanged, so the donor's own
  MARKDOWN-role-link selection in `content_from_evidence_entry` picks the
  identical link the port's `casework/common/materials.py::markdown_link`
  picks for the same entry.

## Classification

**1 BEHAVIOURAL patch** (the adapter, at `CaseworkApi.get_case()`) — it
changes Arm A's output from **nothing** (for 4 of 5 enrichers) to **real
extraction**, which is unambiguously a change in what Arm A can produce. This
is intentional and authorized, not something to obscure by counting it as
mechanical. Every Task 16 comparison on `enrich_missing_bigo`,
`enrich_allegations`, `enrich_timeline`, and `enrich_related_entities` now
rests on this one patch being correct — which is exactly why the
identical-source-text verification below exists and is load-bearing.

**9 MECHANICAL patches** (unchanged in kind from the prior revision of this
document, minus the reverted field-renames):

| # | File | Function | What changed | Why unavoidable |
|---|------|----------|--------------|------------------|
| 1 | `casework/common.py` | `bootstrap()` | `DJANGO_SETTINGS_MODULE` default `"config.settings_scripts"` → `"config.settings"` | The "R1 collapse" removed the separate DB-optional scripts settings module; `config.settings` is the only settings module left. Without this, `django.setup()` raises `ModuleNotFoundError` immediately. |
| 2 | `casework/common.py` | `CaseworkApi.__init__` | Donor sent `Authorization: Token <token>` and hard-raised if no token was given. Patched to also accept HTTP Basic (`JAWAFDEHI_API_BASIC_USER`/`_PASS`) when no token is supplied. | The backend has no DRF `TokenAuthentication` at all anymore (OIDC/Zitadel only, or the local-only `DEV_AUTH` Basic/Session fallback). A bearer/token header has nothing to authenticate against locally. |
| 3 | `casework/common.py` | `is_ciaa_special_court_case` | Also matches `.../courtcase/special/<number>` (case-insensitive), alongside the donor's original colon-prefix check (kept, not removed). | Canonical `court_cases` IRI shape changed from a colon-prefixed token to a full URL path segment — the same predicate re-expressed. **Caveat:** unlike the other mechanical patches, this one changes *which cases* are selected (unpatched: zero, for all 5 enrichers, verified). Task 16 should independently verify both arms select the same case set. |
| 4 | `casework/common.py` | `_court_number` | Also extracts the trailing path segment from a `/courtcase/<court>/<number>` IRI, alongside the donor's original colon-suffix handling (kept). | Same IRI-shape change as #3; used by `--court-case` resolution. Same caveat. |
| 5 | `casework/common.py` | `content_from_evidence_entry` | `from sourcing import jds_client` → `from review import jds_client`; `from sourcing import converter` → `from review import converter`. **The `entry.get("source")` / `source_type` accessors are UNCHANGED donor code** — the adapter supplies `entry["source"]` upstream. | The `sourcing` package no longer exists; `download_source_file(url) -> (bytes, content_type)` and `convert_source({"url": [...]}) -> {status, markdown, ...}` now live at `review.jds_client` / `review.converter` with **identical signatures** (verified by reading both modules) — a pure import-path move. |
| 6 | `casework/common.py` | `source_content` | No code change beyond the docstring note that its (unused by the 5 target enrichers) `entry.get("source")`/`source_type` accessors are also unchanged donor code, fed by the same adapter. | Documentation only; kept for consistency since this document previously described a patch here. |
| 7 | `casework/enrich_missing_bigo.py` | `_get_source_content`, `_build_source_context_from_entry` | Same `sourcing.*` → `review.*` import-path fix as #5, applied locally (this file duplicates the evidence-reading logic rather than calling `common.py`'s). **`entry.get("source")`/`source_type` accessors are UNCHANGED donor code.** | Same as #5 — this file has its own copy of the import. |
| 8 | `casework/enrich_allegations.py` | — | **No changes.** Byte-identical to donor `0321a85`. | The adapter at `CaseworkApi.get_case()` makes this file's original `entry.get("source")` code work unmodified. |
| 9 | `casework/enrich_timeline.py`, `casework/enrich_related_entities.py` | — | **No changes.** Byte-identical to donor `0321a85`. | Same as #8. |

(`enrich_tags.py` needed **no material-reading patch at all**, before or
after the adapter — it classifies from case title/description/
key_allegations/court_cases/bigo metadata only, never evidence content;
confirmed by reading `_collect_case_text`/`classify_case_rules`.)

## Deliberately NOT patched (preserved donor behavior / dead code)

- **`enrich_timeline._get_ngm_data`** (colon-prefix `"special:"` selector for
  the NGM lookup) — confirmed dead: 0 of the case's `court_cases` entries are
  colon-prefixed under today's full-IRI encoding, so `special_ref` is always
  `None` and the (now-removed, 2026-07-01) `/ngm/court_case/` endpoint is
  never called. Re-verified after the adapter: running `enrich_timeline`
  against a real-evidence case printed `NGM data: none` with no NGM-related
  HTTP call in the verbose log. **Not patched**, per instruction.
- **`enrich_tags._detect_court_context`** (identical colon-prefix bug) —
  confirmed dead the same way: rule-tagging three real special-court cases
  never produced a "Special Court" tag. **Not patched.**
- **`enrich_related_entities.create_entity(display_name, nes_id="")`** +
  `{"entity": <int id>}` patch payload — the current
  `EntityPatchItemSerializer` requires a canonical NES `@id` IRI. **Not
  patched** (the port is extraction-only for exactly this reason). **Still
  not observed this task** even with the adapter: `create_entity` sits behind
  an `if dry_run: return` guard, and this task is `--dry-run` only. With the
  adapter, the enricher now genuinely reaches real extraction (see
  runnability table) and prints what it *would* PATCH — 3 entities, in the
  one real-evidence case tested — but the write itself, and its expected 400,
  is Task 16's to observe under `--apply`.
- **The `charge_sheet`/`AG_ABHIYOG_PATRA`/`COURT_FILING_OTHER` mapping
  question** — see "The adapter" section above. Left unmapped, flagged for
  an explicit decision.
- **`casework/common.py`'s `matches_fiscal_year` and `special_court_number`**
  retain their original colon-based logic, unpatched — not named in the
  authorized IRI-selection category, not exercised by any of the 5 target
  enrichers' default (no `--fiscal-year`) runs, and `special_court_number` is
  only used by the out-of-scope, unported `enrich_description.py`/`enrich_title.py`.
- **The donor's `len(text) > 200` adequacy gate** in
  `content_from_evidence_entry` (accepting a MARKDOWN-derived text only if it
  exceeds 200 chars, else falling through to RAW conversion, then `None`) —
  untouched, pre-existing donor logic. Noted here because it is a real,
  narrow source of potential divergence from the port's own
  `materials.py::source_text` (which has no such gate — see next section) on
  the rare evidence entry with very short markdown text; not a defect in the
  adapter.

## Identical-source-text verification

**Property being verified:** for a given evidence entry, does Arm A (via the
adapter) resolve the exact same underlying document text that the port's
`casework/common/materials.py` resolves for that same entry? This is checked
**per evidence entry**, not as an aggregate per case — the donor's own
per-enricher functions (`enrich_allegations._get_press_release_content`,
`enrich_related_entities`'s two loops, `enrich_missing_bigo._get_source_content`)
only look at the *first* matching entry and stop, while the port's
`materials.py::source_text` concatenates *all* matching entries — a
pre-existing, out-of-scope difference in aggregation logic between donor and
port that predates this task. Comparing per-entry isolates exactly what the
adapter is responsible for (finding + fetching the right URL) from that
unrelated scope difference.

**Method:** loaded `casework/common/materials.py` (port) and `casework/common.py`
+ `casework/arm_a_adapter.py` (Arm A) by explicit file path in one Python
process (bypassing the `casework` package-name collision between the
worktree and the scratch tree), fetched real case JSON from the local API,
and for every evidence entry whose `material_type` is `press_release` or
`court_order`, compared:
- port: `materials.markdown_link(material)` then `materials.fetch_markdown(link)`
- Arm A: `arm_a_adapter.adapt_case()` on a copy of the entry, then the
  donor's unmodified `content_from_evidence_entry(entry)`

**Sample:** 6 cases (the known real-evidence case
`chandra-singh-lama-embezzlement-080-cr-0067`, plus 5 more discovered by
scanning local DRAFT cases for `press_release`/`court_order` material),
covering **12 evidence entries** (6 press-release + 6 court-order).

**Result: 12/12 exact text matches (byte-for-byte string equality), 0
mismatches.**

> **Task 16 addendum — identical source text is NOT identical prompt.**
> This verification establishes that both arms read the same document TEXT.
> It says nothing about the rest of the assembled prompt, and Task 16 found
> the two arms were in fact sending *different* prompts: the port adds a
> metadata context block built from `material.display_name`, while the
> donor's equivalent block was empty because this adapter had left
> `source.title` unpopulated (see Revision 2 above). Any future comparison
> must verify the identical-PROMPT property, not just identical source text.

```
chandra-singh-lama-embezzlement-080-cr-0067 [0] press_release  port=1255  arm_a=1255  MATCH
chandra-singh-lama-embezzlement-080-cr-0067 [1] court_order    port=60174 arm_a=60174 MATCH
case-080-cr-0216-080-cr-0216-bd100e         [0] press_release  port=1536  arm_a=1536  MATCH
case-080-cr-0216-080-cr-0216-bd100e         [1] court_order    port=25828 arm_a=25828 MATCH
case-080-cr-0215-080-cr-0215-a99de4         [0] press_release  port=868   arm_a=868   MATCH
case-080-cr-0215-080-cr-0215-a99de4         [1] court_order    port=16283 arm_a=16283 MATCH
case-080-cr-0213-080-cr-0213-259202         [0] press_release  port=895   arm_a=895   MATCH
case-080-cr-0213-080-cr-0213-259202         [1] court_order    port=19695 arm_a=19695 MATCH
case-080-cr-0212-080-cr-0212-0a340a         [0] press_release  port=868   arm_a=868   MATCH
case-080-cr-0212-080-cr-0212-0a340a         [1] court_order    port=20385 arm_a=20385 MATCH
case-080-cr-0211-080-cr-0211-e61efd         [0] press_release  port=910   arm_a=910   MATCH
case-080-cr-0211-080-cr-0211-e61efd         [1] court_order    port=14674 arm_a=14674 MATCH
```

No mismatches means the >200-char adequacy gate (noted above) never actually
diverged the two arms in this sample — every markdown text in the sample was
well over 200 chars.

**Data-density context:** of the 200 local DRAFT cases sampled, 78 (39%) carry
any evidence, and 77 carry `press_release`/`court_order` material
specifically — so real content is common, not a rare edge case, in the local
seed.

## Per-enricher runnability (`--dry-run`, local sqlite, `http://127.0.0.1:48010`)

Re-verified after the adapter. PID ownership of port 48010 reconfirmed
(`gaurav`, same PID as before) prior to these runs.

| Enricher | Runs to completion? | Finds real source text? | Produces real output? |
|---|---|---|---|
| `enrich_missing_bigo` | Yes | **Yes** — a batch `--limit 8` run found usable content in 1/8 selected cases (the other 7 have no evidence at all in this local seed — a data-sparsity fact, not a mismatch: confirmed via direct API check); a targeted `--slug chandra-singh-lama-embezzlement-080-cr-0067 --force` run against a case with real evidence found 1,255 chars. | **Yes** (with `--provider claude_cli --model haiku`): extracted BIGO = 913,280 — matching the case's actual, already-known `bigo` value exactly. |
| `enrich_tags` | Yes | N/A (never reads evidence content) | **Yes** — genuine rule-based tags per case (unaffected by the adapter, unchanged from the prior verification). "Special Court"/"Supreme Court" context tags confirmed always absent (known dead code, preserved). |
| `enrich_allegations` | Yes | **Yes** — same real-evidence case: found 1,255 chars of press-release text. | **Yes**: extracted 3 real allegations from the press release. |
| `enrich_timeline` | Yes | **Yes** — same case: found COURT_ORDER (60,174 chars, LLM-summarised to 6,197) + CIAA_PRESS_RELEASE (1,255 chars), assembled 7,509 chars. NGM path confirmed still inert (`NGM data: none`, no HTTP call). | **Yes**: extracted 10 real, dated timeline entries (2014-12-29 through 2024-06-06), including the case's actual acquittal verdict date. |
| `enrich_related_entities` | Yes | **Yes** — a batch `--limit 3` run found press-release + court-order content in **3/3** selected cases (14,987–15,039-char prompts assembled); a targeted run on the known case found 1,255 + 60,174 chars. | **Yes**: identified 1 location entity + 2 related entities with real descriptions, reaching the `--dry-run` "would PATCH 3 entities" gate. The entity-create 400 remains unobserved (gated behind `--dry-run`, as before) — Task 16's to confirm under `--apply`. |

**All five enrichers now run to completion AND produce genuine, non-trivial,
comparable output when given a case with real evidence** (39% of local DRAFT
cases, per the data-density note above). This is a materially different,
better-founded position than the pre-adapter revision of this document.

## What Task 16 can and cannot validly compare

- **Can compare (selection):** for all 5 enrichers, WHICH cases each arm
  selects. The IRI-selector patches (#3/#4) are a pure encoding adaptation of
  the identical predicate. Verify independently rather than assume.
- **Can compare (extraction quality) — now valid for all 5 enrichers**,
  contingent entirely on the adapter and the identical-source-text
  verification above:
  - `enrich_tags`: rule-based tag classification (unaffected by the adapter).
  - `enrich_missing_bigo`, `enrich_allegations`, `enrich_timeline`,
    `enrich_related_entities`: real extraction from real, byte-identical
    source text (verified on 12 evidence entries across 6 cases). This
    reverses the prior revision's finding that these four were structurally
    inert — with the adapter, they are genuinely comparable.
  - **Caveat that still narrows the comparison**: `enrich_timeline` will not
    see any `charge_sheet`-typed evidence (the highest-priority
    `MILESTONE_SOURCE_TYPES` entry, `AG_ABHIYOG_PATRA`) because that mapping
    was deliberately left undecided (see "The adapter" above). If any sampled
    case's richest source is a charge sheet rather than a court order or
    press release, Arm A's timeline for that case will be working from a
    strictly poorer source set than the donor would have had in June, and
    Task 16 should treat a timeline gap on such a case as inconclusive rather
    than a genuine port-vs-donor difference.
  - **Second caveat**: the donor's per-enricher functions read only the
    *first* matching evidence entry per type (`enrich_allegations`,
    `enrich_related_entities`, `enrich_missing_bigo`'s local duplicate all
    `break`/return early), while the port's own enrichers aggregate *all*
    matching entries via `materials.py::source_text`. This is a pre-existing,
    out-of-scope difference in enrichment algorithm (not caused by the
    adapter or by this task) that Task 16 should be aware of when a case has
    more than one evidence entry of the same type: a diff there reflects a
    real, intentional algorithmic difference between donor and port, not an
    Arm A defect.
- **Cannot observe (this task):** `enrich_related_entities`'s entity-creation
  400. Now genuinely reachable in principle (the enricher reaches real
  extraction) but still gated behind the `--dry-run` check in the donor's own
  code — Task 16 must run `--apply` to observe it.
- **Everything Task 16 concludes about `enrich_missing_bigo`,
  `enrich_allegations`, `enrich_timeline`, and `enrich_related_entities` rests
  on the adapter and the identical-source-text property above being correct.**
  If Task 16 finds a case where Arm A's fed text does NOT match the port's
  resolved text for the same evidence, that is a blocker for this task's
  conclusions and should be reported, not silently worked around.
