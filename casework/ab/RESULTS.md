# Three-way A/B: donor (Arm A) vs port (Arm B) vs June's shipped values (Arm G)

**Run date:** 2026-07-19 · **Sample:** 20 cases · **Model:** `claude_cli` +
`claude-haiku-4-5-20251001`, both tiers, both arms · **Target:** local sqlite
at `http://127.0.0.1:48010` (production never contacted; GET-only there).

---

## Verdict in plain language

**The port does not reproduce the donor's output case-for-case, and on this
sample it is modestly better overall — but the headline agreement numbers are
weaker evidence than they look, and two of the five stages cannot be compared
on agreement at all.**

Concretely:

- **`tags` is the one stage that genuinely replicates.** 85% exact set match
  (89% excluding the cases the port cannot read), mean Jaccard 97.3%. This is
  the most trustworthy result in the report, because `tags` reads no evidence
  text and is therefore immune to every infrastructure caveat below.
- **`bigo` agrees on 73% of comparable cases (85% excluding blocked cases)** —
  but the two arms were **not given the same prompt** (§6.1), so this number
  measures port-vs-donor *plus* a prompt difference, and cannot be cleanly
  attributed to enricher logic.
- **`timeline`, `allegations` and `entities` show 0% exact agreement.** This is
  expected and is **not** a finding: these fields are LLM-generated prose, and
  two runs never produce byte-identical prose. The meaningful comparisons are
  structural and reviewer-based, reported in §4.
- **The port produces far more timeline data than the donor** (18 cases vs 6),
  because the donor's timeline write path **fails against today's API** (§5.2).
  This is the single largest behavioural difference in the run.
- **The port has a real regression** that makes it unable to read 8.1% of
  cases the donor reads fine (§5.1).

**Case reviewer (the benchmark): Arm A 63.2, Arm B 65.3** across all 20;
**61.1 vs 64.7** excluding the two cases the port cannot fetch. The port scores
higher on 14 of 18 cases and lower on 1. That gap is driven almost entirely by
`timeline`, where the donor writes nothing on cases the port populates — it is
**not** evidence that the port extracts better prose.

**Would I call the port a faithful behavioural port? No — and not because the
port is worse.** It is a *re-implementation* whose output distribution differs
from the donor's in ways that mostly favour it, on top of one genuine
regression. Anyone expecting drop-in equivalence should not read these numbers
as confirming it.

---

## 1. Sample and method

### 1.1 Frame

| | cases |
|---|---|
| Cases in local seed | 354 |
| **Eligible frame** | **223** |
| Excluded: no adapter-mapped material | 130 |
| Excluded: charge-sheet-only | 1 |

The frame is *cases carrying at least one adapter-mapped material*
(`press_release`, `ciaa_press_release`, `court_order`). This restriction is
load-bearing, not convenience: **0 of 354 cases carry a native `source` key**,
so Arm A sees nothing at all without the input adapter (§6.2), and that adapter
maps only those three types. Sampling outside the frame would have produced
cases where Arm A structurally cannot extract, manufacturing empty-vs-empty
"agreement" — the exact failure this project has been guarding against.

**Charge-sheet exclusion.** The adapter deliberately leaves `charge_sheet`
unmapped, because the donor's `MILESTONE_SOURCE_TYPES` treats a charge sheet as
a separate, *higher*-priority type (`AG_ABHIYOG_PATRA`). Exactly **one** case in
the seed (`case-081-cr-0123-081-cr-0123-d7e9a3`) has a charge sheet and no
mapped material, so this exclusion costs almost nothing. It is excluded rather
than reported separately because a single case supports no conclusion.

### 1.2 Selection

20 cases, stratified by evidence shape, allocated proportionally with at least
one case per non-empty stratum, ordered within stratum by a seeded hash of the
slug (seed `task-16`). Reproducible: `casework/ab/sample.py`.

| stratum | in sample |
|---|---|
| press + court | 16 |
| press only | 3 |
| court only | 1 |

