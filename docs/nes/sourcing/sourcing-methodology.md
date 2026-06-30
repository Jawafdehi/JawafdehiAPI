# NES Sourcing Methodology (per-bucket procedure)

> **STATUS (2026-06-28): CURRENT / LIVE.** The 10-stage pipeline + ≥2-source gate are
> the live program (locations bucket already ingested this way). Id-scheme notation
> updated to the canonical `@id` IRI (`https://jawafdehi.org/entity/<prefix>/<slug>`);
> the legacy `entity:<prefix>/<slug>` scheme is gone. Bulk-ingest is `manage.py
> bulk_ingest`, not the old migration runner. See `../../ARCHITECTURE.md` §6.

**Status:** Live operational procedure · companion to `sourcing-plan.md`
**Scope:** The repeatable, numbered procedure a sourcing operator (human or agent)
runs to take *any* bucket of public entities from raw GoN sources into published
NES entities. The plan (`sourcing-plan.md`) decides **what** buckets and **from
where**; this doc is **how** — applied identically to every bucket.

**Read alongside:**
- `sourcing-plan.md` — buckets, source pairs, the locked ≥2-source rule.
- `../../shared/source-acquisition-pipeline.md` — TLS-tolerant fetch, likhit,
  LibreOffice, OCR (the acquisition layer, defined once, reused here).
- `../../shared/entity-resolution-service.md` — the matcher (candidate gen +
  scoring + decision bands) called in stage 5.
- `../../ARCHITECTURE.md` §2 — the canonical `@id` IRI contract
  (`https://jawafdehi.org/entity/<prefix>/<slug>`), ≥2-source gate, privacy carve-out.
- The bulk-ingest path (`manage.py bulk_ingest`: validate → ≥2-source gate →
  upsert + OpenSearch index) used in stage 8 — see `../../ARCHITECTURE.md` §6.
- NES entity shape: now schema.org JSON-LD stored in `nes_service` (`StoredEntity`,
  JSONB `data`, PK `iri`), authored/normalized per `../../shared/research/nes-schema-org.md`.
  The legacy per-type Pydantic models (`nes-api/nes/core/models/`) were deleted.

---

## 0. Principles that bind every stage

- **≥2 verifiable sources or HELD.** A single official `*.gov.np` source is never
  enough. Every published entity carries a *primary* (ID-anchored) attribution and
  an *independent corroborator* attribution. Single-source candidates are **HELD**
  (staged, not inserted, no id minted) until a credible source #2 appears.
- **Resolve before insert.** Never write an entity without first asking the
  resolution service "does this already exist?" Dedup is not a cleanup pass; it is
  a precondition of every write.
- **Provenance on every field that came from a source.** Use `ProvenanceMethod`
  (`imported` | `translation_service` | `human` | `llm`) on `LangTextValue` so a
  reviewer can tell a fetched fact from a translated or hand-entered one.
- **Deterministic ids → idempotent re-sourcing.** The minted
  `https://jawafdehi.org/entity/<prefix>/<slug>` is the upsert key. Re-running a bucket updates, never
  duplicates.
- **Public entities only.** Private individuals enter NES *only* via the NGM
  plaintiff/defendant carve-out, with minimal PII and no family fields — never
  through a sourcing bucket.

---

## The 10-stage pipeline

### Stage 1 — Bucket definition & source-pair identification

**Goal:** Lock what the bucket is and the two sources before any fetching.

1. Write a one-paragraph **bucket definition**: the entity universe, the NES
   `EntityType` and `entity_prefix` it maps to, the expected count, and the known
   authoritative total to check against later (e.g. "ECN says 35,041 local seats").
2. Name **source #1 (primary, ID-anchored)** — the source that carries a stable
   natural key per record (NEC candidate id, company reg-no, cooperative reg-no).
3. Name **source #2 (independent corroborator)** — a *different* publisher or
   document class (gazette, candidate list, Economic Survey, provincial registry).
   "The same registry on a different day" is **not** a second source.
