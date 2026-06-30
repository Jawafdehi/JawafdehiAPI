> **STATUS (2026-06-28): SUPERSEDED (framing) — see DOC-STATUS.md.** The matcher design (PR-91 candidate-gen + feature scoring + decision bands) is still sound, but this doc frames it as a standalone REST microservice and uses the retired `entity:<prefix>/<slug>` id scheme; ids are now `@id` IRIs and resolution is an in-process call inside the monolith. Note: a live resolution-service call is still NOT wired into the bulk-ingest path. Also known-stale: every `nes/search/*` path and the named classes below (`SearchQuery`, `SearchBackend`, `OpenSearchBackend`, `SearchHit`, `SearchResults`, `EntityDocumentBuilder`, `StaticTransliterator`, `FIELD_WEIGHTS`, `build_query_body`) are pre-monolith design sketches that were NOT built as named — the live search is the function-based `shared/jawafdehi_shared/search/` module (`make_client`, `create_index`, `stream_bulk`, `title_translit`, `to_roman`/`to_devanagari`, mappings in `mappings.py`; per-app indexers over these shared helpers), so read those class/path references as design intent rather than live code. Kept as design reference.

# Shared Entity-Resolution Service (bilingual matcher)

Phase 1C design. Defines the **shared** entity-resolution service:
the bilingual matcher consumed by **NES** (dedup before insert), **NGM**
(populating `court_case_entities.nes_id`), and **Jawafdehi** (linking case
entities to canonical NES entities). It builds **on top of** the real PR-91
OpenSearch search substrate (now `shared/jawafdehi_shared/search/` in the monolith), reusing the index,
`EntityDocumentBuilder`, and index-time transliteration as the candidate
generation layer, and adds a scoring/matching/decision layer above it.

Locked context: 3 microservices over REST; OpenSearch is the shared search
substrate; OIDC/Zitadel auth; bilingual Devanagari + Romanized + English data;
must work at ~1M entities.

---

## 1. Purpose & consumers

Resolution answers one question in two shapes: *does this incoming record refer
to an entity that already exists in NES, and if so which `@id` (the canonical IRI, stored as `nes_id` on NGM/Jawafdehi)?*

- **NES dedup (write path).** Before inserting a new entity (from sourcing /
  bulk ingest), resolve the candidate against the corpus. A confident match
  means "merge / attach a new source to the existing entity" rather than
  creating a duplicate. This is the **dedup core** — the same politician
  appears in many sources and across election cycles.
- **NGM `nes_id` population.** `CaseEntity` (`court_case_entities`, in
  `services/ngm/ngm_service/courts/models.py`) carries `name`, `address`, `side`, and a
  nullable `nes_id`. Resolution takes a court party `(name, address)` and fills
  `nes_id` with the canonical NES entity ID (or leaves it null / queues it).
- **Jawafdehi entity linking.** When a case references a person/org, Jawafdehi
  calls the same `/resolve` to attach the canonical `nes_id` so case entities,
  governance data, and the entity service all converge on one identifier.

All three reach the service over **REST** (peer-to-peer). NES owns the entity tables and the OpenSearch index; the resolution
service is logically part of the NES service boundary (it reads the entity
store and the index directly) and exposes resolution endpoints to the others.

---

## 2. Architecture: candidate generation (PR 91) + scoring layer

Entity resolution is a two-stage pipeline. PR 91 already gives us stage 1.

```
incoming record ─► [1 BLOCKING / CANDIDATE GEN]  (PR-91 OpenSearch search)
                       │  top-K ranked candidates
                       ▼
                   [2 FEATURE SCORING]  pairwise features, record × candidate
                       │  match score ∈ [0,1] per candidate
                       ▼
                   [3 DECISION]  thresholds → auto-accept / review / reject
                       │
                       ▼
              nes_id (link) | review-queue item | "create new"
```

### Stage 1 — candidate generation = reuse PR 91 verbatim

We do **not** scan 1M rows. We turn the incoming record into a
`SearchQuery` (`nes/search/models.py`) and run it through the existing
`SearchBackend.search()` (`nes/search/backend.py`), backed by
`OpenSearchBackend` (`nes/search/opensearch/backend.py`). That returns a
bounded, ranked `SearchResults` (hit `@id`/`nes_id`, `score`, `source`) —
exactly the blocking key we need. The corpus is indexed by
`EntityDocumentBuilder.build()` (`nes/search/document.py`), so candidate
generation already understands names, aliases, misspellings, identifiers,
tags, and descriptions.