Golden coverage within the sample: `bigo` 15/20, `tags` 6/20, `timeline` 7/20,
`key_allegations` 7/20. No sampled case has more than one evidence entry of the
same mapped type, so the known donor-reads-first / port-aggregates-all
difference is **not** exercised here — a limit on generalisability, since 12
cases in the frame do have repeats.

**Sample size is the main limit on this report.** 20 cases at ~2.5 hours of LLM
time is enough to establish direction and to surface defects; it is not enough
to put tight confidence intervals on any percentage. Treat single-case findings
(§5.3) as leads, not rates.

### 1.3 Both arms ran the same model

Arm B forces `claude_cli` + haiku on both tiers via `dev_env_overrides()`. Arm A's
own `bootstrap()` sets the identical four environment variables when invoked
with `--provider claude_cli --model haiku` — it defaults to `proxy`, so passing
these explicitly is mandatory. Confirmed from Arm A's own usage table:
`claude_cli | premium | claude-haiku-4-5-20251001`. Model quality cancels out.

### 1.4 Arm invocation

The donor **writes by default** (`--dry-run` is opt-out); the port is read-only
unless `--apply`. So "apply" means bare `--force` for Arm A and
`--force --apply` for Arm B. Both arms restored the same byte-identical baseline
DB before running, because `enrich_tags` reads fields the other stages write.

---

## 2. Identical-source-text re-verification (the blocker check)

Re-verified per evidence entry on the **actual** sample, comparing the port's
`materials.fetch_markdown` against Arm A's adapter + the donor's unmodified
`content_from_evidence_entry`:

**33 of 36 entries matched byte-for-byte. 3 did not.**

The 3 failures are **not** adapter failures and not text differences — the port
received HTTP 403 where the donor read the document fine. Cause and impact in
§5.1. For the 33 matching entries the property holds and the arms are reading
identical text.

---

## 3. Per-stage results

### 3.1 What each arm actually produced

Counts are from each arm's own run output. Reported separately from agreement
because **an arm that produced nothing has not "agreed" with anything.**

| arm | stage | enriched | extracted | skipped | unmet | error |
|---|---|---|---|---|---|---|
| A | `bigo` | 15 | — | 4 | 1 | 0 |
| A | `tags` | 20 | — | 0 | 0 | 0 |
| A | `timeline` | **6** | — | 11 | 0 | **3** |
| A | `allegations` | 19 | — | 1 | 0 | 0 |
| A | `entities` | — | 19 (dry-run) | 1 | 0 | 0 |
| B | `bigo` | 13 | — | 4 | **3** | 0 |
| B | `tags` | 20 | — | 0 | 0 | 0 |
| B | `timeline` | **18** | — | 0 | 2 | 0 |
| B | `allegations` | 16 | — | 1 | **3** | 0 |
| B | `entities` | — | 16 | 1 | 2 | 1 |

Every `unmet` on Arm B is the User-Agent regression (§5.1). All 3 Arm A
`timeline` errors are `422 Unprocessable Entity` on PATCH (§5.2).

### 3.2 Agreement — all 20 sampled cases

`comparable` excludes rows where **neither** arm produced output. The rate is
over `comparable` only, so a stage where nothing happened reports `n/a`, never
100%.

| stage | cases | comparable | A==B | A==B rate | all three agree | neither produced |
|---|---|---|---|---|---|---|
| `bigo` | 20 | 15 | 11 | **73.3%** | 5 | 5 |
| `tags` | 20 | 20 | 17 | **85.0%** | 0 | 0 |
| `timeline` | 20 | 19 | 0 | 0.0% † | 0 | 1 |
| `allegations` | 20 | 19 | 0 | 0.0% † | 0 | 1 |
| `entities` | 20 | 20 | 0 | 0.0% † | 0 | 0 |

### 3.3 Agreement — excluding the 2 cases the port cannot read

The port could fetch **no source text at all** for
`case-081-cr-0136-oxygen-plant` and `case-081-cr-0060-681d9859` (§5.1). Their
divergence measures a missing HTTP header, not extraction quality. `tags` is
unaffected either way — it reads no evidence.