4. Apply the **≥2-source test now:** if no credible source #2 exists for the bucket,
   the bucket is **capped/deferred**, not forced through single-source. Record that
   decision in the plan, not here.

**Produce:** a bucket spec stub — definition, `entity_prefix`, expected count,
known total, source #1, source #2, the natural key (anchor id), and the dedup axis
(e.g. "same person across election cycles").

### Stage 2 — Access assessment

**Goal:** Classify how each source can actually be obtained.

1. For *each* of the two sources, classify the access path: **API / structured
   feed**, **HTML page/table**, or **PDF/scan** (born-digital vs image-only).
2. Probe the host. GoN TLS is routinely expired/mis-chained/self-signed — this is
   **expected, not a failure**. Confirm a TLS-tolerant fetch reaches the document
   (verification relaxed *for fetching only*; public reads, no secrets sent).
3. If the live host is down, identify a **cached/archive copy** (Wayback, the
   source's own PDF mirror) and note which copy will be used.
4. Decide the acquisition path per source and flag the hard ones: image-only PDFs
   → OCR-heavy → higher audit sampling later.

**Produce:** per-source access record — path type, reachable URL (live or archive),
TLS condition observed, and the planned converter route.

### Stage 3 — Acquisition

**Goal:** Pull every source document through the shared pipeline and capture
provenance per document. (Mechanics live in `source-acquisition-pipeline.md`; do
not reinvent them.)

1. **Fetch** TLS-tolerant; fall back to archive/mirror; record the method used.
2. **Detect type + text layer.** Born-digital vs scanned vs office format. A scan
   with no extractable text layer must route to OCR — born-digital extraction
   silently returns garbage on scans.
3. **Normalize through likhit** (`convert_to_markdown` → likhit plugin) to clean
   Markdown:
   - Legacy `.doc/.docx/.xls` → **LibreOffice** (`soffice --headless`) first, then
     likhit/MarkItDown.
   - Image-only PDF → **high-quality Devanagari+Roman+English OCR** before/within
     conversion; capture OCR **confidence**.
   - HTML pages/tables → `convert_to_markdown` on the URL directly.
4. **Capture the provenance block per document:** source URL, fetch method, TLS
   status, converter path, OCR engine + confidence (if used), retrieval date. This
   block becomes the `Attribution.details` content in stage 6.

**Produce:** structured Markdown per source document + a provenance block per
document.

### Stage 4 — Extraction → canonical NES record

**Goal:** Map raw source fields onto the NES entity shape. Output is *candidate*
records (not yet deduped, not yet inserted).

1. **Pick `entity_prefix`** from the bucket (stage 1). The prefix root must equal
   the `EntityType` (`person | organization | location | project`); depth ≤
   `MAX_PREFIX_DEPTH`. The minted id (the `@id` IRI) will be
   `https://jawafdehi.org/entity/<entity_prefix>/<slug>`.
2. **Mint the `slug`** deterministically: a romanized, lowercased, hyphenated form
   of the primary name, disambiguated by the anchor id when names collide
   (e.g. append a short stable hash of the NEC candidate id). Slug must satisfy
   `SLUG_PATTERN` / length bounds. Determinism here is what makes re-sourcing
   idempotent (stage 10).
3. **Bilingual names.** Build `Name` objects with `kind=PRIMARY` and both `ne`
   (Devanagari, `NameParts.full/given/family`) and `en` (romanized) where
   available. Mark each `LangTextValue.provenance`:
   - source Devanagari taken verbatim → `imported`.
   - romanization produced by a transliteration/translation step →
     `translation_service`.
   - operator-entered correction → `human`.
   Alias/birth-name variants go in additional `Name`s; common misspellings in
   `misspelled_names` (these feed resolution recall).
4. **Identifiers.** Emit `ExternalIdentifier{scheme, value}` for every natural key
   the source carries. Use the right `IdentifierScheme` (`wikidata`, `website`,
   etc.); registry/NEC ids that have no enum go under `scheme="other"` with a
   descriptive `name` LangText. These identifiers are the decisive resolution gate.
5. **Type-specific structure.** For persons, populate `PersonDetails`
   (`birth_date` may be partial: "2012", "2012-01") and `ElectoralDetails`. For the
   elected-officials bucket each `Candidacy` carries `election_year`,
   `election_type`, `constituency_id` (a `location` entity id, referential),
   `candidate_id` (the NEC anchor, int), `party_id`, `votes_received`, `elected`,
   and `symbol`.
6. **Attribution placeholders.** Attach the stage-3 provenance block(s); the
   ≥2-source assembly happens in stage 6.

**Produce:** a set of shape-valid candidate records (each passes the minimal
JSON-LD checks — known `@type`, a valid `@id` entity IRI, `name` present) each
carrying its anchor identifier and per-field provenance.

### Stage 5 — Entity resolution / dedup BEFORE insert

**Goal:** For each candidate, decide *attach to existing* vs *create new* — never
blind-insert. Call the shared resolution service (`entity-resolution-service.md`).

1. Run the batch path `POST /resolve/batch` with `dedup_within_batch: true`. Stage
   1 of the matcher reuses the PR-91 OpenSearch index (bilingual: a Devanagari
   record is reachable by a Roman query and vice versa via index-time translit);
   stage 2 scores name-parts + identifier + DOB + address/constituency + cycle
   overlap; stage 3 returns a band.
2. Honor the **confidence bands**:
   - **`auto_accept`** (`score ≥ T_high`, or identifier-exact gate) → attach this
     source to the existing `nes_id`; do **not** create a new entity.
   - **`review`** (`T_low ≤ score < T_high`, ambiguous ties) → queue for human
     review; hold the record.
   - **`create_new`** (`score < T_low`, no candidate) → mint a new entity.
3. **Cross-cycle dedup (the hard part for persons).** The same person is a
   candidate in one cycle and a winner in another, re-romanized differently, with a
   changed address. Lean on the **persistent NEC `candidate_id`** as the
   identifier gate and on `ElectoralDetails.candidacies` overlap; do *not*
   over-penalize address drift across cycles. A confident match means we **append a
   new `Candidacy`** to the existing person, not create a second person.
4. **Intra-batch clustering.** The matcher also blocks the N incoming records
   against one another and collapses duplicates *within* the batch to one
   `cluster_id` → one new entity, not N.
5. **Outage safety.** If the search backend is down (not merely "found nothing"),
   **fail and retry** — never auto-create during an outage, or the batch duplicates.

**Produce:** per-record decision — `attach to <nes_id>` | `review` | `create_new`,
plus cluster assignments for new entities.

### Stage 6 — Two-source attribution assembly

**Goal:** Bind each entity to both sources; HOLD anything with fewer than two.

1. For every entity (existing or new), assemble **two `Attribution` entries**:
   - **src#1** (primary, ID-anchored) — `title` names the source; `details` carries
     the stage-3 provenance block + the anchor id value.
   - **src#2** (independent corroborator) — same shape, a *different* publisher/doc.
2. **Gate:** if an entity has `< 2` independent attributions, mark it **HELD** —
   staged, not inserted, **no id published**. A `create_new` with one source waits
   for source #2; an `auto_accept` that only adds a redundant copy of an existing
   source does not count as a second source.
3. Record bilingual `Attribution.title`/`details` with provenance so the audit can
   distinguish imported vs translated attribution text.

**Produce:** entities partitioned into **PUBLISHABLE** (≥2 sources) and **HELD**
(<2 sources, with the reason logged).

### Stage 7 — Verification gate

**Goal:** Block bad data before bulk ingest. Every check is pass/fail; failures
route to HELD or human audit, not to silent insertion.

1. **Schema validation.** Records pass the minimal JSON-LD checks
   (`validate_jsonld_entity`): `@type` is a known schema.org / `jawafdehi:` type,
   `@id` is a valid canonical entity IRI (`is_valid_entity_iri`), and `name` is
   present. The rest of the document is free-form JSON-LD stored verbatim — there
   is no per-type Pydantic model or `extra="forbid"` reject. Slug/prefix
   constraints satisfied; prefix root matches type.
2. **Per-field provenance present.** Every sourced `LangTextValue` has a
   `ProvenanceMethod`. No bare imported text without provenance.
3. **Referential integrity.** Every `constituency_id` / `party_id` / `location_id`
   resolves to an existing NES entity of the right type (locations are already in
   NES and anchor this). Dangling references fail.
4. **Aggregate count check.** Deduped publishable count is compared against the
   **known authoritative total** from stage 1 (e.g. ECN seat counts). A large gap
   (over- or under-count) blocks the batch for investigation.
5. **Bilingual transliteration consistency.** `en` and `ne` primary names are
   plausibly the same name (translit round-trip sanity); gross mismatches flag.
6. **BS ↔ AD date validity.** Any Bikram Sambat date converts to a valid Gregorian
   date and vice versa (use the `convert_date` tooling); birth dates and election
   years are within sane ranges.
7. **Low-confidence OCR routing.** Records whose source fields came from
   low-confidence OCR are diverted to human audit rather than auto-inserted.
8. **Privacy gate.** Confirm the bucket is public-entity only; no private
   individuals, no family fields populated outside the (non-sourcing) NGM carve-out.

**Produce:** a gate report (counts passed/failed by check) and a clean,
verified, publishable record set.

### Stage 8 — Bulk ingest + OpenSearch index refresh

**Goal:** Write the verified set efficiently and make it discoverable.

1. Use the **bulk-ingest path** from the storage proposal: a batch writer doing
   `COPY` / multi-row upsert into Postgres (entity row + version row + relationship
   rows per record), **keyed on the minted id** (upsert-by-id). A 100k-record
   bucket is one transaction batch, not 100k commits — no git on the write path.
2. `auto_accept` decisions **attach the new source/Candidacy** to the existing
   entity row (a JSONB update + new version row); `create_new` decisions insert new
   rows.
3. After the batch, run **one** OpenSearch bulk reindex (`index_bulk`/`async_bulk`)
   and **`refresh`** the index so the just-written siblings are visible to the next
   resolution batch (avoids missing same-batch duplicates).
4. Versions are append rows (not snapshot files); relationship back-refs are an
   index lookup, not a dual rewrite.

**Produce:** committed entities (Postgres) + refreshed OpenSearch index; an
ingest manifest (ids written, ids updated, version numbers).

### Stage 9 — Stratified human audit

**Goal:** Measure per-source error rate as an SLA, not spot-check randomly.

1. Draw a **stratified sample** across strata that predict error: source #2 type
   (clean API vs scan-only PDF), OCR-confidence band, decision band
   (`auto_accept` vs `create_new`), and cross-cycle-merged vs fresh. Sample more
   heavily from high-risk strata (low-OCR, scan-only second source).
2. Suggested floor: **~2–5%** of the batch overall, with high-risk strata sampled
   up to ~10%. Larger buckets can taper the percentage but never the high-risk
   strata.
3. A reviewer checks each sampled entity against *both* original sources: names,
   identifiers, bilingual consistency, dedup correctness (did two cycles correctly
   collapse to one person? did two distinct same-name people stay separate?).
4. Record a **per-source error rate** and treat it as the bucket/source **SLA**. If
   a source's error rate exceeds threshold, that source's contribution is
   re-extracted or downgraded; do not publish on a failing source.

**Produce:** an audit report — sampled n, per-source/per-stratum error rates,
defect list, and an accept/re-work decision for the batch.

### Stage 10 — Re-run / idempotency

**Goal:** Re-sourcing the same bucket (refresh, new cycle, corrected source) is
safe and converges.

1. **Deterministic ids.** Because slug + prefix are deterministic (stage 4), a
   re-run produces the *same* `https://jawafdehi.org/entity/<prefix>/<slug>` and upserts in place — no
   duplicates. Anchor identifiers (NEC id, reg-no) make the upsert key stable even
   when display names change.
2. **Additive updates.** A new election cycle re-runs stages 1–8; resolution
   `auto_accept`s existing persons and **appends a `Candidacy`**, creating new
   entities only for genuinely new people.
3. **Merge events.** If audit (stage 9) finds two ids that are one person, merge:
   the surviving `https://jawafdehi.org/entity/<prefix>/<slug>` is canonical, the loser becomes an alias
   so existing NGM/Jawafdehi `nes_id` references still resolve.
4. **Re-source on source correction.** A corrected/republished source re-enters at
   stage 3; provenance retrieval-date distinguishes versions; the version row trail
   records the change.

**Produce:** an updated, de-duplicated bucket with a clean version trail and zero
new duplicates.

---

## Per-bucket checklist template

Copy per bucket; an item is done only when its **Produce** artifact exists.

```
Bucket: __________________________   Operator: __________   Date: __________

[ ] 1. Definition & source pair
      entity_prefix: ______________   EntityType: ______________
      expected count: ______   known authoritative total: ______
      source #1 (primary, ID-anchored): ______________
      source #2 (independent corroborator): ______________
      anchor / natural key: ______________   dedup axis: ______________
      ≥2-source test PASSED (else bucket capped/deferred): [ ]
[ ] 2. Access assessment (per source: API/HTML/PDF-scan; TLS condition; route)
[ ] 3. Acquisition (likhit/LibreOffice/OCR done; per-doc provenance captured)
[ ] 4. Extraction → canonical records (prefix, slug, bilingual names, identifiers,
      type-specific structure, per-field provenance)
[ ] 5. Resolution/dedup BEFORE insert (batch resolve; bands honored; cross-cycle
      + intra-batch dedup; outage-safe)
[ ] 6. Two-source attribution assembled; <2-source entities HELD
[ ] 7. Verification gate: schema / provenance / referential / count vs total /
      translit / BS↔AD / OCR-confidence / privacy  — all pass
[ ] 8. Bulk ingest (upsert-by-id) + OpenSearch reindex + refresh
[ ] 9. Stratified human audit; per-source error rate recorded as SLA
[ ] 10. Idempotency confirmed (deterministic ids; re-run produces no duplicates)

Definition of done (see below): [ ]
```

---

## Worked example — ELECTED OFFICIALS pilot bucket

The pilot from `sourcing-plan.md` §5. Chosen because it is a clean bilingual
**person** bucket with stable NEC ids, a real source pair, and it exercises the
hardest capability the whole program depends on: **cross-cycle dedup**.

**Stage 1 — Definition & source pair.**
Universe = candidates and winners across cycles (local 2074/2079, federal,
provincial, CA 2064/2070). `EntityType=person`, `entity_prefix=person`. Expected:
~70k+ positions collapsing to fewer deduped persons. Known total to check against:
ECN's published per-cycle seat/candidate counts. **Source #1 (primary,
ID-anchored):** ECN results (carry the **NEC `candidate_id`**, votes, elected
flag). **Source #2 (independent corroborator):** ECN candidate lists / gazette
notifications. Anchor = NEC `candidate_id` (int). **Dedup axis = same person across
election cycles.** ≥2-source test passes.

