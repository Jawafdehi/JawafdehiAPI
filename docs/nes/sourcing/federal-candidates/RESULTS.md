# FEDERAL HoR CANDIDATES (प्रतिनिधि सभा उम्मेदवार) — ECN FPTP — RESULTS

Real acquisition, 2026-06-29. The "all who ran" FEDERAL House-of-Representatives
candidate bucket: every First-Past-The-Post constituency candidate — **winners AND
losers** — from the federal HoR election. Read-only on all sources; the live DB was
NOT touched.

## The federal JSON-tree discovery (the blueprint)

The ward-chairs/local waves found the LOCAL tree at `/JSONFiles/Election2079/Local/`.
The FEDERAL trees are the documented siblings. The portal's CURRENT data layer is
`/Scripts/MapElectionResult.js` (loaded by `MapElectionResult2082.aspx` — the default
election is now **2082**), which constructs its JSON paths as:

| Path template | Content |
|---|---|
| `JSONFiles/Election<YEAR>/HOR/Lookup/constituencies.json` | `[{distId, consts}]` — 77 districts, `consts` = FPTP seats; **165 total** |
| `JSONFiles/Election<YEAR>/HOR/FPTP/HOR-<districtCd>-<fConst>.json` | per-constituency candidate rows |
| `JSONFiles/Election<YEAR>/PA/FPTP/PA-<dc>-<fc>-<pc>.json` | provincial-assembly FPTP (sibling, not in scope) |

In **2082** these are fetched through `/Handlers/SecureJson.ashx?file=…` with an
`X-CSRF-Token` header — that handler returns **403** to an external read. BUT the
**2079** generation of the *same* tree is still served as **plain static files** at the
identical paths (no handler, no CSRF), exactly like the LOCAL tree. Verified directly:

```
GET /JSONFiles/Election2079/HOR/Lookup/constituencies.json   -> 200
GET /JSONFiles/Election2079/HOR/FPTP/HOR-1-1.json            -> 200
GET /Handlers/SecureJson.ashx?file=.../constituencies.json   -> 403 (2082-style, CSRF)
```

GoN TLS chain is mis-built → `fetch_ecn_2079_hor.sh` uses `--insecure` for these
**public reads only**, politely spaced. **All 165 constituency files fetched, 0 misses.**

### Row schema (`HOR-<dc>-<fc>.json`)