Key reused fields from the document builder (`document.py`):
- `name_primary_en` / `name_primary_ne` — boosted ^10 (`FIELD_WEIGHTS` in
  `models.py`).
- `name_alias_en` / `name_alias_ne` — aliases, `misspelled_names`, and the
  last-name-only / first+last variants from `extract_name_variants`.
- `name_translit_roman` / `name_translit_devanagari` — index-time cross-script
  forms (see bilingual matching below).
- `identifiers` (keyword, lowercase-normalized) / `identifier_names`.
- Filter fields: `type`, `sub_type`, `entity_prefix`, `entity_prefix_path`,
  `attributes` (flat_object equality filters).

We block by issuing one or more `SearchQuery`s per incoming record:
1. A **name query** (`query=<incoming full name>`, `entity_type=person`)
   leaning on the multi_match + phrase + bool_prefix clauses in
   `build_query_body` (`opensearch/mapping.py`).
2. An **identifier query** when the record has an NEC id / reg-no / citizenship
   number — `build_query_body` already emits a high-boost
   `term {identifiers: <value>}` clause (boost 12).
3. Optional **filter narrowing** via `entity_prefix` / `attributes` (e.g.
   constituency) to shrink the candidate set.

Take the union of the top-K hits (K≈25–50) as candidates for stage 2. PR 91's
`track_total_hits` and `MAX_RESULT_WINDOW` guard rails keep this bounded.

### Stage 1 bilingual matching (the important part)

The crucial property is already in PR 91: a **Devanagari query matches a Roman
record (and vice versa)** because transliteration happens at *index time*, not
query time. In `EntityDocumentBuilder._collect_name` (`document.py`):
- For a Devanagari `name.ne.full`, `StaticTransliterator.to_roman()`
  (`translit.py`) produces a Roman form stored in `name_translit_roman`.
- For a Roman `name.en.full`, `to_devanagari()` produces an (approximate)
  Devanagari form stored in `name_translit_devanagari`.

So a record indexed only in Devanagari is reachable by a Roman query (it hits
`name_translit_roman`), and a Roman-only record is reachable by a Devanagari
query (it hits `name_translit_devanagari`). The transliterated fields are
*additive recall boosters* — the real `en`/`ne` names always win on score
(^10 vs ^4). This means an NGM court party written in Devanagari resolves
against a NES entity whose primary name is Romanized English, with no
query-time transliteration in the resolution service. (Optional
`IndicTransliterator` improves Devanagari→Roman quality where installed.)

### Stage 2 — feature scoring (the new layer)

For each (incoming record, candidate) pair we compute a feature vector and
combine it into a calibrated match score in `[0,1]`. Candidate generation gives
recall; scoring gives precision. Features are computed in the resolution
service against the rehydrated NES entity (or directly from `SearchHit.source`,
which is the indexed document).

### Stage 3 — decision

Map the score to a confidence band (Section 4) and emit the decision.

---

## 3. Match features

Computed per candidate. Each is normalized to `[0,1]`; weights are configurable
and calibrated on labeled pairs.

| Feature | Source fields | Notes |
|---|---|---|
| **Name similarity (per name-part, bilingual)** | `NameParts.full/given/middle/family` on `Name.en` and `Name.ne` (`core/models/base.py`) | Compare given/family separately, not just full. Compute in *both* scripts: incoming-Roman vs candidate-Roman (incl. `name_translit_roman`), incoming-Devanagari vs candidate-Devanagari. Use token-set + Jaro-Winkler / Levenshtein; take max across script pairings. |
| **Identifier exact match** | `ExternalIdentifier.value` / `identifiers` (NEC id, reg-no, citizenship) | Exact match is near-decisive: very high weight, can short-circuit to auto-accept. For persons, NEC `candidate_id` / symbol `nec_id` (`person.py`), and NGM `registration_number`. |
| **Date of birth** | `PersonDetails.birth_date` (partial: "2012", "2012-01") | Compare at the available precision; year-only still discriminates. Mismatch is a strong negative. |
| **Address / constituency** | `PersonDetails.address` / `citizenship_place` (`Address.location_id`), `Candidacy.constituency_id`, NGM `CaseEntity.address` | Same district/constituency boosts; conflicting districts penalize. Normalize free-text `address` against location entity IDs where possible. |
| **Type / prefix compatibility** | `Entity.type`, `entity_prefix`, `entity_prefix_path` | Person↔person only; an org never matches a person. Incompatible type → hard zero. Use as a stage-1 filter too. |
| **Election-cycle / candidacy overlap** | `ElectoralDetails.candidacies` (year, party, constituency) | Shared candidacy history strongly supports "same person across cycles". |