**Stage 2 — Access assessment.**
`election.gov.np` presents flaky TLS (seen in research) → TLS-tolerant fetch,
expected, recorded in provenance. Results are partly HTML tables, partly
per-constituency result-sheet PDFs (some scanned → OCR). Candidate lists are
PDF-locked. Route: HTML → `convert_to_markdown`; born-digital PDF → likhit;
scanned result sheets → OCR. Wayback fallback identified for down pages.

**Stage 3 — Acquisition.**
Fetch each cycle's result pages and candidate lists; detect text layer; HTML via
likhit directly, scanned sheets via Devanagari+English OCR (confidence captured).
Per-doc provenance: ECN URL, TLS-bypassed flag, converter path, OCR engine +
confidence, retrieval date.

**Stage 4 — Extraction → canonical person records.**
Per candidate: `Name` with `ne` (Devanagari from ECN, provenance `imported`) and
`en` (romanized, provenance `translation_service`); `slug` =
romanized-name + short hash of `candidate_id` (deterministic, collision-safe);
`ExternalIdentifier{scheme:"other", name:"NEC candidate id", value:<id>}`;
`ElectoralDetails.candidacies = [Candidacy{election_year, election_type,
constituency_id=https://jawafdehi.org/entity/location/constituency/..., candidate_id, party_id,
votes_received, elected, symbol}]`. `PersonDetails` filled where the source gives
it (district → `citizenship_place`/`address`).