| stage | cases | comparable | A==B | A==B rate | all three agree | neither produced |
|---|---|---|---|---|---|---|
| `bigo` | 18 | 13 | 11 | **84.6%** | 5 | 5 |
| `tags` | 18 | 18 | 16 | **88.9%** | 0 | 0 |
| `timeline` | 18 | 18 | 0 | 0.0% † | 0 | 0 |
| `allegations` | 18 | 17 | 0 | 0.0% † | 0 | 1 |
| `entities` | 18 | 18 | 0 | 0.0% † | 0 | 0 |

**† These 0.0% figures are not findings.** `timeline`, `key_allegations` and
`entities` are LLM-generated prose; byte equality between two runs is
vanishingly unlikely regardless of how well the port works. Quoting "0%
agreement" for these stages without this note would badly misrepresent the
result. See §4 for the comparisons that do carry signal.

---

## 4. Field-appropriate comparison

### 4.1 `bigo` — exact integer match

11 of 13 comparable cases agree exactly (excluding blocked cases). The two
divergences:

| case | Arm A | Arm B | golden |
|---|---|---|---|
| `case-080-cr-0064-anup-mehra-land-fraud` | 50,542,000 | **505,420,000** | 50,542,000 |
| `case-080-cr-0174-vikal-paudel-illegal-assets` | 621,918,684 | 6,221,918,684 | 6,219,188,684 |

On `0064` Arm B is **exactly 10× high** and Arm A matches both golden and the
source document. On `0174` the direction reverses: Arm A is ~10× *low* and Arm
B is nearer golden. See §5.3 — this is the paisa 10× failure class, and it is
**not** a code regression.

Also note 5 cases where **neither** arm produced a bigo, 3 of which have a
golden value. Both arms failed to reproduce June's number there; that is
reported as `no_output`, never as agreement.

Four cases show both arms diverging from golden **in golden's disfavour**:
golden carries a 10×-inflated value (e.g. `0114` golden 42,293,589 vs both arms
4,229,358; `0145` golden 1,471,085,482 vs both arms 147,108,548). Both arms are
right and June's shipped value is wrong — the paisa bug that donor commit
`0321a85` was written to fix. **Do not read `both_diverge_from_golden` as a
defect; here it is the fix working.**

### 4.2 `tags` — set comparison

