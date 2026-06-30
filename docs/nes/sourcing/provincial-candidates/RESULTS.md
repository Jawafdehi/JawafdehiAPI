# PROVINCIAL ASSEMBLY (Pradesh Sabha) FPTP CANDIDATES — winners + losers — RESULTS

Real acquisition, 2026-06-29. Every first-past-the-post candidate (the WINNER and ALL
LOSERS) for every provincial-assembly constituency, harvested from the Election
Commission of Nepal (ECN) static results portal. Read-only on all sources; the live DB
was NOT touched (acquire + validate only; `bulk_ingest`/`promote_held` is the
orchestrator's job).

## Cycle coverage — 2079 (2022) ONLY; 2074 (2017) is NOT machine-readable (the verdict)

The brief asked for BOTH the 2074 (2017) and 2079 (2022) cycles. Only **2079 is
obtainable as machine-readable data**:

- **2079 (2022 AD provincial election): FULL static-JSON tree — acquired.** 330/330
  constituency files, 0 misses (below).
- **2074 (2017 AD): NOT available, not fabricated.** The 2017 federal+provincial results
  lived on the OLD jqGrid portal (pages `ElectionResultState.aspx` /
  `FinalCandidateState.aspx`), whose grid data was served by **dynamic ASMX/ASHX
  endpoints**, not static files. The current portal has **no `Election2074` tree**
  (every `/JSONFiles/Election2074/...` path 404s, all spellings probed). The Wayback
  Machine has a Jan-2018 snapshot of the portal homepage and lists the
  `ElectionResultState.aspx` link, but the `.aspx` snapshots are **0-byte shells** and
  **no data endpoint / no JSON** was ever captured (CDX for `Election2074*` and for any
  `.json/.asmx/.ashx` data file under the domain returns empty). So there is **no
  machine-readable named-candidate source** for the 2017 provincial election from ECN or
  Wayback. We do **not** synthesize 2074 rows. (A future pass could OCR ECN's certified
  2074 result-sheet PDFs or scrape Wikipedia's per-constituency 2017 tables, but that is
  a separate, lower-precision tier.)

## ECN PROVINCIAL access verdict (the headline) — the JSON path discovered

**A complete, machine-readable, nationwide PROVINCIAL FPTP candidate dataset (winners
AND losers) IS obtainable as plain static JSON.** Discovery method (mirrors the
ward-chairs wave):

- The portal's current SPA data layer is `/Scripts/MapElectionResult.js` (loaded by
  `MapElectionResult2082.aspx`, found from the homepage). It fetches results via
  `/Handlers/SecureJson.ashx?file=JSONFiles/Election<YEAR>/PA/FPTP/PA-<dc>-<fc>-<pc>.json`
  (CSRF-gated for the live 2082 view), but **the same files are served PLAINLY without
  CSRF** under the static path — exactly like the 2079 Local tree.

**The discovered provincial JSON path:**
```
https://result.election.gov.np/JSONFiles/Election2079/PA/FPTP/PA-<dc>-<fc>-<pc>.json
```
plus the HOR (federal) constituency-count lookup that drives enumeration:
```
https://result.election.gov.np/JSONFiles/Election2079/HOR/Lookup/constituencies.json   -> [{distId,consts}]
```

| Token | Meaning |
|---|---|
| `dc` | district code (1..78; the HOR lookup mislabels one Rukum half as a 2nd "77") |
| `fc` | HOR (federal) constituency within the district = `CenterConstID` |
| `pc` | PA (provincial) constituency within that HOR seat = `SCConstID`, **always 1 or 2** |

**Structure:** each of Nepal's 165 HOR (federal) FPTP constituencies contains exactly
**2** provincial FPTP constituencies → **165 × 2 = 330 PA FPTP constituencies**. One
`PA-<dc>-<fc>-<pc>.json` per constituency is the FULL candidate slate. Row fields:
`CandidateName` (**Devanagari ONLY — no English field**, unlike the Local tree),
`Gender`, `Age`, `PartyID`, `PoliticalPartyName`, `CandidateID` (stable ECN anchor),
`State` (1..7) + `StateName`, `DistrictCd` + `DistrictName`, `CenterConstID` (=fc),
`SCConstID` (=pc), `TotalVoteReceived`, `Rank`, `Remarks` (`"Elected"` for the single
winner; `NULL` for every loser).