**Stage 5 — Resolution/dedup (the hard part).**
`POST /resolve/batch dedup_within_batch=true`. For each candidate, the matcher
searches NES (bilingual recall). **Cross-cycle:** a 2079 winner who was a 2074
candidate hits the existing person via the **persistent NEC `candidate_id`**
identifier gate and `candidacies` overlap, even though the romanization differs and
the address changed → `auto_accept` → **append the new `Candidacy`** to that
person, not create a second. Two distinct "Ram Bahadur" in one district with
different `candidate_id`s stay separate (identifier-conflict → not auto-merged);
ambiguous ties → `review`. Intra-batch: the same person appearing in both the
results sheet and the candidate list collapses to one `cluster_id`.

**Stage 6 — Two-source attribution.**
Each person gets `Attribution`#1 = ECN results (with `candidate_id` in details) and
`Attribution`#2 = ECN candidate list / gazette. A person found only in a result
sheet but absent from any candidate list / gazette is **HELD** until the second
source confirms.

**Stage 7 — Verification gate.**
Schema OK; every `LangTextValue` has provenance; every `constituency_id` resolves
to an existing NES `location` (locations already in NES — referential integrity
holds); deduped winner count per cycle is reconciled against ECN's published seat
totals (count check); en/ne name translit consistency; BS election dates ↔ AD
validity; low-OCR result sheets diverted to audit; privacy gate trivially passes
(all public officials).