Scoring combination: a weighted logistic / gradient-boosted scorer over the
feature vector, falling back to a hand-tuned weighted sum at launch (no labels
yet). Identifier-exact is a **gate**: present-and-equal → auto-accept band;
present-and-conflicting → strong reject signal even if names match (different
people, same name).

---

## 4. Two modes

### ONLINE (single lookup)

"Given this NGM party `name`+`address`, what `nes_id`?" One record in, ranked
candidates + a decision out. Synchronous, low-latency. Used by NGM `nes_id`
population on extraction and by Jawafdehi entity linking in the UI/case flow.
One stage-1 search + stage-2 scoring of top-K. Target p95 < ~300 ms.

### BATCH (bulk sourcing dedup)

Resolve **N** candidate records against the corpus **and against each other**
(intra-batch dedup) — a bulk source dump often contains the same person twice.
Steps:
1. Stage-1 search each record (reuse PR-91 `index_bulk` to warm/refresh the
   index first if needed).
2. Stage-2 score each record's candidates.
3. **Intra-batch clustering**: also block the N records against one another
   (same blocking keys) and run transitive closure so duplicates *within* the
   incoming batch collapse to one new entity, not N.
4. Emit a per-record decision plus a cluster assignment.

Batch runs asynchronously (job + result document), tolerant of the eventual
consistency of the OpenSearch index.

---

## 5. REST API surface

OIDC/Zitadel-protected (service-to-service client-credentials). All requests/responses bilingual-aware.

### `POST /resolve` (online)

```jsonc
// request
{
  "record": {
    "type": "person",
    "name": { "en": {"full": "Hari Bahadur"}, "ne": {"full": "हरि बहादुर"} },
    "identifiers": [{"scheme": "other", "value": "NEC-12345"}],
    "birth_date": "1970",
    "address": "Dang",
    "constituency_id": "https://jawafdehi.org/entity/location/constituency/dang-3"
  },
  "options": { "max_candidates": 25, "min_score": 0.30 }
}
// response
{
  "decision": "auto_accept",            // auto_accept | review | create_new
  "matched_nes_id": "https://jawafdehi.org/entity/person/hari-bahadur-abc123",
  "candidates": [
    { "nes_id": "https://jawafdehi.org/entity/person/hari-bahadur-abc123",
      "score": 0.94, "band": "auto_accept",
      "candidate_gen_score": 41.2,      // raw PR-91 SearchHit.score
      "features": { "name": 0.97, "identifier": 1.0, "dob": 1.0,
                    "address": 0.8, "type": 1.0 } },
    { "nes_id": "https://jawafdehi.org/entity/person/...", "score": 0.41, "band": "review", "...": {} }
  ]
}
```

### `POST /resolve/batch` (bulk)

```jsonc
// request: array of records + intra-batch dedup flag
{ "records": [ /* ...N records... */ ], "dedup_within_batch": true,
  "options": { "max_candidates": 50 } }
// response: a job handle (async)
{ "job_id": "resolve-batch-2026...", "status": "running",
  "results_url": "/resolve/batch/resolve-batch-2026.../results" }
```

Batch results: per input index → `{decision, matched_nes_id?, cluster_id,
candidates[]}`. `cluster_id` groups intra-batch duplicates that should become a
single new entity.

### Confidence bands

- **`auto_accept`** — `score ≥ T_high` (default 0.90, or identifier-exact gate).
  Link automatically.
- **`review`** — `T_low ≤ score < T_high` (default 0.55–0.90). Queue for human
  review; do not write `nes_id` yet.
- **`create_new`** / reject — `score < T_low`. No existing match; create a new
  entity (NES) or leave `nes_id` null (NGM/Jawafdehi).

Thresholds are per-consumer configurable (NGM may want a higher bar for
auto-link than NES dedup).

---

## 6. Decision policy tie-in

- **NES dedup → `≥2-source` gate.** A confident match doesn't *publish*; it
  attaches the incoming source to the existing entity. The locked `≥2-source`
  rule still governs visibility: resolution decides *which* entity a
  source attaches to; the gate decides whether that entity is public. Auto-accept
  → attach source to matched entity; create_new → new (gated) entity.
- **NGM `nes_id` write-back.** `auto_accept` writes `CaseEntity.nes_id` (via the
  NGM service's own write path / REST — never a cross-service direct DB write,
  per Q1). `review`/`create_new` leave `nes_id` null and emit a review item;
  the indexed `nes_id` column stays a clean "resolved vs not" signal.
