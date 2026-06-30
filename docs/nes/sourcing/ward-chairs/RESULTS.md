# WARD CHAIRPERSONS (वडा अध्यक्ष) — 2022 local election — RESULTS

Real acquisition, 2026-06-28. The highest-volume *person* bucket in the NES program:
the elected head of each of Nepal's ~6,743 wards, from the 2022 (2079 BS) local-level
election. Read-only on all sources; the live DB was NOT touched.

## ECN ward-data access verdict (the headline)

**A complete, machine-readable, nationwide ward-results dataset IS obtainable — no
OCR, no PDF tier, no scrape-and-guess.** This overturns the earlier recon assumption
(`recon-elected-officials.md` §1/§5) that ECN ward data was a PDF/OCR
scrape with no clean source.

The Election Commission of Nepal results portal (`result.election.gov.np`) is a
Knockout/Leaflet single-page app, but its 2079-BS LOCAL-election data is served as
plain **static JSON** under `/JSONFiles/Election2079/Local/`. The endpoints were
discovered by reading `/Scripts/LocalElectionResult2079.js` (the SPA's data layer):

| File | Content |
|---|---|
| `Lookup/states.json` | 7 provinces `[{id,name}]` |
| `Lookup/districts.json` | 77 (+2 `NA` pseudo) districts `[{id,name,parentId=state}]` |
| `Lookup/localbodies.json` | **753** local bodies `[{id,name,parentId=district}]`, id `5001..5888` |
| `VdcPost.json` | posts per local body; **`postid "5"` == `वडा अध्यक्ष` (WARD CHAIRPERSON)**, with the per-body `wards` list |
| `<localbodyId>.json` | every candidate row: `CandidateID` (stable NEC anchor), `CandidateName`(Devanagari) + **`CandidateNameEng`**, `Gender`, `Age`, `PartyID`, `PoliticalPartyName`(+Eng), `TotalVoteReceived`, `Remarks "निर्वाचित"`/`RemarksEng "Elected"` (== WINNER flag), `PostId`, `Ward` |

Key facts:
- The portal supplies **both a Devanagari and an English candidate name** — no
  transliteration and no OCR were needed. `ocr_bedrock.py` was therefore **not used**:
  the per-ward winners are born-digital JSON, not scans. (Certified result-sheet PDFs
  do exist at `/matpatra-pdf/1-<bodyCD>-<postid>-<ward>.pdf` and are recorded as the
  second `sources` entry, but they were not OCR'd — the JSON already carries the data.)
- `PostId==5` "Elected" rows sum to a near-complete national ward-chair set.
- GoN TLS chain is mis-built; `fetch_ecn_2079_local.sh` uses `--insecure` for these
  **public reads only**. All 753 result files fetched, **0 misses** (polite, spaced).

## Ward chairs sourced vs 6,743

| Quantity | Count |
|---|---:|
| Ward-chair SEATS declared (sum of `postid 5` wards over 753 bodies) | **6,743** |
| Ward-chair WINNERS actually present in ECN result JSON (751 bodies) | **6,695** |
| Seats with NO "Elected" winner row in ECN data (postponed/incomplete polls) | **48** |
| **Published Person records** | **6,685** |
| Held: Dodhara Chandani Municipality (10 wards — no ward office to link, see below) | **10** |

**Coverage gap to 6,743 = 58**, fully explained, none fabricated:
- **48 seats have no ECN winner.** All 17 affected units are remote high-mountain
  gaunpalikas (Manang: Nar-Phu/Nason/Chame/Manang-Ngisyang; Mustang: Gharapjhong/
  Lo-Ghekar/Dolpo; Dolpa: Kaike 0/7, Charka Tangsong 0/6, She-Phoksundo, Dolpo Buddha;
  Humla: Namkha/Simkot/Kharpunath; Taplejung, Solu, Manang …) where 2022 polling was
  postponed or results never posted to the portal. We do NOT invent winners for them.
- **10 wards held (Dodhara Chandani Nagarpalika, Kanchanpur).** This unit is absent
  from the ingested local-unit / ward-office data — it is the known **6,742-vs-6,743
  off-by-one** the offices-local wave flagged. With no ward office to attach `memberOf`,
  its 10 ward chairs are held rather than emitted with a dangling link.

## ≥2-source rule — verdict: SINGLE-SOURCE (HOLD)

**Every one of the 6,685 records is `jawafdehi:sourcingStatus = "single-source-hold"`.**
This is the honest application of the methodology's ≥2-independent-publisher rule:

- ECN is a **single publisher**. The two `sources` entries each record carries (the
  per-body result JSON + the certified result-sheet PDF) are two ECN *artifacts*, not
  two independent publishers — the methodology explicitly warns that "the same registry
  on a different day is not a second source."
- An exhaustive search for an **independent** machine-readable source of *named* 2022
  ward-chair winners came up empty:
  - GitHub mirrors (`gauravyad69`, `nirmalrizal`) are **ECN re-hosts** (circular) and/or
    the wrong election (federal, not local).
  - **Open Data Nepal CKAN** has no 2022 local-election results (only 2017 district
    voter counts).
  - **Wikipedia** carries ward-chair data only as *party seat totals*, never named
    individuals; its independent named data stops at the **municipality (mayor)** tier
    (`List_of_mayors_of_municipalities_in_Nepal`, ~292 rows) — which cannot corroborate
    *ward* chairs.
- We therefore do **not** claim verification and do **not** force a fake second source.
  Records are written in full so a future independent corroborator (Nepal Gazette /
  MoFAGA local-government rosters / a news-outlet result mirror) can flip
  `single-source-hold → verified` without re-acquiring; the HOLD flag is explicit and
  machine-checkable (`jawafdehi:sourcingStatus` + `jawafdehi:sourcingNote`).

**Recommended next step for full coverage / verification:** harvest the MoFAGA
local-government rosters and gazette by-election notices as an independent per-unit
corroborator, and OCR ECN's certified result-sheet PDFs for the 17 mountain units to
recover any of the 48 missing winners that were later posted. Both are independent of
the ECN results JSON and would lift the bulk of these records out of HOLD.

## Validation (platform validators)

Run from `/damodaha-volunteer/jawafdehi-platform`:
`TESTING=true DJANGO_SETTINGS_MODULE=monolith.config.settings uv run python` →
`validate_jsonld_entity` + `is_valid_entity_iri`.

| Check | Result |
|---|---|
| `validate_jsonld_entity` PASS | **6,685 / 6,685** |
| `is_valid_entity_iri` PASS | **6,685 / 6,685** |
| Duplicate IRIs | **0** |
| `memberOf` → resolves to an ingested ward-office IRI | **6,684 / 6,684** (100%) |
| `containedInPlace` → resolves to an ingested local-unit IRI | **6,685 / 6,685** (100%) |

(One record — Janakpurdham Sub-Metropolitan ward 25 — has no `memberOf` because that
ward office is itself the single ward the offices-local wave was short by; the record is
still emitted with its local-unit `containedInPlace`.)

## Join to the ingested entities

ECN local-body id (`5001..`) **≠** CBS `municipality_code` (e.g. `10101`). The bridge is
**(district, fuzzy-Devanagari palika name)**, reusing the offices-local wave's `_ne_stem`:

1. **District id → district IRI**, resolved once (77/79; the 2 extras are ECN `NA`
   pseudo-rows): ne-stem exact → `difflib` (0.7) → Rukum East/West alias.
2. **Palika → local-unit**, per body, keyed by `(district IRI, palika ne-stem)`:
   - exact ne-stem within district,
   - else unique substring containment within district (handles "दोरम्बा शैलुङ्ग" vs
     "दोरम्बा", "भेरी" vs "भेरीमालिका", "ठोरी" vs "ठोरी(सुवर्णपुर)"),
   - else `difflib` (0.6) within district (handles ड/द, इ/ई, dropped matras, nasal
     variants),
   - else a globally-unique palika-stem fallback.
   Result: **752 / 753 local bodies joined** (only Dodhara Chandani unjoinable — not in
   the target data). Ward office for each chair = `…/organization/government/ward/
   <cbs_code>-<ward>` via `(cbs_code, ward)`; local-unit location = the matched IRI.

**Party join** (`PoliticalPartyName` ne → parties wave `name.ne`, collapsed key folding
ँ/ं, ष/स, व/ब, ि/ी, ई/इ, parens): **6,551 party-linked + 131 Independent (स्वतन्त्र);
3 unmatched** (`संघीय लोकतान्त्रिक राष्ट्रिय मञ्च` ×2, `नेपाल सुशासन पार्टी` ×1 — parties
genuinely absent from the parties wave). The UML abbreviation `(एमाले)` is aliased to the
full registered name.

## Record shape (per brief)

`@type "Person"`; IRI `…/entity/person/<romanized-name>-ward-<cbs_code>-<wardnum>`;
bilingual `name`; `hasOccupation` Role `roleName "Ward Chairperson, Ward <n> of <Unit>"`
with `memberOf` → ward-office IRI + `jawafdehi:party` (+ `jawafdehi:partyOrg` IRI);
`containedInPlace` → local-unit location IRI; `jawafdehi:wardNumber`;
`jawafdehi:electionCycle "2022 local"`; `jawafdehi:branch "local-government"`;
`identifier` `nec-candidate-id`; plus `jawafdehi:gender`, `jawafdehi:votesReceived`,
and the `jawafdehi:sourcingStatus`/`sourcingNote` HOLD flag.

## Files

- `fetch_ecn_2079_local.sh` — polite read-only acquisition of the ECN 2079 local JSON
  (lookups + VdcPost + 753 per-body result files → `/tmp/ecn2079`).
- `normalize_ward_chairs.py` — the builder (pure + offline given the snapshot).
- `ward_chairs_records.json` — `{"records":[...]}` (6,685 records), ready for `bulk_ingest`.

## Reproduce

```bash
bash fetch_ecn_2079_local.sh /tmp/ecn2079    # ~753 polite reads, 0 misses
python3 normalize_ward_chairs.py             # -> ward_chairs_records.json
```