GoN TLS chain is mis-built → `fetch_ecn_pa.sh` uses `--insecure` for these **public
reads only**. **All 330 PA constituency files fetched, 0 misses** (polite, 0.12s spaced).

## What was sourced

| Quantity | Count |
|---|---:|
| PA FPTP constituency files (= constituencies) | **330** (0 misses) |
| **Total candidate Person records** | **3,225** |
| WINNERS (`Remarks="Elected"`, 1/constituency) | **330** |
| LOSERS | **2,895** |
| Distinct `CandidateID` (no cross-file dupes) | 3,225 |

Winner count per province exactly matches the ECN FPTP seat structure (Koshi 56,
Madhesh 64, Bagmati 66, Gandaki 36, Lumbini 52, Karnali 24, Sudur Paschim 32 = **330**).

### Candidates per province (all / winners)

| Province | All candidates | Winners |
|---|---:|---:|
| Koshi | 510 | 56 |
| Madhesh | 1,007 | 64 |
| Bagmati | 668 | 66 |
| Gandaki | 235 | 36 |
| Lumbini | 496 | 52 |
| Karnali | 116 | 24 |
| Sudur Paschim | 193 | 32 |
| **Total** | **3,225** | **330** |

## UPGRADE vs NEW — winners matched to the sitting members

Winners are matched to the already-sourced **2nd provincial-assembly MEMBERS**
(the provincial-assemblies wave's `provincial_assembly_records.json`) by a
**DETERMINISTIC STRUCTURAL key**, not by transliteration:
`(province, normalized-district, HOR-const, PA-const 1|2)`. The member records carry a
`constituency` identifier like `"Jhapa 5(A)"` (district + HOR-const + A/B, where A=PA
const 1, B=2); the ECN row carries `DistrictName` + `CenterConstID` + `SCConstID`.
District names are bridged via `dnorm` (folds the romanization splits Chitwan/Chitawan,
Tanahun/Tanahu, Dhanusha/Dhanusa, Kapilvastu/Kapilbastu, Nawalparasi East/West ↔
Nawalpur/Parasi, Eastern/Western Rukum ↔ Rukum East/West) and the province key folds
Sudur Paschim ↔ Sudurpashchim.

| | Count |
|---|---:|
| **UPGRADE** (winner matched to an existing PA member → reuse its @id + curated EN/NE name, UPSERT a candidate role) | **328 / 330** |
| **NEW** (unmatched winners + all losers) | **2,897** |
| └ of which: NEW *winners* | **2** |

**The 2 NEW winners are honest, not errors:** Madhesh **Siraha 2(A)** and **Bara 1(B)**
have no member row in the provincial-assemblies wave (that wave documented a Madhesh
−1/−2 transitional shortfall). Their member roster simply never captured those two seats,
so the ECN winner is emitted as a NEW person. **All other 328 winners UPSERT onto the
existing member** (reusing the member's Wikidata-anchored @id where present, e.g.
`person/til-kumar-menyangbo-limbu-q7802013`, plus the member's curated English name and
official Devanagari), so a re-ingest does not duplicate the person.

## Province link rate

**3,225 / 3,225 (100%)** — every record's `containedInPlace` (and the role's
`containedInPlace`) resolves to one of the 7 ingested province location IRIs
(`…/location/province/<slug>-np0X`), and carries `jawafdehi:province`.

## Name romanization (the candid part)

The ECN PA JSON has **no English name field** (Devanagari only). For the **328 UPGRADE
winners** we use the provincial-assembly member's **curated** English name + official
Devanagari. For **NEW** records (the 2 unmatched winners + all 2,895 losers) we
**transliterate** Devanagari → Latin with `indic_transliteration` (IAST) plus a
Nepali-tuned cleanup (ṅ→ng, ś/ṣ→sh, व→w, …) and word-final inherent-schwa deletion (the
one reliable Nepali rule — e.g. "राम बहादुर थापा" → "Ram Bahadur Thapa"). This is an
honest, deterministic romanization of the authoritative `ne` (which is **always**
preserved as `name.ne`), **not** a claim of a sourced English spelling. Rough edges
remain on a minority of names (e.g. व rendered "w" not "b"; a few retained/over-deleted
schwas); the Devanagari is canonical.

## Party join

