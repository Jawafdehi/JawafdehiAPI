# Jawafdehi case-upload dedup — design (detect + merge)

**Status:** implemented. Stage 1 (detect, read-only default) + Stage 2 (merge, `--apply`).
**Date:** 2026-07-14
**Scope:** backend-only (`services/JawafdehiAPI`). One command,
`dedup_jawafdehi_materials`, read-only by default (`--dry-run`) with an opt-in
mutating `--apply`. No frontend changes.

## Context

The Data Quality page shows a source labeled "Jawafdehi original documents" — the
744 materials whose `@id` is `/material/jawafdehi/<ident>`. These are documents
caseworkers attached to individual cases (via the importer/seeder path, not the
hashing upload endpoint — every one has `sha256: null`).

A manager review asks us to **plan for a deduplication**: many of these uploads
are ordinary press releases, charge sheets, court orders, etc. — the same
documents we may already hold canonically under `ciaa_press_release`, `ag`,
`court_order`, `nkp`, `legal_corpus`. The concern is redundancy: caseworkers
re-uploading documents already in the corpus.

Deduplication is never a single destructive step. It is staged:

1. **Detect, read-only** (`--dry-run`, default) — find which jawafdehi materials
   duplicate a canonical document + print the merge plan. Mutates nothing. **← this spec.**
2. **Merge** (`--apply`) — repoint each case reference to the canonical material and
   soft-delete the confirmed duplicate (canonical visibility left untouched). Touches
   evidence; runs behind review, operator-driven. **← this spec (Stage 2 below).**
3. **Prevent (ingest guard)** — catch dupes at upload time. **Deferred.**

Both stages live in one command, `dedup_jawafdehi_materials`; the detect pass is the
default and the operator reviews its output before running `--apply`.

## Goal

A management command, `dedup_jawafdehi_materials` (read-only by default), that scans
every `/material/jawafdehi/*` material, decides whether it duplicates a canonical
corpus material by **natural key**, and writes a reviewable report plus a printed
summary. It classifies every material into an outcome bucket and records the
signal that produced each match.

## Non-goals (explicit)

- **No mutation by default.** The default (`--dry-run`) reads only. Mutation happens
  only under the explicit `--apply` flag (Stage 2, below).
- **No fuzzy / text-similarity matching.** Natural-key only (see Match scope). The
  charge-sheet bucket, which has no shared key with the AG corpus, is reported as
  `no_canonical_key`, not force-matched.
- **No content-hash backfill.** The materials carry no `sha256`; we do not fetch
  S3 bytes to hash them.
- **No frontend, no statistics-payload field.** The command's summary is a natural
  future feed for a page metric, but wiring that is out of scope here.
- **No new dependencies.**

## Data findings (grounding — from the live API, 2026-07-14)

