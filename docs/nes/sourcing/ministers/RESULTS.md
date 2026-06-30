# Federal Cabinet Ministers — Councils of Ministers since 2017 (NES sourcing wave)

REAL ACQUISITION, 2026-06-28. PUBLIC officials, office data only (no personal/family
data). Follows the leaders / ciaa-leadership precedent exactly: `>=2 INDEPENDENT
sources -> reconcile against existing NES persons -> schema.org JSON-LD {"records":[...]}
-> validate -> ready for bulk_ingest`. **The live DB was NOT touched.**

## Scope

The federal **Council-of-Ministers rosters** of every government since the 2017
federal election, captured as a per-cabinet ROSTER (anyone who held a portfolio at
any point in a government's tenure, not a single snapshot):

| Cabinet (`jawafdehi:cabinet`) | Tenure | Roster source #1 |
|---|---|---|
| Second Oli cabinet 2018 | 2018-02-15 .. 2021-07-13 (NCP era) | Wikipedia *Second Oli cabinet* |
| Fifth Deuba cabinet 2021 | 2021-07-13 .. 2022-12-26 (5-party) | Wikipedia *Fifth Deuba cabinet* |
| Third Dahal cabinet 2022 | 2022-12-26 .. 2024-07-15 (churn coalition) | Wikipedia *Third Dahal cabinet* |
| Fourth Oli cabinet 2024 | 2024-07-15 .. 2025-09-09 (UML–NC) | Wikipedia *Fourth Oli cabinet* |
| Karki interim cabinet 2025 | 2025-09-12 .. 2026-03-27 (technocratic caretaker) | Wikipedia *Karki interim cabinet* |
| Balen Shah cabinet 2026 | 2026-03-27 .. incumbent (RSP) | Wikipedia *Balen Shah cabinet* |

This replaces the previous coverage of just **11 ministers** (the partial current
Balen cabinet captured by the `leaders` wave) with **123 distinct ministers across
six cabinets**, each carrying their portfolio(s) + tenure.

## Validation (live platform validators)

Validated with `nes_service.entities.validation.validate_jsonld_entity` +
`jawafdehi_shared.entities.ids.is_valid_entity_iri`:

- `validate_jsonld_entity`: **123 / 123 PASS**
- `is_valid_entity_iri`: **123 / 123 PASS**
- **No dedup misses** (no two records share a normalized name).
- `memberOf` ministry anchors: **17 / 17 distinct ministry @ids resolve** to the
  ALREADY-INGESTED `offices` wave (referenced, never re-emitted).

## Counts

| Metric | Value |
|---|---|
| **Total distinct persons** | **123** |
| Publishable (>=2 independent sources) | **90** |
| HELD (single roster source) | **33** |
| **UPGRADE** existing NES entity (role added) | **79** |
| **NEW** person minted | **44** |
| With Wikidata Q-id | 53 |
| memberOf resolved to an ingested ministry IRI | 106 |
| Multi-role persons (>=2 portfolios across cabinets) | 5 |

### Role-holders per cabinet

| Cabinet | Role-holders |
|---|---|
| Second Oli 2018 | 25 |
| Fourth Oli 2024 | 25 |
| Third Dahal 2022 | 24 |
| Fifth Deuba 2021 | 22 |
| Balen Shah 2026 | 17 |
| Karki interim 2025 | 15 |
| **Total slots** | **128** (across 123 distinct persons) |

## Reconciliation — UPGRADE vs NEW (the critical requirement)

Most ministers are ALSO MPs already in NES (the `hor-275` / `parliament-api` /
`leaders` waves). Before minting an `@id`, each roster entry is looked up in
`sources/nes_name_index.json` (a name -> existing-`@id`/Q-id index built from those
three waves, 1,016 names / 213 Q-ids). On a match we **REUSE the existing `@id`** so
`bulk_ingest` UPSERTs a "Minister of X" role onto the existing entity rather than
creating a duplicate — and we inherit that entity's Q-id + Devanagari name.

- **79 / 123 are UPGRADEs** of an existing NES person (e.g. `Upendra Yadav` ->
  `…/upendra-yadav-hor-3636`, `Padma Kumari Aryal` -> `…/padma-kumari-aryal-q73733621`,
  `Sobita Gautam` -> `…/sobita-gautam-q115406648`).
- **44 / 123 are NEW** — minted Q-id-keyed (`…/person/<slug>-q<id>`) when a Q-id is
  known (e.g. PMs/DPMs not in the MP roster), else slug-keyed (`…/person/<slug>-min-<slug>`).

**Spelling-variant reconciliation (the HoR-fix lesson):** the matcher falls back to a
no-space-collapsed name when the exact-normalized form misses, recovering 3 that
would otherwise have duplicated: **KP Sharma Oli** == `k-p-sharma-oli-hor-3400`,
**Barsha Man Pun** == `barshaman-pun-hor-rolpa-1`, **Ram Nath Adhikari** ==
`ramnath-adhikari-hor-3545`.

### Multi-role persons (one entity, roles across cabinets)

- **KP Sharma Oli** — PM (Second Oli 2018) + PM (Fourth Oli 2024) on `…/k-p-sharma-oli-hor-3400`.
- **Bishnu Prasad Paudel** — DPM/Finance (Third Dahal 2022) + DPM/Finance (Fourth Oli 2024).
- **Shakti Bahadur Basnet** — Forests/Environment (Second Oli) + Energy (Third Dahal).
- **Biraj Bhakta Shrestha** — Youth & Sports (Third Dahal) + Energy (Balen 2026).
- **Mahabir Pun** — Education (Karki interim) + Science/Tech/Innovation (Balen 2026).

(Other cross-cabinet repeats — e.g. Upendra Yadav, Pradeep Yadav, Sharat Singh
Bhandari — also collapse onto a single entity; where one cabinet's role is HELD and
another publishable, the publishable one promotes the record.)

## >=2-source vs HOLD (honest)

The `held` flag (and the `bulk_ingest` gate) keys on an **independent** second source:

- **Publishable (90):** a verified Wikidata human Q-id (source #2 = the WD item),
  OR the person is already a corroborated NES entity (the matched record already
  carries its own >=2 sources; this wave only ADDS a role), OR the per-cabinet
  research pass independently confirmed a 2nd source.
- **HELD (33):** single Wikipedia roster source, no Q-id, not already in NES. These
  are predominantly the **single-roster-source cohort** the per-cabinet research
  flagged — lower-profile full ministers/state ministers and the **technocratic
  Karki-interim appointees** (Rameshwar Khanal, Kul Man Ghising, Om Prakash Aryal,
  Anil Kumar Sinha, etc. — non-party experts with no MP record and no resolved WD
  human item this pass). No second source was fabricated; they are staged for a
  follow-up Wikidata-label resolution pass.

## memberOf — ministry anchoring

Each Role's `memberOf` points at the ingested ministry organization IRI
(`…/organization/government/<slug>`): `opmcm, mof, moha, mofa, mod, moljpa, mohp,
moest, mocit, moald, mopit, moewri, moics, mofaga, motca, moless, mowcsc` — all 17
resolve to the `offices` wave. **106 / 123** records carry at least one IRI-anchored
`memberOf`. Portfolios with no ingested ministry anchor (**Water Supply, Urban
Development, Forests & Environment, Land Management, Youth & Sports** — these are
sub-units folded into combined ministries in the current structure) carry the
ministry **name as text** on `memberOf` (no `@id`), per the brief.

## Sourcing & confidence caveats (recorded honestly)

1. **Post-2025 churn (verified against multiple sources, per the brief):** the Sept
   2025 Gen-Z upheaval -> Oli resigned 2025-09-09 (caretaker to 09-12) -> **Sushila
   Karki** technocratic interim PM 2025-09-12 -> 2026 election -> **Balen Shah** (RSP)
   PM 2026-03-27. The Karki interim roster is rolling (ministers added/resigned across
   Sep 2025–Jan 2026); dates reflect the per-portfolio appointment windows. opmcm.gov.np
   was NOT reachable (all council/minister paths 404 per the `leaders` wave finding),
   so the current cabinet rests on Wikipedia + the existing `leaders` NES corroboration.
2. **Date conflicts flagged in research, carried as best-available:** Upendra Yadav's
   Third-Dahal DPM dates (10 Mar vs 13 May 2024) and Barsha Man Pun / Padma Aryal
   Second-Oli end dates had source conflicts; the well-attested value was used.
3. **Q-id gap:** 70 of 123 lack a Q-id this pass. Many are recoverable via a
   Nepali-label Wikidata search (the natural follow-up) — but every UPGRADE (79)
   already inherits its existing NES corroboration regardless of Q-id.
4. **Balen cabinet 14-May-2026 restructuring:** several ministers' `tenureStart`
   reflects the current (merged-portfolio) definition; the person-level swearing-in
   was 2026-03-27. Both are correct depending on whether you track person or portfolio.

## Files

- `normalize_ministers.py` — six per-cabinet rosters; per-person upsert with
  Q-id/existing-`@id` reconciliation (exact + no-space name fallback); ministry-IRI
  vs text `memberOf`; multi-role merge. Pure + offline given `sources/`.
- `ministers_records.json` — `{"records":[...]}` (123 records), ready for `bulk_ingest`.
- `sources/nes_name_index.json` — the reconciliation index (hor-275 + parliament-api +
  leaders: 1,016 names, 213 Q-ids).
