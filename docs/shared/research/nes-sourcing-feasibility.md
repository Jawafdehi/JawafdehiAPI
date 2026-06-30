# NES Sourcing Feasibility — Verified Findings (2026-06-27)

Source: deep-research workflow `wf_39262631-91e` (6 angles, 28 sources fetched,
101 claims extracted, 25 adversarially verified 3-vote, 23 confirmed / 2 refuted).
Confidence tiers below: **[V]** verified 3-0 · **[I]** indicative (single/secondary
source, not adversarially verified) · **[U]** unresolved (no public aggregate found).

## Headline

**1,000,000 public entities is NOT achievable** from authoritative, individually-
enumerable Government of Nepal records. The verified realistic total of distinct
public *persons + organizations + locations* lands in the **low-to-mid hundreds of
thousands**. Reaching 1M would require counting fine-grained, multiplying records —
every election *candidate* (not just winners) across all cycles, plus every
budget-line *project* across all levels and fiscal years — most of which are
unverified or hard to access.

## Per-bucket results

| Bucket | Count | Tier | Source / access | Stable ID? |
|---|---|---|---|---|
| **Admin geography** (7 prov + 77 dist + 753 local + 6,743 wards) | **~7,580** | V | CBS codes via Open Data Nepal (CSV); FCGO CFS confirms 753 | ✅ integer geo-codes (wards need separate sourcing) |
| **Elected local reps** (2074: 35,038 + 2079: 35,097) | **~70,135 positions** | V | ECN results (via Wikipedia) | ⚠️ positions, not deduped persons |
| **Federal MPs** (HoR 165 FPTP + 110 PR) | **275** | V | Constitution Art.84 | ✅ |
| **Provincial assembly members** | **550** | I | 330 FPTP + 220 PR | ✅ |
| **Courts/judicial bodies** (1 SC + 18 HC entries + 77 DC + tribunals) | **~96** | V | supremecourt.gov.np (bilingual, URL IDs) | ✅ numeric ID + slug |
| **Cooperatives** | **~32,325** (2026); peaked ~34,837 (FY19/20) | I | Dept of Cooperatives / COPOMIS (19,903 integrated) | likely reg-no |
| **NGOs/INGOs** | **~58,353 cumulative** / **~25,760 active** | I | Social Welfare Council (figure inflated/cumulative) | ⚠️ |
| **Community schools** | **UNRESOLVED** | U | CEHRD/IEMIS Flash Report — locked in PDFs, no aggregate page | PDF-only |
| **Health facilities** | **UNRESOLVED** | U | NHFR (mohp.gov.np) — official, per-facility unique ID; site had expired TLS at check | ✅ (registry assigns ID) |
| **Registered contractors** | **UNRESOLVED** | U | PPMO / bolpatra e-GP — PDF bulletins, no aggregate; possible 2081 migration/closure | ⚠️ |
| **Budget-line projects** | **tens of thousands** (Karnali prov alone ~11,262) | I | NPC Project Bank (NPBMIS, npbmis.npc.gov.np); federal 4,391 figure **REFUTED** | ⚠️ cross-year ID unconfirmed |
| **BFIs** (NRB-licensed) | **107** (20 class-A commercial) | I | NRB | ✅ |
| **Govt office tree** (ministries→dept→div→local offices) | **UNRESOLVED — biggest gap** | U | No public enumerated total in FCGO/LMBIS/MoFAGA/Wikipedia (18 ministries listed, no aggregate) | ❌ no public stable office ID found |
| Election **candidates** (all who ran, all cycles) | **NOT COUNTED** | U | ECN — would multiply winner counts substantially | — |
| SOEs / listed cos (SEBON/NEPSE) | not in verified set | U | — | — |
| Political parties | not in verified set | U | ECN | — |

## Critical flags

- **The PILOT bucket (govt office tree) is the single biggest sourcing gap.** No
  authoritative source publishes an enumerated office count or a stable office ID.
  It must be **reconstructed** from FCGO/LMBIS/Red Book/MoFAGA primary data — a
  data-extraction project, not a download. This directly affects the pilot plan.
- **Refuted:** federal Project Bank total of 4,391 (0-3) and "Project Bank
  maintained by MoF" (0-3, it's NPC). Federal project volume is unconfirmed.
- **No aggregator shortcut:** Open Data Nepal has only ~630 datasets / 48
  publishers — good for CBS geo-codes, useless as a source-of-truth registry.
- **Access reality:** most authoritative registries are **PDF/index-page only**,
  no API, no published aggregate count (schools, contractors, office tree). The
  ones with clean machine access: CBS geo-codes (CSV), courts (URL IDs), Open Data
  Nepal CKAN API. NHFR has per-record IDs but had a TLS issue at check.

## Two-official-source rule — impact

Under the hard "≥2 official sources" gate, several buckets get harder: schools,
contractors, and the office tree barely have *one* clean machine-readable source,
let alone two. Buckets with a viable source-pair today: geography (CBS + FCGO),
elected officials (ECN results + ECN candidate lists), courts (SC portal + cause
lists), BFIs (NRB + SEBON/NEPSE). This rule further constrains the reachable total.

## Implication for the 1M target

Distinct **persons + orgs** realistically available and verifiable:
- elected officials (deduped) + appointed officials: ~80–120k
- cooperatives + active NGOs: ~50–60k
- schools + health facilities + contractors + offices: unresolved, plausibly
  another ~100–150k once extracted
- courts, BFIs, parties, SOEs: a few thousand

→ **Realistic ceiling ≈ 250k–450k distinct verifiable public entities.** The only
ways to "reach 1M" are by counting **(a) every election candidate across all
cycles** and **(b) every budget-line project across all 761 governments × multiple
fiscal years** — both are high-volume but have weak stable-ID / second-source
stories and stretch the definition of the entity set.

See open questions in the workflow result for the specific unresolved counts.
