# NES Sourcing Plan (revised)

**Status:** Draft for discussion · supersedes earlier estimates with verified research
**Goal:** Plan at **1,000,000 public entities** — reached by *expanding the entity
universe*, not by lowering the bar. Verified authoritative ceiling for the obvious
buckets is ~250k–450k (see `../../shared/research/nes-sourcing-feasibility.md`),
so 1M depends on the high-volume expansion + historical buckets below.

This plan feeds the next step: a **sourcing methodology** (the repeatable
per-bucket procedure for getting entities into NES). See §6.

---

## 1. Hard rules (locked)

- **Public entities only.** Include cooperatives, NGOs/INGOs, private contractors
  (public contracts), civil-service **leadership** (gazetted/secretary tier).
  Exclude low-level civil servants. Private individuals enter **only** via the NGM
  plaintiff/defendant carve-out (minimal PII, no family fields — mig-007).
- **≥2 verifiable sources per entity** (REAFFIRMED). A single official Nepal
  Government source is **not** sufficient alone. Source #1 (primary, ID-anchored) +
  source #2 (independent corroborator), both stored as attributions. Single-source
  entities are HELD, not inserted.
- **Acquisition via the shared pipeline** (`../../shared/source-acquisition-pipeline.md`):
  TLS-tolerant fetch of `*.gov.np`, normalize through likhit, LibreOffice for
  office docs, high-quality Devanagari OCR for scans.

## 2. Composition toward 1M

Tiered by how the volume is actually reached. Counts are verified [V], indicative
[I], or unresolved [U] per the research.

### Tier A — verified core (~110k–160k deduped persons + orgs)
| Bucket | Volume | Source #1 | Source #2 |
|---|---|---|---|
| Elected officials, all cycles (local 2074/2079, fed/prov, CA 2064/2070) | ~70k+ positions → deduped persons | ECN results | ECN candidate lists / gazette notifications |
| Cooperatives | ~32k | Dept of Cooperatives / COPOMIS | Economic Survey (MoF) / provincial registry |
| Active NGOs/INGOs | ~26k | Social Welfare Council | line-ministry affiliation / district admin records |
| Courts + judges | ~96 courts + judges | Supreme Court portal | judicial council / gazette appointments |
| BFIs, listed cos, SOEs | ~few thousand | NRB / SEBON / NEPSE | company registry (OCR) |
| Political parties | ~hundreds | ECN | party registration gazette |

### Tier B — expansion buckets (the volume that approaches 1M) [mostly U]
| Bucket | Volume | Source #1 | Source #2 | Risk |
|---|---|---|---|---|
| **Election CANDIDATES** (not just winners, all cycles) | candidate counts multiply winners several-fold | ECN candidate registry | ECN per-constituency result sheets | dedup across cycles; PDF-locked |
| **Budget-line projects** (federal NPBMIS + 7 provincial + 753 local × multiple FYs) | tens–hundreds of thousands | NPC Project Bank (NPBMIS) | Red Book / provincial & local budget books | weak stable cross-year ID; federal count unverified (4,391 was REFUTED) |
| **Community schools** | tens of thousands | CEHRD/IEMIS Flash Report | local-gov education section / approval lists | locked in PDFs, OCR-heavy |
| **Contractors** (public-works firms) | tens of thousands | PPMO / e-GP (bolpatra) | PPMO blacklist + company registry | bolpatra possibly migrated/closed 2081 |

### Tier C — historical & enrichment
| Bucket | Notes |
|---|---|
| **PMs + Kings of Nepal** | Full list of all Prime Ministers and Kings. |
| **Government leaders since BS 2008 (~1951 AD)** | Important officeholders since the fall of the Rana regime — ministers, chief justices, governors, party leaders, key civil-service heads across the modern era. |
| **Locations (enrichment)** | Already in NES (~7,745) but skeletal — enrich with codes, bilingual names, hierarchy, ward layer (6,743 wards not yet sourced). |
| **Hospitals** | Already in NES (mig-006 / NHFR) — baseline, may refresh. |

## 3. Reality check on 1M

- Tier A gives a solid, fully-verifiable **~110k–160k**.
- Tier C historical adds a few thousand high-value entities.
- **1M is only reached if Tier B lands at scale** — and Tier B is exactly where the
  two-source rule bites hardest (single-registry, PDF-only, weak IDs). So the
  honest plan is: **Tier B feasibility is gated on the acquisition pipeline (OCR
  quality) and on finding a real second source per bucket.** Where a credible
  source #2 cannot be found, that bucket is capped, not forced.
- Net: plan and build *toward* 1M, but treat the number as a stretch contingent on
  Tier B, with ~250k–450k as the defensible near-certain floor.

## 4. Tension to manage

"Plan at 1M" + "≥2 sources for everything" are in genuine tension: the volume
buckets are the least corroboratable. This is not resolved by wishing — it's
resolved per-bucket in the methodology (§6) by identifying the source pair *before*
committing to a bucket's volume contribution.

## 5. Pilot: current elected officials

First bucket through the full pipeline (switched from the office tree, which has no
public count or stable ID and is deferred). Why elected officials:
- Clean bilingual **person** bucket with stable **NEC IDs**.
- Exercises **cross-cycle dedup** — the core entity-resolution capability the whole
  program depends on (same person as candidate in 2074 and winner in 2079 = one
  entity).
- Has a real source pair (ECN results + ECN candidate lists).
- Locations already in NES to anchor `constituency_id` referential integrity.

Pilot proves: acquisition (TLS/likhit/OCR) → extraction → dedup/resolution (PR #91
OpenSearch) → two-source attribution → privacy gate → publication. Detailed pilot
spec + worked example: the ELECTED OFFICIALS worked example in
`sourcing-methodology.md` §"Worked example".

## 6. Next: the sourcing methodology

This plan defines *what* and *from where*. The immediate next deliverable is the
**methodology** — the repeatable procedure to actually source a bucket into NES:
1. Per-bucket source-pair identification + access assessment.
2. Acquisition (shared pipeline).
3. Extraction → canonical NES entity shape (per `entity_prefix`).
4. Entity resolution / dedup before insert.
5. Two-source attribution assembly + provenance.
6. Verification gate + stratified human audit.
7. Bulk ingest (Postgres + bulk path, per the storage proposal) + OpenSearch index.

We will build this methodology next and run the elected-officials pilot through it.