`CandidateName` (Devanagari — **no English-name field**, unlike the LOCAL tree),
`Gender`, `Age`, `PartyID`/`SymbolID`/`SymbolName`, **`CandidateID`** (stable NEC
anchor), `DistrictCd`, `DistrictName`, `State`/`StateName`, **`SCConstID`**
(constituency #), `PoliticalPartyName`, `TotalVoteReceived`, `Rank`,
**`Remarks`** (`"Elected"` == WINNER; English here, vs Devanagari `"निर्वाचित"` in the
local tree), plus `DOB`/`FATHER_NAME`/`SPOUCE_NAME`/`QUALIFICATION`/`ADDRESS`/`CTZDIST`.
Because there is no English name, an English `name.en` + IRI slug is **transliterated**
deterministically from the Devanagari (`_translit`); the Devanagari `name.ne` remains
authoritative.

## Cycles harvested — 2079 done, 2074 deferred (honest verdict)

| Cycle | Verdict |
|---|---|
| **2079 (2022)** HoR FPTP | **COMPLETE** — 165/165 constituencies, 0 misses |
| **2074 (2017)** HoR | **NOT obtainable here.** `Election2074/HOR/…` is **404** at every path variant; **no Wayback snapshot** of any `Election2074` JSON exists. The 2017 results predate this static-JSON portal generation, so a machine-readable 2074 federal-candidate roll is not reachable. **Deferred — not fabricated.** |
| **PR / proportional members** | **Deferred.** The portal exposes only `HoRPartyTop5.txt` (party seat *totals*), never named PR members — same finding as the ward-chairs wave: PR is a party roster, not constituency candidates. |

## Candidates sourced (2079 HoR FPTP)

| Quantity | Count |
|---|---:|
| Constituency files scanned | **165** |
| Candidate rows (= published Person records) | **2,411** |
| **Winners (elected)** | **165** (one per constituency) |
| **Losers (defeated)** | **2,246** |
| Independents (स्वतन्त्र) | 867 |
| District `containedInPlace` linked | 2,411 / 2,411 (100%) |

### UPGRADE vs NEW (keying onto existing MPs)

Winners are the same people the **parliament-api** wave already ingested as MP entities,
so a winner UPSERTS a "candidate" role onto the existing `…/person/<name>-hor-<id>`
entity instead of duplicating. The bridge is the **collapsed Devanagari name** (reliable
across sources) with the constituency number as a consistency check — the English
district spelling is unreliable (locations "Chitawan"/"Tanahun"/"Nawalparasi East" vs
parliament "Chitwan"/"Tanahu"/…), so name-key is matched first by seat, then globally.

| | Count |
|---|---:|
| **UPGRADE** (winner = existing 6th-HoR FPTP MP) | **144** |
| **NEW winners** (no MP record in the prior roster) | **21** |
| NEW losers (every defeated candidate) | 2,246 |
| **NEW total** | **2,267** |
| **UPGRADE total** | **144** |

All 144 UPGRADE `@id`s resolve to real parliament-api MP IRIs (0 invented). The **21 NEW
winners** are genuine gaps in the parliament-api 6th-HoR roster (which holds only 145 of
165 FPTP seats) — they include Pushpa Kamal Dahal "Prachanda" (Gorkha 2), Ram Chandra
Paudel (Tanahu 1), Mahanta Thakur (Mahottari 3), Sobita Gautam (Kathmandu 2), Subas
Chandra Nembang (Ilam 2). These are NOT duplicates: an exact + fuzzy Devanagari name
search across the entire 6th-HoR FPTP set confirms each is absent, so they are minted as
new Person entities (a future parliament-roster refresh can merge on the shared name).

## Party join

`PoliticalPartyName` (ne) → parties wave via the collapsed key folding (reused verbatim
from the ward-chairs wave) + the UML `(एमाले)` alias. **1,369 party-linked + 867
Independent; 175 unmatched** across ~16 distinct party names genuinely absent from the
parties wave (dominant: the Maoist Centre single-symbol variant
`…(एकल चुनाव चिन्ह)`, `संघीय लोकतान्त्रिक राष्ट्रिय मञ्च`, the ML splinter). Unmatched-party
candidates are still emitted in full, just without a `partyOrg` IRI.

## Record shape

`@type "Person"`; IRI for NEW = `…/entity/person/<romanized-name>-hor2079-<district-slug>-<n>`
(the brief's slug-from-name+constituency+cycle form — human-legible, not the raw
CandidateID); UPGRADE reuses the existing MP `@id`. Bilingual `name`; `hasOccupation`
Role `roleName "House of Representatives candidate (2079, <Constituency>)"` with
`jawafdehi:electionResult "elected"/"defeated"`, `jawafdehi:party` (+ `jawafdehi:partyOrg`
IRI) / `"Independent"`, `jawafdehi:constituency "<District> <n>"`,
`jawafdehi:electionCycle "2079"`, `jawafdehi:containedInPlace` → district IRI;
top-level `containedInPlace` → district IRI; `jawafdehi:branch "legislative-candidate"`;
`identifier` `ecn-candidate-id`; plus `jawafdehi:gender`/`age`/`votesReceived`. UPGRADE
records carry `jawafdehi:upgradeOf "member-of-parliament (parliament-api wave)"`.

## Sourcing — election-authority (NOT held single-source)

Each record carries the **2 ECN artifacts** in `sources` (authority
`result.election.gov.np`): the per-constituency result JSON + the certified result-sheet
PDF (`/matpatra-pdf/2-<dc>-<const>.pdf`). `promote_held --election-authority` publishes
these as a single trusted election authority, so they are not flagged single-source-hold
(unlike the ward-chairs wave, which lacked an election-authority promotion path).

## Validation (platform validators)

Run from `/damodaha-volunteer/jawafdehi-platform`:
`TESTING=true DJANGO_SETTINGS_MODULE=monolith.config.settings uv run python` →
`validate_jsonld_entity` + `is_valid_entity_iri`.

| Check | Result |
|---|---|
| `validate_jsonld_entity` PASS | **2,411 / 2,411** |
| `is_valid_entity_iri` PASS | **2,411 / 2,411** |
| Duplicate IRIs | **0** |
| `sources` carry url/title/authority/kind | **all** |
| `sources.authority == result.election.gov.np` | **all** |
| UPGRADE `@id` resolves to a real parliament MP IRI | **144 / 144** |

## Files

- `fetch_ecn_2079_hor.sh` — polite read-only acquisition of the ECN 2079 HoR FPTP JSON
  (lookups + 165 per-constituency result files → `/tmp/ecn2079_hor`).
- `normalize_federal_candidates.py` — the builder (pure + offline given the snapshot).
- `federal_candidates_records.json` — `{"records":[...]}` (2,411 records), ready for `bulk_ingest`.

## Reproduce

```bash
bash fetch_ecn_2079_hor.sh /tmp/ecn2079_hor      # 165 polite reads, 0 misses
python3 normalize_federal_candidates.py          # -> federal_candidates_records.json
```