Public (LISTED) jawafdehi materials sampled: 242 (the stats total of 744 also
counts draft/in-review materials that anon callers can't see). Every one carries
`jawafdehi:sourceType` (0 missing), which is 1:1 with its document type:

| `jawafdehi:sourceType` | count | canonical twin source | keyed by | matchable? |
|---|---:|---|---|---|
| `NEWS` | 96 | (none — no news source in corpus) | — | no twin |
| `MISC` | 50 | (none — generic) | — | no twin |
| `AG_ABHIYOG_PATRA` | 28 | `ag` (99,750) | AG **internal id** | **no shared key** |
| `CIAA_PRESS_RELEASE` | 26 | `ciaa_press_release` | **PR number** | ✅ by number |
| `COURT_ORDER` | 17 | `court_order` | `<court>.<case_number>` | ✅ by case number |
| `SOCIAL_MEDIA` | 10 | (none) | — | no twin |
| `LAW_OR_BILL` | 8 | `legal_corpus` (0 public rows) | no clean number key | no key |
| `COURT_FILING_OTHER` | 5 | `court_order` | `<court>.<case_number>` | ✅ by case number |
| `OAG_AUDIT_REPORT` | 2 | `official_report` | no clean number key | no key |

The matchable number lives in `name.ne` as free text, in **mixed script** — e.g.
press release `३१५५` (Devanagari = 3155), court-case number `०८१-CR-०१३८`
(Devanagari digits + ASCII `CR` = 081-CR-0138). So matching is a name parse +
Devanagari-digit normalization, then a canonical-existence check against the
`Material` table.

**Honest expected yield:** the natural-key matcher attempts a real match on
~`CIAA_PRESS_RELEASE` + `COURT_ORDER` + `COURT_FILING_OTHER` ≈ 48 of 242 visible
(~20%). Charge sheets (28) are explicitly `no_canonical_key`. News + social + misc
(~156, ~64%) are `no_canonical_twin`. Deduplication here is *partial by nature* —
the report makes that concrete rather than implying "744 → tiny."

## Architecture — two pieces

### 1. Pure matcher — `materials/dedup.py` (no DB, unit-testable)

DB-free so it tests on sqlite / no live DB, mirroring `person_sector.py`.

```python
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

@dataclass(frozen=True)
class CanonicalRef:
    source: str          # e.g. "ciaa_press_release", "court_order"
    ident: str | None    # exact ident when known; None => suffix match on case_number
    case_number: str | None  # set for court_order matches (matched by suffix)
    signal: str          # human-readable reason, e.g. "CIAA press release no. 3155"

class Outcome:
    HAS_KEY = "has_key"                  # parsed a canonical key (existence TBD by command)
    NO_CANONICAL_KEY = "no_canonical_key"    # type known, but no shared key exists (charge sheets, laws, reports)
    NO_CANONICAL_TWIN = "no_canonical_twin"  # type has no canonical source at all (news, social, misc)

def normalize_digits(text: str) -> str: ...       # Devanagari -> ASCII

def extract_canonical_key(data: dict) -> tuple[str, CanonicalRef | None]:
    """Return (outcome, ref). ref is set only when outcome == HAS_KEY.
    Routes on data['jawafdehi:sourceType']; parses the number from data['name']['ne']."""
```

Routing rules (by `jawafdehi:sourceType`):

- `CIAA_PRESS_RELEASE` → parse the press-release number from `name.ne`
  (the digits following `विज्ञप्ति नं.` / `प्रेस विज्ञप्ति नं.`), normalize, →
  `CanonicalRef("ciaa_press_release", ident=<n>, signal=...)`. If no number
  parses → `NO_CANONICAL_KEY`.
- `COURT_ORDER`, `COURT_FILING_OTHER` → parse a court-case number
  (`\d{2,4}-(CR|WF|WO|RE|WH|MS|...)-\d+` over the digit-normalized name), →
  `CanonicalRef("court_order", ident=None, case_number=<case>, signal=...)`.
  No number → `NO_CANONICAL_KEY`.
- `AG_ABHIYOG_PATRA`, `LAW_OR_BILL`, `OAG_AUDIT_REPORT` → `NO_CANONICAL_KEY`
  (type is known but there is no shared natural key to the canonical corpus).
- `NEWS`, `SOCIAL_MEDIA`, `MISC` (and unknown/missing sourceType) →
  `NO_CANONICAL_TWIN`.

The court-case-code alternation is a module constant so it is easy to extend.
`name.ne` may be a bilingual dict or a plain string; a small `_text()` helper
(mirroring `person_sector._text`) handles both.

### 2. Command — `materials/management/commands/dedup_jawafdehi_materials.py`

Read-only by default (`--dry-run`); `handle()` for the detect pass:

`handle()`:

1. Query `Material.objects.filter(source="jawafdehi", is_deleted=False)` (the
   `materials` app auto-routes to the `ngm` DB; no explicit `.using` needed).
   Use `.iterator(chunk_size=...)` — the set is small (~744) but streaming keeps
   it uniform with the stats iterators.
2. For each, call `extract_canonical_key(row.data)`:
   - `NO_CANONICAL_TWIN` / `NO_CANONICAL_KEY` → record that bucket + reason.
   - `HAS_KEY` → run the **canonical existence check** in-DB:
     - exact ident: `Material.objects.filter(source=ref.source, ident=ref.ident, is_deleted=False).exists()`
     - court_order (case-number suffix): `Material.objects.filter(source="court_order", ident__endswith=f".{ref.case_number.lower()}", is_deleted=False)` — take the first match's IRI.
     - Also honor the on-the-fly court-case derivation is **not** needed here; we
       match against stored `court_order` rows only (a stored canonical row is
       what a merge would repoint to).
     - Exists → bucket `duplicate`, record `canonical_iri`.
     - Absent → bucket `key_but_absent` (we parsed a key but hold no canonical
       copy — this material is the only copy; a merge would keep it).
3. Blast-radius: for each material, list the cases that reference it —
   `CaseMaterialReference.objects.filter(material_iri=row.iri).values_list("case__slug", flat=True)`
   (cross-DB read from the default DB; read-only).
4. Write a JSONL report and print a summary.

Final outcome buckets: `duplicate`, `key_but_absent`, `no_canonical_key`,
`no_canonical_twin`.

Command options: `--dry-run` (default; read-only detect + merge plan), `--apply`
(Stage 2 merge — mutually exclusive with `--dry-run`), `--limit N` (cap rows, for
spot checks / staged apply), `--output <path>` (default `dedup-<runstamp>.jsonl` in
the CWD; `--output -` streams the JSONL report to **stdout** and routes the human
summary to **stderr**, the retrievable channel on an ephemeral prod pod).

### Report schema (one JSON object per line)

```json
{
  "jawafdehi_iri": "https://jawafdehi.org/material/jawafdehi/20260507.77db76dc",
  "source_type": "CIAA_PRESS_RELEASE",
  "name": "CIAA प्रेस विज्ञप्ति नं. ३१५५ — …",
  "outcome": "duplicate",
  "canonical_iri": "https://jawafdehi.org/material/ciaa_press_release/3155",
  "signal": "CIAA press release no. 3155",
  "referencing_cases": ["case-081-cr-0138-jhalak-poudel"]
}
```

Printed summary: a bucket-count table + the top line
`N of M jawafdehi materials duplicate a document we already hold` and a
by-`source_type` breakdown (the re-bucket view, produced for free).

## Testing (TDD)

**Pure matcher** — `materials/tests/test_dedup.py` (no DB):

- `normalize_digits("३१५५") == "3155"`.
- CIAA press release name → `HAS_KEY`, `CanonicalRef(source="ciaa_press_release", ident="3155")`.
- court-order name `…०८१-CR-०१३८…` with `sourceType=COURT_ORDER` → `HAS_KEY`,
  `case_number == "081-cr-0138"`, source `court_order`.
- `AG_ABHIYOG_PATRA` → `NO_CANONICAL_KEY`.
- `NEWS` / `SOCIAL_MEDIA` / `MISC` → `NO_CANONICAL_TWIN`.
- `CIAA_PRESS_RELEASE` with a number-less name → `NO_CANONICAL_KEY`.
- `name` as bilingual dict vs plain string both parse.

**Command (detect)** — `materials/tests/test_dedup_command.py` (`@pytest.mark.django_db`):

- Seed a jawafdehi `CIAA_PRESS_RELEASE` material (name carries `३१५५`) **and** a
  canonical `Material(source="ciaa_press_release", ident="3155")` → that row lands
  in `duplicate` with the right `canonical_iri`.
- Seed a jawafdehi press release whose number has **no** canonical row →
  `key_but_absent`.
- Seed a `NEWS` material → `no_canonical_twin`.
- Assert the summary bucket counts; assert `referencing_cases` is populated when a
  `CaseMaterialReference` points at the material.
- Assert **no writes**: material `updated_at` / count unchanged after the run.

**Merge (`--apply`)** — `materials/tests/test_dedup_merge.py`: repoint (note+ordinal
preserved), collision-dedupe, save()-not-update() soft-delete (search eviction),
canonical-visibility-untouched, and idempotency.

Run: `pytest materials/tests/test_dedup.py materials/tests/test_dedup_command.py materials/tests/test_dedup_merge.py -v`.

## Files

- Create `materials/dedup.py` — pure matcher.
- Create `materials/management/commands/dedup_jawafdehi_materials.py` — command.
- Create `materials/dedup_merge.py` — Stage 2 merge (`plan_merge`/`apply_merge`).
- Create `materials/tests/test_dedup.py`, `materials/tests/test_dedup_command.py`,
  `materials/tests/test_dedup_merge.py`.
- No changes to models, statistics, the API payload, or the frontend.

## Deployment note (matters here)

The database is reachable only through the API — there is **no local DB**. So:

- The command's **correctness** is proven by the sqlite fixture tests above, run
  locally / in CI.
- The command produces **real numbers only on a deployed environment** (staging /
  prod) where it can reach the real Postgres. It is run there, on demand, by an
  operator — never against a local DB (there isn't one). Its output is a file +
  stdout summary an operator reviews.

This is why Stage 1 is a management command rather than a stats aggregate: the
audit is an on-demand review artifact, not a per-request page number.

## Stage 2 — Merge (`--apply`, implemented)

Lives in `materials/dedup_merge.py` (`plan_merge` / `apply_merge`), django_db-tested in
`materials/tests/test_dedup_merge.py`. For each `duplicate` row, `apply_merge`:

1. **Repoint** each `CaseMaterialReference` from the jawafdehi IRI to `canonical_iri`,
   preserving `additional_details` + `ordinal`.
2. **Collision-dedupe.** `CaseMaterialReference` has a `unique_case_material_reference`
   constraint on `(case, material_iri)`, so if the case already references the canonical,
   a repoint would raise. Instead, fold the jawafdehi ref's note into the existing
   canonical ref and **delete** the jawafdehi ref.
3. **Soft-delete** the jawafdehi material via the sanctioned model `save()` path
   (`is_deleted=True`, `update_fields=["is_deleted","updated_at"]`) so the `post_save`
   search-index eviction signal fires — a raw `.update()` would leave it in search.

**Canonical visibility is deliberately NOT recomputed.** The canonical is NGM-native
public corpus (`ciaa_press_release` / `court_order`), LISTED and public independent of
any case. `recompute_material_visibility` takes the MAX over referring case states with
no NGM-native guard, so recomputing would **demote** a public press release to
PRIVATE/UNLISTED the instant a draft/in-review case referenced it — hiding public data.
Leaving it LISTED leaks nothing: the document was already public, and the draft case
itself stays hidden (case evidence links surface only for PUBLISHED cases).

**Cross-DB + idempotency.** References live on the `default` DB, materials on `ngm`; no
atomic transaction spans both. The per-material ref rewrite is a `default`-DB
transaction; the material soft-delete is a separate `ngm` write. Re-runs are safe — a
soft-deleted material drops out of the detect pass (`is_deleted=False`), and a partial
run (refs moved, material still live) re-detects and completes the soft-delete.

**Operation.** Operator runs `--dry-run` first and reviews the JSONL, then `--apply`
(optionally `--limit N` for a staged rollout), on staging/prod against the real Postgres.

## Stage 3 — Prevent (out of scope, recorded for the plan)

Run the matcher at ingest so a document already in the corpus binds the canonical
material instead of minting a new jawafdehi copy.
