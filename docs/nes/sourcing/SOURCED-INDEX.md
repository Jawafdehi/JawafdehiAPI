# NES Sourced-Entity Index

**What this is:** the authoritative inventory of what has ACTUALLY been sourced into
the live NES database — the real-state companion to [`sourcing-readiness-matrix.md`](./sourcing-readiness-matrix.md)
(which is the *plan*) and [`sourcing-plan.md`](./sourcing-plan.md). For how entities
are sourced, see [`sourcing-methodology.md`](./sourcing-methodology.md).

**Snapshot:** 2026-06-29 (post ECN-harvest), live `nes` Postgres DB (monolith at `:48000`).
**Totals:** **182,390 published entities** (of which **160,909 persons**, **20,644 organizations**,
837 locations) + **1,900 HELD** + NGM **2,122 materials**. Every published entity passed the
**≥2 independent publisher** gate (see the election-authority exception below); all are
bilingual-searchable (OpenSearch) and IRI-linked.

> **ECN HARVEST (2026-06-29) — the six-figure inflection.** The Election Commission
> serves complete, bilingual, born-digital **static JSON** at
> `result.election.gov.np/JSONFiles/Election2079/{Local,HOR,PA}/` (no OCR). Harvested +
> published under the election-authority exception: **152,960 local candidates** (2022,
> all posts, winners+losers), **6,685 ward chairs**, **1,497 local heads** (mayors/
> deputies, 753 units), **3,225 provincial-assembly candidates** (2079), **2,411 federal
> HoR candidates** (2079). Winners upsert candidate-roles onto existing elected entities
> (keyed on ECN CandidateID; 0 dup IRIs). DEFERRED: 2074 cycle (predates the static
> portal — old dynamic ASMX, 404 today, no Wayback JSON); PR/proportional members
> (portal exposes only party seat totals, never named PR members).

> IRI scheme: `https://jawafdehi.org/entity/<prefix>/<slug>`. Counts below are by
> prefix (the live grouping). "HELD" = sourced + validated but withheld from publish
> pending a 2nd independent source — recoverable, not discarded.