| metric | all 20 | excl. blocked |
|---|---|---|
| exact set match | 17/20 (85.0%) | 16/18 (88.9%) |
| mean Jaccard(A,B) | **97.3%** | — |
| mean precision (B's tags also in A) | 98.5% | — |
| mean recall (A's tags also in B) | 98.5% | — |

**This is the strongest replication result in the report.** Both arms enriched
all 20 cases, no unmet, no errors, and the disagreements are single-tag
differences rather than wholesale divergence. `tags` reads no evidence text, so
it is untouched by the adapter, the User-Agent regression and the prompt
asymmetry alike — which is precisely why it is the cleanest measurement here.

### 4.3 `timeline` — structural (dates and counts)

| metric | value |
|---|---|
| Cases where Arm A produced a timeline | **6 / 20** |
| Cases where Arm B produced a timeline | **18 / 20** |
| Mean date Jaccard, all 19 comparable | 4.8% |
| **Mean date Jaccard where BOTH produced (n=5)** | **18.4%** |
| Exact ordered date match | 0/19 |

The 4.8% figure is dominated by 13 cases where Arm A produced nothing; it
restates the donor's write failure rather than measuring extraction agreement.
**18.4% over the 5 cases where both arms produced is the honest number**, and it
is low: even where both arms build a timeline, they largely pick *different
dates*, not merely different prose for the same dates. Entry counts also differ
substantially (e.g. `0145`: A 17, B 29).

**On this evidence the port's timeline stage does not reproduce the donor's, in
either coverage or content.** It produces much more, and what it produces
overlaps the donor's poorly. Whether "more" is "better" is a question the
mechanical comparison cannot answer; see §4.5.

### 4.4 `key_allegations` and `entities` — prose

Exact comparison is meaningless (§3.3 †). Production counts:

- `allegations`: Arm A 19 cases, Arm B 16 (3 of the 4-case gap is the
  User-Agent regression).
- `entities`: Arm A extracted 128 items across 19 cases; Arm B 92 across 15.
  **Extraction only** — see §5.4 for why the write paths are not comparable.

Entity counts are not directly comparable in kind: Arm A's count comes from its
per-item dry-run lines, Arm B's from its own reported total. Both were captured
per-slug in isolated subprocesses, so attribution is exact, but a raw
128-vs-92 comparison should be read as "the donor proposes more entities", not
as a quality judgement.

### 4.5 Case reviewer — the benchmark

Scored with `review.scorer.score_case`, **fully offline and deterministic**:
rules live in code (not the DB), `converted_sources=[]` and
`source_analyses=[]` suppress the judge's source analysis, and all 8 LLM-graded
rules are filtered out so the benchmark carries no sampling noise. Both arms are
graded against the same rule set (`assert_same_rule_basis` enforces it).

| | all 20 | excl. blocked (18) |
|---|---|---|
| **Arm A mean overall** | 63.2 | 61.1 |
| **Arm B mean overall** | **65.3** | **64.7** |
| B scores higher | 14 cases | 14 cases |
| A scores higher | 3 cases | 1 case |

**What is actually driving this, stated plainly:** the `timeline_completeness`
rule. On the 12 cases where Arm A wrote no timeline, Arm A scores **0** on that
rule and Arm B scores 68–82. Strip that rule out and the arms are close. The
gap measures *coverage*, not extraction quality.

**What the reviewer cannot tell you here — do not over-read it:**

- `bigo` — well covered (dedicated gate rule, weight 1.2).
- `timeline` — well covered (dedicated rule: count, BS/AD dates, description
  depth, chronological order).
- `key_allegations` — **weak**. No dedicated rule; a 24-point *count* sub-term
  (`min(n/4,1)*24`) inside `structural_completeness`.
- `tags` — **weakest**. No dedicated rule; an 8-point count sub-term.

So for the two prose stages where exact comparison failed, the reviewer's
signal is **a count, not a quality judgement**. The honest position is that
**neither exact comparison nor the reviewer establishes which arm writes better
allegations.** I have not substituted my own judgement for that gap.

---

## 5. Defects and incompatibilities revealed

### 5.1 PORT REGRESSION — `fetch_markdown` sends no User-Agent (8.1% of cases)

`casework/common/materials.py::fetch_markdown` calls
`urllib.request.urlopen(link)` with **no headers**. The `s3.jawafdehi.org` WAF
rejects non-browser User-Agents with 403.

Four-way probe against a real s3 MARKDOWN link — the fourth is decisive, since
a three-way test cannot separate the header from credentials:

```
bare (no UA, no auth)  -> HTTP 403
browser UA only        -> HTTP 200, 14599 bytes
UA + Basic auth        -> HTTP 200, 14599 bytes
auth only, no UA       -> HTTP 403      <-- auth alone does NOT fix it
```

**It is a missing request header, not a missing credential.** The donor succeeds
because `review/jds_client.py:127` sends `headers={"User-Agent": UA}`. The
constant already exists four times in this codebase (`casework/convert.py:43`,
`casework/ab/snapshot.py:9`, `casework/common/api.py:7`,
`review/jds_client.py:18`); `materials.py` is the one fetch path that omits it.
The fix is one line reusing an existing constant.

**Impact, measured over the 223-case frame:**

| | count |
|---|---|
| Mapped materials hosted on s3 | 28 / 413 (6.8%) |
| Cases with ≥1 s3-hosted markdown | 20 / 223 (9.0%) |
| **Cases the port can read NOTHING for** | **18 / 223 (8.1%)** |

Two such cases fell in the sample (10%, consistent with the base rate). The port
additionally attempts `charge_sheet` (its `PRESS_TYPES` is deliberately wider
than the donor's), so it has *extra* exposure where a charge sheet is s3-hosted.

**The port fails loudly, not silently** — `materials.source_text` catches the
exception and records `"MARKDOWN fetch failed (HTTP Error 403: Forbidden)"` as
an unmet prerequisite, which surfaces in the run summary. That is the pipeline's
unmet-reason design working as intended, and it is the only reason this A/B
could detect the regression at all rather than reading it as "the port found
nothing here".

### 5.2 DONOR INCOMPATIBILITY — timeline PATCH returns 422

Arm A's timeline stage hit `422 Unprocessable Entity` on PATCH for **3 of 20
cases** (`case-080-cr-0117`, `case-080-cr-0175`, `case-081-cr-0060`). The donor
extracted a timeline and then could not write it: today's serializer rejects its
payload shape.

Combined with 11 cases the donor skipped outright, the donor successfully wrote
a timeline for only **6 of 20** cases. **This, not extraction quality, is the
main reason the port's reviewer score is higher.** It also means the timeline
comparison is substantially a comparison against a partly non-functional arm —
weigh §4.3 accordingly.

### 5.3 The paisa 10× failure class survives at the LLM layer (1 case)

Donor commit `0321a85` is *"drop paisa before parsing bigo to stop 10x
inflation"*, and that bug shipped to production once already. I checked whether
the port regressed it: **the paisa-stripping code is byte-identical between
donor and port.** No code regression.

But on `case-080-cr-0064` Arm B still returned exactly 10× the correct figure.
The material's `display_name` states `बिगो रु.५,०५,४२,०००।–` = 50,542,000 —
the correct value, present in Arm B's own prompt — and Arm B returned
505,420,000 anyway. The failure is the model misreading Devanagari digit
grouping, downstream of a parser that is doing its job.

**Flagging this loudly despite n=1**, because this exact failure class reached
production before, the parser fix does not prevent it, and 1 in 13 enriched
cases is not a reassuring rate. It warrants a targeted guard (e.g.
cross-checking the extracted figure against `display_name`), not just the
existing parser fix. It is **not** a port-vs-donor difference in code.

### 5.4 DONOR WRITE PATH — entity creation returns 404, not the documented 400

The brief and `arm_a_patches.md` both record that the donor's entity write
"400s against the current `EntityPatchItemSerializer`". **Observed under
`--apply`, that is not what happens.** Every `create_entity` call returns:

```
Failed to create entity '<name>': 404 Client Error: Not Found
  for url: http://127.0.0.1:48010/api/entities/
```

`POST /api/entities/` **does not exist** on today's API. All 7 creations 404'd,
so `entities_to_patch` stayed empty and the PATCH that would have hit the
serializer was **never reached** — the documented 400 is unobservable because
the donor fails one step earlier and more fundamentally.

This strengthens the case for the port being extraction-only, but the recorded
reason should be corrected: the endpoint is gone, not merely stricter.

*(Method note: Arm A's entities stage ran `--dry-run` during the main A/B so
extraction counts could be captured cleanly. This 404 was observed in two
separate targeted `--apply` runs afterwards. No writes succeeded, so the local
DB was unaffected.)*

### 5.5 Not a defect: the port's wider `PRESS_TYPES`

The port's `PRESS_TYPES` includes `charge_sheet`; the donor's does not. This is
a documented, deliberate deviation (Task 8 measured `charge_sheet` MARKDOWN
coverage at 100% vs `press_release` at 8.6%). It means the two arms do not
always read the same *set* of materials even when they read identical text from
the materials they share.

---

## 6. Caveats that limit what these numbers mean

### 6.1 The arms did not receive the same prompt

**This is the most serious limitation on the `bigo` comparison and I found it
only while investigating §5.3.**

The port logs a `bigo context` block on 17 of 20 cases; the donor logs it on
**0**. The donor's `_build_source_context_from_entry` reads
`source.title`/`source.description` — fields the adapter **deliberately does not
populate** (documented in `arm_a_patches.md`: synthesising them would be
manufacturing donor input). The port's replacement, `_source_metadata`, builds
context from the case title and `material.display_name` instead.

So Arm A's bigo prompt is **source text alone**, while Arm B's is **source text
plus ~800 characters of metadata that frequently contains the बिगो figure
itself**. Identical source text was verified (§2); identical *prompts* were not,
and are not the case.

**Consequence:** the `bigo` agreement rates (73.3% / 84.6%) measure port-vs-donor
*plus* a prompt-content difference. They cannot be attributed to enricher logic
alone. Arm A is also handicapped relative to what the donor actually had in June,
when `DocumentSource.title` was populated for real. Note the direction is not
simply "more context is better" — on `0064` the extra context contained the
correct answer and Arm B still got it wrong.

### 6.2 Arm A rests entirely on one BEHAVIOURAL patch

Every Arm A extraction result depends on the input adapter that synthesises
`entry["source"] = {"source_type": ..., "urls": ...}` from
`material.material_type`. **I re-verified independently that 0 of 354 cases
carry a native `source` key** — without the adapter, four of five donor
enrichers extract nothing at all and this entire A/B would be empty-vs-empty.
This is one deliberate, documented, authorised deviation, and it is the
foundation under every number in §3 and §4 except `tags`.

### 6.3 Three donor bugs are deliberately preserved — agreement on them is expected

These appear in **both** arms by design. Agreement here is **not** evidence of
port quality and must not be counted as such:

1. **Dead NGM hearing path** — `startswith("special:")` vs today's full IRIs;
   0/109 match, so the lookup never fires.
2. **Dead Special/Supreme Court tags** — identical colon-prefix bug; these
   context tags are never produced by either arm.
3. **Spelled-out-Nepali-numeral gap** in the headcount guard.

Part of the 97.3% tags Jaccard is agreement on *not* emitting court-context tags
that neither arm can emit.

### 6.4 Other limits

- **`related_entities` is compared on extraction only.** The write paths are not
  comparable in principle (§5.4).
- **Sample size 20**, one seed, one model, one run. No repeated sampling, so
  LLM run-to-run variance is not separated from arm differences. A rerun would
  move these percentages.
- **No sampled case exercises the donor-reads-first vs port-aggregates-all
  difference** (§1.2).
- **The frame excludes 130 evidence-poor cases**; results generalise to
  evidence-bearing cases only.
- **`missing_details` was not compared** — no ported enricher writes it.

---

## 7. Run incident: concurrent duplicate harness

**Cause: controller action, not a harness defect.** Nothing in `run_ab.py`
launched or permitted the duplicate.

After the first run was killed mid-way through Arm A `allegations`, the
coordinator independently relaunched the harness from scratch (PID 1677659, no
`--resume`) while I recovered and relaunched with `--resume` (PID 1679732). For
roughly three minutes two separately-launched harnesses targeted the same local
sqlite DB. The coordinator's initial diagnosis was that this had overwritten Arm
A's `bigo` values.

**Verification found no surviving contamination**, on three independent checks:

1. Per-slug `bigo` in the live DB vs the values Arm A's own log reported:
   **15/15 enriched slugs matched exactly, 0 mismatches.**
2. The 3 slugs flagged as duplicate-written were **identical to the baseline DB
   *and* to golden** — June's pre-existing residue, present before either run
   started. The original diagnosis compared "my log said None" against "the DB
   says 190000" and inferred an overwrite; the correct comparison is against
   *baseline*, which shows the values predate both runs.
3. Live DB vs the `ckpt_A_timeline` checkpoint across all 20 slugs for `bigo`,
   `tags` and `timeline`: **0 differences.**

The mechanism is that `restore_dbs()` is a whole-file overwrite of all three
sqlite files, so anything the duplicate wrote before that restore was discarded.
The coordinator independently re-checked the baseline and checkpoint files and
withdrew the diagnosis.

**No re-run was performed, and a targeted one would have been harmful:**
`enrich_tags` reads `bigo`, and tags/timeline were computed against run 1's bigo
values, so re-running bigo alone would have left tags derived from inputs that no
longer existed. Correctness would have required a full ~60-minute Arm A re-run
to repair damage that did not occur.

**Belt and braces:** even had a write survived, `resolve_arm_values` credits an
arm only where its *own* outcome is `enriched`. All 3 disputed slugs are
`skipped`, so they resolve to `None` regardless of DB contents.

**Residual caveat:** if the duplicate re-wrote one of the 15 enriched slugs with
an *identical* value, a value comparison cannot distinguish that from "never
touched". That is a provenance ambiguity, not a data one — the numbers are the
same either way.

**Suggested hardening (out of scope here):** `assert_port_is_ours` prevents
writing into *another user's* server; it is not, and was never intended as, a
mutual-exclusion lock between two of our own concurrent runs. A pid/lockfile in
`--work` would close that gap.

---

## 8. Harness defects found and fixed during this task

Recorded because each would have corrupted this report, and three were in my own
code:

1. **The residue trap.** Sample cases already carry June's values, so a plain
   readback returns June's value whenever an arm produced *nothing* — crediting
   both arms with output neither generated, and scoring it as agreement with
   each other *and* with golden. The `no_output` guard does **not** catch this,
   because the field is not empty. Fixed by making each arm's own reported
   outcome the authority: only `enriched` counts as production.
2. **Summary misattribution.** The run summary follows the last case block and
   contains `Cases llm error  0`; the parser kept attributing to the last case
   seen, silently marking one case per stage as `error`. Verified live against
   the real Arm A logs — it mislabelled the same case in all three completed
   stages, making Arm A look worse than it was.
3. **`unmet` misclassified as `error`.** The port reports a WAF 403 as
   "... MARKDOWN fetch failed", which a generic `"failed"` match filed as an
   extraction error — overstating the port's error rate and hiding the
   infrastructure cause.
4. **Not resumable.** A killed run cost an hour of LLM calls; stages now
   checkpoint.

Mutation testing: **32 mutations, 31 caught, 1 verified equivalent mutant**
(`source_analyses=None` vs `[]` cannot differ — `review/judge.py:272` returns
`[]` before any LLM call when `converted_sources` is empty). Six survived the
first pass: four were genuinely weak tests of mine (a `__ne__` test that never
exercised `__eq__`; a symmetric precision/recall fixture; an ordered-dates
fixture whose date *sets* also differed; a splice test that never tried to
overwrite a rule-basis field), one was a broken test harness, one equivalent.
All fixed and re-verified.

---

## 9. Reproducing

```bash
uv run python casework/ab/run_ab.py \
  --arm-a  work/2026-07-17-enricher-extraction/arm_a \
  --snapshot work/2026-07-17-enricher-extraction/snapshot \
  --survey <survey.json> --work <workdir> --n 20 --seed task-16 --apply
uv run python casework/ab/analyse.py --raw <workdir>/ab_raw.json \
  --cases <snapshot>/cases --golden <snapshot>/golden.json \
  --blocked case-081-cr-0136-oxygen-plant,case-081-cr-0060-681d9859 \
  --out <workdir>/tables.md
```

Raw results, per-stage logs and per-stage DB checkpoints are under
`work/2026-07-17-enricher-extraction/ab_run/` (gitignored).

---

## 10. Queue for follow-up

| # | Item | Severity |
|---|---|---|
| 1 | `materials.py::fetch_markdown` sends no `User-Agent` → 403 on 8.1% of cases. One-line fix, constant already exists. | **High** |
| 2 | Donor timeline PATCH 422s on today's API (3/20). Affects any donor-shaped payload still in use. | Medium |
| 3 | 10× paisa inflation still reachable at the LLM layer despite an intact parser fix. Consider cross-checking against `display_name`. | Medium |
| 4 | Correct the record: donor entity creation 404s (endpoint gone), it does not 400. | Low (doc) |
| 5 | Prompt asymmetry between arms (§6.1) — decide whether the port's richer context is intended, then re-measure `bigo`. | Medium (method) |
| 6 | Add a pid/lockfile guard to `run_ab.py --work`. | Low |
