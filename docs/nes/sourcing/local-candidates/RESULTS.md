# LOCAL-LEVEL ELECTION CANDIDATES — 2022 (2079 BS) — RESULTS

Real acquisition, 2026-06-29. The **highest-volume person bucket in the entire NES
program**: every candidate row — winners AND losers, every post — from Nepal's 2022
(2079 BS) local-level election, sourced from the Election Commission of Nepal (ECN)
static result JSON. Read-only on all sources; the live DB was NOT touched.

## Headcount

| Quantity | Count |
|---|---:|
| Total ECN candidate rows scanned (753 bodies × 12 posts) | **152,960** |
| Rows dropped (no Devanagari name / no/zero CandidateID) | **0** |
| Duplicate CandidateID rows collapsed | **0** (CandidateID is globally unique) |
| **Distinct persons / records emitted** | **152,960** |
| — **NEW** Person entities (own `…-<ecn-candidate-id>` IRI) | **146,275** |
| — **UPGRADE** of an already-ingested ELECTED official | **6,685** |
| **Winners** (Remarks ∈ {निर्वाचित Elected, निर्विरोध Unopposed}) | **40,192** |
| **Losers** (defeated) | **112,768** |

Each candidate row → exactly one record (one person = one CandidateID). The same
human standing for two posts would be two CandidateIDs in ECN's data (ECN keys per
candidacy), so 152,960 records == 152,960 distinct ECN candidacies.

### By post (PostId → ECN post)

| PostId | Post | Level | Records |
|---|---|---|---:|
| 1 | अध्यक्ष — Gaunpalika Chairperson | body | 3,092 |
| 2 | उपाध्यक्ष — Gaunpalika Vice-Chairperson | body | 2,163 |
| 3 | प्रमुख — Mayor | body | 3,238 |
| 4 | उपप्रमुख — Deputy Mayor | body | 1,964 |
| 5 | वडा अध्यक्ष — Ward Chairperson | ward | 32,495 |
| 6 | वडा सदस्य — Ward Member | ward | 53,177 |
| 7 | महिला सदस्य — Ward Woman Member | ward | 25,600 |
| 8 | दलित महिला सदस्य — Ward Dalit Woman Member | ward | 23,284 |
| 9 | महिला सदस्य — Gaunpalika Exec Woman Member | body | 2,691 |
| 10 | दलित/अल्पसंख्यक सदस्य — Gaunpalika Exec Dalit/Minority Member | body | 1,468 |
| 11 | महिला सदस्य — Nagarpalika Exec Woman Member | body | 2,318 |
| 12 | दलित/अल्पसंख्यक सदस्य — Nagarpalika Exec Dalit/Minority Member | body | 1,470 |

**Post taxonomy discovery:** `VdcPost.json` only enumerates PostIds 1–8. PostIds
**9–12 appear only in the per-body result rows** and are the body-level (ward=null)
**executive-committee members**: 9/10 occur exclusively in the 460 gaunpalikas, 11/12
exclusively in the 293 nagarpalikas/metros. They are mapped accordingly.

**Winner flag:** a row is a winner if `Remarks ∈ {निर्वाचित, निर्विरोध}` (Elected /
Unopposed) — `निर्विरोध` (uncontested) is also an election and is counted as elected.

## Dedup — do NOT duplicate already-ingested ELECTED officials

This is the load-bearing part of the brief. CandidateID (the stable, globally-unique
NEC key) drives idempotent dedup:

- We index `CandidateID → existing entity IRI` from the prior waves' records files:
  - **ward-chairs** wave (`ward_chairs_records.json`, 6,685 elected ward chairs, every
    record carrying a `nec-candidate-id`).
  - **local-heads** wave (mayor/deputy/chair winners) — read from
    `…/local-heads/*records*.json` **if present**. At this wave's run time that
    directory was **empty** (the local-heads wave had not yet written its records), so
    no head-winner CandidateIDs were available to dedup against — **the PostId 1–4
    winners here are therefore emitted as NEW.** When local-heads lands first, re-run
    this normalizer and those head winners will flip NEW→UPGRADE automatically (no code
    change). This is called out so the orchestrator can sequence/re-run if it wants
    head winners deduped rather than co-existing.
- For a row whose CandidateID **is** in the index, we do **not** mint a new
  `…-<candidate-id>` entity. We emit an **UPGRADE** record whose `@id` is the
  **existing** IRI, so `bulk_ingest` UPSERTS a candidate `Role` (electionResult /
  postType / electionCycle + the ECN sources) onto the already-live person. The record
  also carries `jawafdehi:upgradeOf`. **All 6,685 UPGRADE `@id`s were verified to equal
  the matching ward-chair IRI exactly (0 mismatches), and no UPGRADE IRI collides with
  any NEW IRI (0 duplicate IRIs across all 152,960 records).**
- **All 6,685 PostId-5 winners that were ingested by the ward-chairs wave matched here
  by CandidateID** — i.e. this wave fully recognizes and upgrades the prior elected
  ward chairs rather than re-minting them. (The 58 PostId-5 winners the ward-chairs
  wave could *not* link to a ward office — Dodhara Chandani + mountain units — were
  never ingested, so here they are correctly NEW.)

**Net new records this wave = the 112,768 losers + the ward members (PostId 6/7/8) +
body-level executive members (PostId 9–12) + the head/deputy candidacies + the 58
unlinked ward-chair winners**, exactly as the brief anticipated.

## Validation (platform validators)

Run with the platform on `sys.path`
(`services/nes` + `shared` from `/damodaha-volunteer/jawafdehi-platform`):
`nes_service.entities.validation.validate_jsonld_entity` + `is_valid_entity_iri`.
**Validated the FULL set (all 152,960 records, not a sample).**