> **Deep-enrichment waves (2026-06-29) added since the 22.5k snapshot:** full monarch
> lineage (38, to 1382), cabinet ministers across 6 governments (123), full former-SC-justices
> roster (74), affiliated colleges (135), full Press Council newspaper register (897, OCR'd),
> metro/sub-metro mayors (33), and **6,685 ward chairpersons** (2022 local election) — the
> last drove persons from 1,264 → 7,982. Consolidation commands (`consolidate_roles`,
> `merge_persons`, `promote_held`) merged cross-wave roles + split identities + promoted HELD.

> **ELECTION-AUTHORITY EXCEPTION (policy, 2026-06-28).** Elected officials sourced from
> the **Election Commission of Nepal** publish on ECN alone: ECN is the constitutional
> authority of record, and each record carries two distinct ECN artifacts (the live
> result JSON + the certified result-sheet PDF). Applied via
> `manage.py promote_held --election-authority` (6,685 ward chairs flipped HELD→live).
> The strict ≥2-*independent*-publisher rule still governs all NON-elected buckets.

---

## 1. Inventory by category (published, live)

### Locations — 837
| Prefix | Count | Notes |
|---|---:|---|
| `location/province` | 7 | complete |
| `location/district` | 77 | complete |
| `location/localunit` | 753 | complete (all palikas; CBS code) |

Wards as *locations* not minted (only ward *offices*, below). Source: HDX/OCHA
P-codes + SurajMazar CBS codes/Devanagari.

### Government office tree — ~9,400 (the spine)
| Prefix | Count | Notes |
|---|---:|---|
| `government` (ministries) | 14 | ~22 ministries exist → **8 missing** (mocit, moics, motca, etc.) |
| `government/body` | 15 | constitutional bodies/commissions (CIAA, OAG, PSC, ECN, NHRC, AG, Supreme Court, …) |
| `government/department` | 24 | federal departments |
| `government/ciaa` | 8 | CIAA branch offices |
| `government/attorney/high_court` | 18 | high-court govt attorney offices |
| `government/attorney/district` | 77 | district govt attorney offices |
| `government/district/{dao,dcc,dfo,district_court,dpo}` | 77 each (385) | 5 core district line-office types |
| `government/treasury` | 80 | DTCOs |
| `government/revenue/iro` | 48 | Inland Revenue + LTO/MLTO |
| `government/revenue/customs` | 40 | customs offices + checkpoints |
| `government/field/survey` | 132 | Survey Offices (multi-per-district) |
| `government/field/post` | 76 | district post offices |
| `government/field/road` | 36 | division road offices |
| `government/police` | 9 | Nepal Police HQ + APF + NID + 7 provincial police (+ DPO ×77 above) |
| `government/provincial/<province>` | 97 | 7 provinces' OCMCM + ministries + commissions |
| `government/localunit` | 753 | local-unit executive offices |
| `government/ward` | 6,742 | ward offices |
| `government/soe` | 14 | state-owned enterprises (NEA, NTC, NOC, NAC, KUKL, …) |

### Judiciary — 26 (+ 77 district courts under `government/district`)
`judiciary` 3 (Supreme Court + apex) · `judiciary/high_court` 18 (7 HCs + benches) ·
`judiciary/tribunal` 5 (Special/Administrative/Revenue/Labour/Foreign-Employment).

### Other organizations — ~11,900
| Prefix | Count | Notes |
|---|---:|---|
| `hospital` | 11,399 | NHFR health facilities (gov + non-gov) |
| `political_party` | 151 | ECN-registered |
| `diplomatic` | 73 | foreign missions in Nepal + Nepal's missions abroad |
| `education/university` | 31 | all UGC universities + health academies |
| `education/campus` | 64 | TU constituent campuses |
| `bfi/{commercial,development,finance,microfinance,insurance}` | 100 | NRB banks (A/B/C/D) + NIA insurers |
| `media/{newspaper,broadcaster,online,agency}` | 23 | major outlets |
| `professional/{council,chamber,union}` | 12 | regulators + FNCCI/CNI + union federations |
| `ngo/ingo` | 42 | INGOs (SWC iPact) |
| `company/listed` | 9 | NEPSE-listed (non-BFI) |
| `contractor` | 1 | + 19 more HELD (PPMO blacklist) |
| `industry` | 2 | Shivam Cement, Chaudhary Group |

### Persons — 1,224
Legislators (HoR 5th/6th/7th houses + National Assembly + 7 provincial assemblies),
executive (PMs full succession to 1846, current cabinet), monarchy (12 Shah kings),
judiciary (Chief Justice lineage + sitting SC justices + HC chief judges), civil-service
leadership (Chief Secretary lineage to 1955 + current secretaries), constitutional
(CIAA Chief Commissioner lineage to 1977). **Multi-term/multi-role people = ONE entity**
with a `hasOccupation` role list (consolidated 2026-06-28: 322 roles merged across 306
persons). HoR verified at exactly **275** for the current 7th house.

---

## 2. Data-completeness issues (known gaps in sourced data)

> **NOTE (2026-06-30):** items 1–2 below were computed on the **pre-ECN 22,562-entity
> snapshot** and are NOT rescaled to the current 182,390 published. The 2026-06-29 ECN
> harvest added ~160k persons that are mostly English-only and slug-keyed, so live
> bilingual and person-Wikidata coverage are now substantially **lower** than the ratios
> shown. Treat 1–2 as directional (the gaps are real and grew); exact live percentages
> need a DB recount.

1. **Bilingual coverage (pre-ECN snapshot): ~46%** — **10,440 / 22,562** entities carried a
   Devanagari (`name.ne`). Biggest causes: the **11,399 hospitals** (NHFR `c_hf_name` empty
   in the snapshot — English-only), TU campuses, some office networks, and slug-only persons.
   *Fix:* re-fetch NHFR Devanagari from a Nepal egress; backfill office names from
   bilingual directories. This is the single largest data-quality gap (worsened by ECN).
2. **Person Wikidata coverage (pre-ECN snapshot): ~31%** — 382 / 1,224 persons carried a
   `wikidata-qid`. The rest are slug-keyed (mostly PR-list MPs, provincial-assembly members,
   historical civil servants). Limits cross-term/cross-wave dedup precision (see §4).
3. **~1,900 HELD entities** (single-source / same-publisher). Composition: ~318 NGOs (PDF-only
   registry), ~305 PR/NA MPs (parliament-secretariat-only, NOT ECN-sourced), ~194 historical
   persons (Wikidata-less leaders/secretaries/judges), ~45 schools (same-publisher sources),
   ~29 contractors (PPMO/bolpatra single-authority), plus a ~46-record org long-tail. All
   recoverable on a 2nd-source pair. **`promote_held` flips 0 of these with no new sourcing**
   — see [`HELD-PROMOTION-ANALYSIS.md`](./HELD-PROMOTION-ANALYSIS.md) for the per-bucket verdict
   and the ranked promotion work (the 930 figure was the archived-records count; ~970 more were
   staged in later ECN-era waves).
4. **Hospitals: ~590** use pre-2017 (Nawalparasi/Rukum undivided) districts → mapped
   best-effort east/west; sub-district placement approximate.
5. **8 ministries missing** (mocit / moics / motca + others) → 6 SOEs carry a
   `jawafdehi:parentMinistryUnresolved` soft-note instead of a resolved `parentOrganization`.
6. **No public office code anywhere in the govt tree** — the dedup key is the official
   `.gov.np` domain / office email / district anchor. The real LMBIS numeric office code
   is login-gated (IFMIS); a data-sharing request is the only unlock.
7. **Schools are a 45-row proof sample** (one palika), not the ~35k national corpus
   (IEMIS geo-fenced — see §3).
8. **Tenure dates are AD-mostly** for historical persons (BS not added where the
   source table was AD); some province PR members lack Devanagari names.

---

## 3. Access-gated buckets (NOT a pipeline problem — need access, not engineering)

Normalization + schema + join are PROVEN for these (each has a validated proof-sample);
the blocker is source ACCESS. These are the bulk of the "1M" headroom.

| Bucket | Est. volume | Blocker | Unlock |
|---|---:|---|---|
| **Companies (OCR)** | ~300k+ | per-name lookup only, no bulk export; `application.ocr.gov.np` cert-fail | open-data/bulk request to OCR |
| **Cooperatives (COPOMIS)** | ~32k | login wall, no public directory | data-share request to Dept of Cooperatives |
| **Schools (IEMIS)** | ~35k | `emis.cehrd.gov.np` geo-fenced to Nepal; only aggregate Flash PDFs public | **Nepal-egress fetch** of IEMIS (also fixes school Devanagari) |
| **NGOs (SWC)** | ~26k | affiliation list only in per-FY PDFs (`NP-SWC-#####`); iPact API has only ~276 new sign-ups | parse all FY PDFs + a 2nd-source pairing (District NGO Federation/PAN) |
| **Federal projects (NPBMIS)** | tens of thousands | all `/api/` endpoints HTTP 401 | token/approved access from NPC |
| **Provincial/local projects** | 100k+ | per-province hosts found (`ppc`/`pppc`/`kppc`.*), Preeti-font PDFs | Preeti→Unicode remap + GIWMS harvest |
| **Election candidates (all cycles)** | 100k+ | ECN portal is per-cycle ASP.NET scrape, Devanagari-only, no stable IDs; both sources same-publisher | ECN candidate-list PDF OCR + dedup |
| **Listed companies (full NEPSE)** | ~532 | `nepalstock.com` API 401; only a bounded set captured | NEPSE list access |

**Common unlock:** a **Nepal-side egress / credentialed / data-sharing** path. Until
then these stay sampled-or-HELD. Engineering is done; access is the constraint.

---

## 4. Areas to explore next

**Consolidation (highest leverage, partly done):**
- ✅ Cross-wave role merge — DONE (`manage.py consolidate_roles`; 322 roles across 306 persons).
- ⏳ **HELD promotion** — pair a 2nd source to flip HELD→published. **`promote_held`'s
  VAT/PAN pairing does NOT work for contractors** (audit 2026-06-30): the PPMO blacklisted
  rows carry no VAT/PAN at all and the 10 bolpatra VATs are all distinct + same-publisher,
  so 0 pairs form. Realistic targets, all needing a fetch: **3 ministries** (stale Wikidata
  P856 alias — cheapest), the **10 bolpatra contractors** (`ocr.gov.np` VAT lookup — highest
  anti-corruption value), the **18 HELD INGOs** (own site/Wikidata), and **305 PR/NA MPs**
  (ECN PR-allocation + NA-results fetch). See [`HELD-PROMOTION-ANALYSIS.md`](./HELD-PROMOTION-ANALYSIS.md).
- ⏳ **Slug-only person dedup** — name-resolve cross-wave duplicates lacking a Q-id
  (e.g. Suman Raj Aryal in leaders + secretaries → 2 entities). Needs the
  entity-resolution service (Splink/OpenSearch candidate matching).

**Coverage extensions (cleanly sourceable, not yet done):**
- The 8 missing ministries (fixes the SOE parent links + future dept links).
- ~1,340 private affiliated colleges (UGC count; parse pattern established).
- Full historical SC-justice roster; Constituent Assemblies 2064/2070 (601 each —
  gazette-PDF, no stable IDs, the hard historical tier).
- Land Revenue / Health / Agriculture district office networks (HELD: no public
  directory — DoLMA data-share or a provincial-government wave).
- Sub-post-office long tail (thousands below the 76 district POs); community FM long tail.

**Enrichment:**
- Backfill Devanagari names (the §2.1 gap) — Nepal-egress NHFR + bilingual directories.
- Raise person Q-id coverage to sharpen cross-term dedup.
- Link persons↔offices↔parties↔cases by IRI to enable the accountability queries this
  is built for ("everyone who was an MP, then a minister, then sat on a constitutional body").

---

## 5. Provenance

Per-wave RESULTS.md run records live under `nes/sourcing/<bucket>/` in this docs tree.
The sourcing scripts, captured snapshots, and record JSON (the `normalize_*.py` /
`fetch_*.sh` / `*_records.json` siblings) are kept with the sourcing working set outside
this repo. Entity ingest is via `manage.py bulk_ingest` (entities); materials are
ingested through the material API plane (`POST /api/materials/`, or the
`/file` upload endpoint) — see `materials/sourcing/README.md`. Role consolidation
via `manage.py consolidate_roles`.