**Stage 8 — Bulk ingest.**
Verified persons upserted by `https://jawafdehi.org/entity/person/<slug>` via the bulk path (one batch),
`auto_accept` cases appending a Candidacy as a version-row update; single OpenSearch
reindex + refresh so the next cycle's batch sees them.

**Stage 9 — Stratified audit.**
Sample stratified by source #2 type (gazette vs candidate-list PDF), OCR band, and
**cross-cycle-merged vs fresh** (merges are the highest-risk stratum — audit ~10%).
Reviewer confirms merges are correct (one person, not two) and non-merges are
correct (two same-name people kept separate). Per-source error rate recorded as the
ECN-source SLA.

**Stage 10 — Idempotency / next cycle.**
The next election cycle re-runs the pipeline; deterministic slugs + NEC-id anchor
mean returning candidates `auto_accept` and gain a new `Candidacy`; only genuinely
new candidates create new persons. No duplicates; clean version trail.

---

## Definition of done (per bucket)

A bucket is **done** when:

1. Both sources are acquired through the shared pipeline with per-document
   provenance, and the source pair satisfied the ≥2-source test up front.
2. Every published entity carries **≥2 independent `Attribution` entries** with
   per-field `ProvenanceMethod`; every `<2`-source candidate is explicitly **HELD**
   with a logged reason (not silently dropped, not single-source inserted).
3. Resolution ran **before** insert; cross-cycle and intra-batch duplicates are
   collapsed; the deduped count reconciles against the known authoritative total
   within tolerance.
4. The verification gate passed on schema, provenance, referential integrity, count,
   bilingual translit, BS↔AD dates, OCR confidence, and privacy.
5. Entities are committed via upsert-by-id and discoverable in OpenSearch.
6. A stratified human audit produced a **per-source error rate** within SLA; failing
   sources were re-worked, not published.
7. A re-run is proven idempotent: deterministic ids, additive updates, zero new
   duplicates.