`PoliticalPartyName` (Devanagari) → parties wave `name.ne` (collapsed-key folding +
the UML `(एमाले)` alias, **plus** stripping ECN's `(एकल चुनाव चिन्ह)` = "single election
symbol" suffix that it appends to many names).

| | Count |
|---|---:|
| party-linked (resolves to a `…/organization/political_party/…` IRI) | **2,028** |
| Independent (`स्वतन्त्र`) | **1,094** |
| party unmatched (party genuinely absent from the parties wave) | **103** |

The 103 unmatched are the same small set of fringe/unregistered parties the ward-chairs
wave flagged as missing from the parties wave (e.g. `संघीय लोकतान्त्रिक राष्ट्रिय मञ्च`,
`नेपाल कम्युनिष्ट पार्टी (मार्क्सवादी लेनिनवादी)`, `नेपाल सुशासन पार्टी`); their candidates
still carry the party **name** string (`identifier`/`jawafdehi:party`) without a party-org
IRI. No party was fabricated.

## Sourcing (election-authority) — `promote_held --election-authority` publishes these

Each record carries two ECN `sources`, both `authority = result.election.gov.np`:
1. **primary** — the per-constituency result JSON
   (`…/JSONFiles/Election2079/PA/FPTP/PA-<dc>-<fc>-<pc>.json`)
2. **corroborator** — the certified provincial result-sheet PDF
   (`…/matpatra-pdf/2-<dc>-<fc>-<pc>.pdf`, derived from the same dc-fc-pc keys)

This is the ECN election-authority source, so `promote_held --election-authority`
publishes them. (Note: like the ward-chairs wave, both artifacts are ECN — a single
publisher. This wave does not assert a second independent publisher; the orchestrator's
election-authority promotion path is the intended route, per the brief.)

## Validation (live NES validators)

Run from `/damodaha-volunteer/jawafdehi-platform`:
`TESTING=true DJANGO_SETTINGS_MODULE=monolith.config.settings uv run python` →
`nes_service.entities.validation.validate_jsonld_entity` +
`jawafdehi_shared.entities.ids.is_valid_entity_iri`.

| Check | Result |
|---|---|
| `validate_jsonld_entity` PASS | **3,225 / 3,225** |
| `is_valid_entity_iri` OK | **3,225 / 3,225** |
| unique `@id` | 3,225 (duplicates: 0) |
| `containedInPlace` → 1 of the 7 province loc IRIs | **3,225 / 3,225 (100%)** |

## Record shape (per brief)

`@type "Person"`; IRI = the matched member's `@id` for UPGRADE winners, else
`…/entity/person/<romanized-name>-pa2079-<province-slug>-<district>-<hor>-<pa>`;
bilingual `name {en, ne}`; `hasOccupation` Role `roleName "Provincial Assembly candidate
(<Province>, 2022)"` with `jawafdehi:electionResult` (`won`|`lost`), `jawafdehi:party`
(+ `jawafdehi:partyOrg` IRI), and the role's `containedInPlace` → province loc IRI;
top-level `containedInPlace` → province loc IRI; `jawafdehi:province`;
`jawafdehi:electionCycle "2022 provincial"`; `jawafdehi:branch "legislative-candidate"`;
`identifier` `ecn-candidate-id` (+ `constituency`, `party`); plus `jawafdehi:gender`,
`jawafdehi:votesReceived`, `jawafdehi:ecnPartyId`.

## Files

- `fetch_ecn_pa.sh` — polite, read-only acquisition of the ECN 2079 PA FPTP JSON
  (HOR constituency lookup + all 330 `PA-<dc>-<fc>-<pc>.json` → `/tmp/ecn_pa_2079`).
- `normalize_provincial_candidates.py` — pure/offline builder + structural winner→member
  join → `provincial_candidates_records.json`.
- `provincial_candidates_records.json` — `{"records":[...]}` (**3,225** records), ready
  for `bulk_ingest` + `promote_held --election-authority`.

## Reproduce

```bash
YEAR=2079 bash fetch_ecn_pa.sh /tmp/ecn_pa_2079        # 330 polite reads, 0 misses
# (run from /damodaha-volunteer/jawafdehi-platform for the validator env)
TESTING=true DJANGO_SETTINGS_MODULE=monolith.config.settings \
  uv run python normalize_provincial_candidates.py
```