| Check | Result |
|---|---|
| `validate_jsonld_entity` PASS | **152,960 / 152,960 (100%)** |
| `is_valid_entity_iri` PASS | **152,960 / 152,960 (100%)** |
| Duplicate IRIs | **0** |
| `memberOf` → resolves to an ingested ward/localunit office IRI | **152,645 / 152,645 (100%)** |
| `containedInPlace` → resolves to an ingested local-unit location IRI | **152,687 / 152,687 (100%)** |

## Joins & link rates

ECN local-body id (`5001..`) **≠** CBS code. Reuses the ward-chairs wave's proven
join ladder verbatim: district id→IRI (resolved once, ne-stem→difflib→Rukum alias),
then palika→local-unit per body keyed by `(district IRI, palika ne-stem)` with
substring/`difflib` fallbacks. **752 / 753 bodies joined.**

- **memberOf** (link rate **99.79%**, 152,645 / 152,960):
  - **WARD** posts (5–8) → ward-office IRI `…/government/ward/<cbs>-<ward>`.
  - **BODY** posts (1–4, 9–12) → localunit-office IRI `…/government/localunit/<slug>-<cbs>`.
  - 315 rows have no `memberOf`: **273** are all of Dodhara Chandani Nagarpalika
    (body 5741) — the single unit that does not join to any ingested local-unit (the
    known 6,742-vs-6,743 off-by-one the offices-local wave flagged); **42** are
    Janakpurdham Sub-Metropolitan (body 5825) ward 25, the single ward office the
    offices-local wave was short by. These records are still emitted (with their
    other links) rather than dropped or given dangling IRIs.
- **containedInPlace** (link rate **99.82%**, 152,687 / 152,960): only the 273
  Dodhara Chandani rows lack it (no local-unit location to point at).
- **Party** (`PoliticalPartyName` ne → parties wave, collapsed key + UML alias):
  **142,085 party-linked + 9,737 Independent (स्वतन्त्र); 1,138 unmatched** across
  **13 distinct minor/fringe parties genuinely absent from the parties wave** (top:
  संघीय लोकतान्त्रिक राष्ट्रिय मञ्च ×528, नेपाल सुशासन पार्टी ×256, नेपाल कम्युनिष्ट
  पार्टी (मार्क्सवादी लेनिनवादी) ×113, उन्‍नत लोकतन्त्र पार्टी ×64, …). Same registry
  gap the ward-chairs wave noted; unmatched rows keep the party name in `roleName`/
  source context but carry no `partyOrg` IRI. None fabricated.
- **English name synthesis:** 0 — ECN supplies `CandidateNameEng` for every emitted
  row (no transliteration / OCR needed).

## Record shape (per brief)

`@type "Person"`; IRI `…/entity/person/<romanized-name>-<ecn-candidate-id>` (NEW) or
the existing elected IRI (UPGRADE); bilingual `name`; `hasOccupation` Role
`roleName "<Post> candidate (2022 local, <Unit>[ ward N])"` with
`jawafdehi:electionResult` ("elected"/"defeated"), `jawafdehi:postType`,
`jawafdehi:electionCycle "2022 local"`, `memberOf` → office IRI, `jawafdehi:party`
(+ `jawafdehi:partyOrg` IRI), `jawafdehi:wardNumber` (ward posts);
`containedInPlace` → local-unit location IRI; `jawafdehi:branch "local-candidate"`;
`jawafdehi:postType`; `jawafdehi:electionCycle`; `identifier ecn-candidate-id`;
plus `jawafdehi:gender`, `jawafdehi:age`, `jawafdehi:votesReceived`; UPGRADE records
also carry `jawafdehi:upgradeOf`.

## Sourcing — ELECTION AUTHORITY (publishes, not held)

Each record carries the **two ECN artifacts** (both `authority =
result.election.gov.np`): the per-body result JSON (`primary`) + the certified
result-sheet PDF `/matpatra-pdf/1-<body>-<postid>-<ward>.pdf` (`corroborator`). ECN is
the sole, authoritative election authority for these results, so the orchestrator
publishes via **`promote_held --election-authority`** rather than holding them as
single-source. (For body-level posts with no ward, the PDF path uses ward `0`.)

## Files & shards

Records are **sharded by province** (381 MB total; a single file would be unwieldy) —
the orchestrator should ingest **each shard**:

| Shard file | Records |
|---|---:|
| `local_candidates_records_01-koshi.json` | 22,888 |
| `local_candidates_records_02-madhesh.json` | 42,586 |
| `local_candidates_records_03-bagmati.json` | 23,949 |
| `local_candidates_records_04-gandaki.json` | 12,892 |
| `local_candidates_records_05-lumbini.json` | 23,527 |
| `local_candidates_records_06-karnali.json` | 13,461 |
| `local_candidates_records_07-sudurpashchim.json` | 13,657 |
| **Total** | **152,960** |

- `normalize_local_candidates.py` — the builder (pure + offline given the
  `/tmp/ecn2079` snapshot). `--shard-by {province,none}` (default `province`).
- `RESULTS.md` — this file.

The ECN snapshot is the one fetched by the ward-chairs wave's
`fetch_ecn_2079_local.sh` (`/tmp/ecn2079`: lookups + VdcPost + 753 per-body result
files, 0 misses). No re-fetch was needed.

## Reproduce

```bash
# (snapshot already present at /tmp/ecn2079 from the ward-chairs wave)
python3 normalize_local_candidates.py            # -> 7 province shards
# validate (from /damodaha-volunteer/jawafdehi-platform with services/nes + shared on sys.path)
```