- **Jawafdehi linking.** Same contract: auto-link writes the canonical `nes_id`
  on the case entity; review queues it; below threshold leaves it unlinked.
- **Human review** resolves `review`-band items; an accept then triggers the
  same write-back as auto-accept and (optionally) feeds the labeled-pair set
  used to recalibrate the stage-2 scorer.

---

## 7. Hard cases

- **Same person across election cycles (the dedup core).** Names re-romanize
  inconsistently year to year, addresses change. Lean on identifier match (NEC
  ids persist), `ElectoralDetails.candidacies` overlap, and bilingual name
  similarity; do *not* over-penalize address drift across cycles.
- **Transliteration variants.** "Krishna" / "Krisna" / "कृष्ण": handled at two
  layers — PR-91 index-time translit fields for recall, plus stage-2 fuzzy
  per-part similarity for precision. `misspelled_names` (already indexed as
  aliases) and `extract_name_variants` cover common spellings.
- **Honorific / title noise.** "Dr.", "Mananiya", "श्री", suffixes. Use
  `NameParts.prefix`/`suffix` to strip honorifics before name-part comparison
  so a title never inflates or deflates similarity; compare given+family cores.
- **Married-name changes.** Family-name change with stable given name +
  father/spouse name + DOB + constituency. Weight given-name and non-name
  features higher when family names diverge; surface as `review` rather than
  auto-reject. `BIRTH_NAME`-kind names (`NameKind.BIRTH`) help anchor identity.
- **Common-name collisions.** Many "Ram Bahadur" in one district. Require a
  second corroborating feature (identifier / DOB / father's name) before
  `auto_accept`; otherwise route to `review`.

---

## 8. Scale & performance at ~1M

- **Candidate gen is O(query), not O(corpus).** Stage 1 is an OpenSearch
  lookup over the bounded result window — independent of corpus size. 1M
  entity documents is small for OpenSearch; the index is single-source-of-truth
  via `EntityDocumentBuilder` upserts keyed by `id` (`backend.index`).
- **Stage-2 cost is K per record** (K≈25–50 pairwise scorings). Online stays
  sub-second; batch parallelizes across records.
- **Bulk ingest** uses PR-91 `index_bulk` / `async_bulk`; resolution batches
  should `refresh` (`OpenSearchBackend.refresh`) before re-querying to avoid
  missing just-indexed siblings within the same batch.
- **Index tuning at 1M:** the PR-91 defaults are single-node friendly
  (`number_of_shards: 1`, `number_of_replicas: 0` in `mapping.py`); production
  raises shards/replicas. Install `analysis-icu` for proper Devanagari
  tokenization (the backend auto-falls-back to the standard analyzer without
  it).

### Failure modes

- **No candidate.** Stage 1 returns empty → `create_new` (NES) / `nes_id` null
  (NGM/Jawafdehi). Distinguish "searched, found nothing" from "search backend
  down": if `get_search_backend` returns `None` / `health()` is false, **do not
  auto-create** — fail the resolution and retry, so we never duplicate during an
  outage.
- **Ambiguous tie.** Two candidates within a small score delta of each other and
  both above `T_low` → force `review` regardless of `T_high`, never silently
  pick the top one.
- **Translit gaps.** Approximate Roman→Devanagari can miss; mitigated by
  bidirectional indexing and stage-2 fuzziness. Persistent misses become
  `misspelled_names` entries to improve future recall.
- **Stale `nes_id`.** If a matched entity is later merged/split, NGM/Jawafdehi
  `nes_id`s must be re-pointed; track via the entity merge events (NES owns).

---

## 9. Open questions

Tracked separately (do not block Phase 1). Resolution-specific:

- Does the resolution service live *inside* the NES service boundary (direct
  index/DB read) or as a separate microservice that calls NES over REST? Ties
  to Q1/Q2 (schema ownership) and Q3 (gateway vs peer-to-peer). *Lean: inside
  NES boundary, reading the shared index/store; exposes resolution REST to
  NGM/Jawafdehi.*
- Threshold calibration & labeled-pair sourcing — where do training labels come
  from before launch (bootstrap with hand-tuned weights)?
- Scorer model: hand-tuned weighted sum vs learned model, and where it runs.
- Identifier authority/normalization (NEC id vs reg-no vs citizenship formats)
  and which are decisive gates.
- Entity merge/split propagation contract for already-written `nes_id`s across
  NGM and Jawafdehi.
