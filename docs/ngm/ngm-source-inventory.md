# NGM Lake — Source Inventory (what we pull in)

**Status:** CURRENT — authoritative source inventory + silver-table-family mapping.
The lakehouse and serving are in-process modules of `ngm_service`, see `../ARCHITECTURE.md`.
This doc answers: *which governance documents/datasets does the lake ingest, from
where, in what format, and what does each yield?* The "Yields" columns below map
each source to its **silver-table family** (`court_documents`, `charge_sheets`,
`legal_documents`, `official_reports`, …). The authoritative crosswalk from those
silver families to schema.org `@type` (e.g. `legal_documents`/`legal_corpus` →
`Legislation`, charge sheets → `DigitalDocument` + `jawafdehi:ChargeSheet`, court
orders → `Manuscript`, reports → `Report`, court case → `CreativeWork` +
`jawafdehi:CourtCase`) lives in live code at
`services/ngm/ngm_service/materials/jsonld.py` (the `MATERIAL_TYPES` map) — not
duplicated here.

Scope (locked 2026-06-27): courts + procurement/contracts + audits/asset-declarations
+ company/financial registries + ministerial decisions + already-scraped sources.
All acquisition runs through the shared pipeline (`../shared/source-acquisition-pipeline.md`):
TLS-tolerant `*.gov.np` fetch → likhit/OCR/LibreOffice normalization → bronze.

Legend — **Status:** ✅ scraped today · 🔶 partial/bolted-on · 🆕 new.
**Access:** API · HTML · PDF(born-digital) · SCAN(OCR needed) · DOC(LibreOffice).

---

## 1. Judicial — courts & tribunals — ✅→🆕

Court hierarchy: 77 District Courts → 7 High Courts → Supreme Court, plus the
**Special Court** and the **specialized tribunals** (§1b). All produce the same
shape of record (case listing, hearings, verdict doc), so one `court_*` silver
family covers them, typed by `court_identifier`.

### 1a. Courts (current core)
| Source / document | Owner | Access | Yields (silver tables) | Entities surfaced |
|---|---|---|---|---|
| Supreme / High / District / Special **court case listings** | Supreme Court of Nepal | HTML | `court_cases` | parties (plaintiff/defendant), courts |
| **Hearing / cause lists** | courts | HTML | `court_hearings` | judges, lawyers, benches |
| **Court orders / verdicts** (full-text docs) | courts | PDF/SCAN | `court_documents` (markdown) | judges, parties, cited laws |
| Court **case entities** (party detail pages) | courts | HTML | `court_entities` (`nes_id`) | persons, orgs |

Already in NGM's relational schema; becomes silver court table-family + the
court-orders document modality (likhit markdown).

### 1b. Specialized tribunals — 🆕
Quasi-judicial forums — distinct jurisdictions, all data via the Supreme Court CMS
or own sites. Land in the `court_*` family (`court_identifier=tribunal:…`).
| Tribunal | Jurisdiction | Feeds from | Access |
|---|---|---|---|
| **Revenue Tribunal** (राजस्व न्यायाधिकरण) | Appellate: tax/VAT/excise/income-tax/customs assessment & penalty disputes | IRD, Customs decisions | supremecourt.gov.np/rajashow/ (full-text decisions, cause lists) |
| **Administrative Court** (प्रशासकीय अदालत) | Civil-servant disciplinary/administrative appeals | govt personnel actions | full-text decisions, bulletins |
| **Debt Recovery Tribunal** (ऋण असुली न्यायाधिकरण) + Appellate | Bank/FI loan recovery (Debt Recovery Act 2058) | BFIs | drtribunal.gov.np / drat.gov.np (**fully bilingual**) |

Note: Revenue Tribunal is **appellate** for assessment disputes — distinct from the
DRI *criminal* revenue track (§2b), which goes to District/High Courts.

## 2. Prosecution, charge sheets & enforcement agencies — 🔶→🆕

The accusatory record: charge sheets (अभियोगपत्र / abhiyog patra; CIAA uses
आरोपपत्र / aaroppatra) filed by prosecutors across ALL courts, plus the agencies
that investigate and the money-laundering ecosystem. **Pull all charge-sheet types
across all courts, not just CIAA corruption.**

### 2a. The prosecution rule (data model)
Per **Constitution Art. 158(2)** the **Attorney General** makes the final decision
to prosecute Government cases — so most agencies *investigate* but **government
attorneys file** the charge sheet (District → High → Special Govt Attorney offices).
**Independent exceptions:** **CIAA** (constitutional, files corruption cases
directly) and **DRI** (statutory, files its own revenue cases). Charge sheet =
National Criminal Procedure Code 2074 §32, Schedule-20: accused identity +
fingerprints, offence particulars, evidence/witness lists, **statutory sections
applied**, and **माग दाबी (mag dabi)** = demanded punishment. Plaintiff in state
cases = **नेपाल सरकार** (सरकारवादी), "on the report of (जाहेरी)" a complainant.

### 2b. Charge-sheet types — all agencies × all courts
| Charge-sheet type | Law | Investigator | Files it | Court |
|---|---|---|---|---|
| **Corruption** (आरोपपत्र) | Prevention of Corruption Act 2059 | **CIAA** | CIAA (independent) | **Special Court** → appeal direct to Supreme Court |
| **Money laundering / TF** | ALPA 2064 | **DMLI** | Govt Attorney | **Special Court** (moved from District Court by 2083 Third Amendment Ordinance) |
| **Revenue / customs / tax / forex / hundi** | Revenue Leakage Act 2052; Customs Act 2064; Foreign Exchange Act | **DRI** / Customs | DRI (its own) | District / High Court |
| **Organized crime** | Organized Crime Prevention Act 2070 | Nepal Police (CIB) | Govt Attorney | **District Court** (NOT Special Court) |
| **Banking offence** | Banking Offence & Punishment Act 2064 | Police (NRB *refers*) | Govt Attorney | **gazette-designated District Court / Patan HC commercial bench** |
| **Narcotics** | Narcotic Drugs Control Act 2033 | Police NCB | Govt Attorney | District Court |
| **Human trafficking** | HT&T Control Act 2064 | Police (Anti-Trafficking) | Govt Attorney | District Court |
| **Ordinary criminal** | National Penal Code 2074 | Nepal Police | Govt Attorney | District → High → Supreme |

### 2c. Agency & document sources
| Source / document | Owner | Access | Yields (silver) | Entities |
|---|---|---|---|---|
| **CIAA charge sheets** (press-release PDF+DOC) | CIAA (ciaa.gov.np/pressreleaseCategory/charge) | PDF/DOC | `charge_sheets` (`type=corruption`) | accused officials, orgs, bigo amounts |
<!-- PHASE-3 INGESTION TASK (user directive 2026-06-27): bulk-download the FULL backlog —
     ALL CIAA press releases (all categories) + ALL CIAA annual reports + ALL abhiyog
     patras from https://ag.gov.np/abhiyog (all office tiers/districts/years). Lands in
     NGM bronze zone via the acquisition pipeline (TLS-tolerant fetch, likhit/OCR). ag.gov.np
     /abhiyog is JS-rendered → find the XHR/API endpoint or use headless. Recon was started
     then deferred with Phase 3. Do NOT mass-scrape until Phase 3. -->
| **CIAA annual reports / Akhtiyar Bulletin** | CIAA | PDF/SCAN | `official_reports` (§8) | case stats, named officials |
| **AG charge sheets / search** | Office of AG (**ag.gov.np**/abhiyog) | HTML/PDF | `charge_sheets` (all types) | defendants, charges, sections |
| **AG annual report / Abhiyojan journal / najir** | AG | PDF | `official_reports` (§8) | prosecution stats, precedents |
| **DMLI charge sheets / case digests** | DMLI (dmli.gov.np) | PDF (+SCAN press releases→OCR) | `charge_sheets` (`type=money_laundering`) | accused incl. PEPs, frozen assets, amounts |
| **DMLI annual reports** | DMLI (MoF) | PDF (born-digital) | `official_reports` (§8) | cases filed, conviction rate, biggo |
| **DRI charge sheets / press releases / auctions** | DRI (dri.gov.np) | HTML/PDF | `charge_sheets` (`type=revenue`) | evaders, firms, amounts |
| **Police / CIB / NCB press releases & seizure reports** | Nepal Police (cib/ncb.nepalpolice.gov.np) | HTML/PDF | `charge_sheets` (org crime, narcotics) | accused, seizures |
| **Kanun Patrika** (NKP, नेपाल कानून पत्रिका) | Supreme Court (nkp.gov.np) | **HTML (Unicode, searchable!)** + PDF | `gazette_entries` / precedents | precedent cases, judges, laws |

⚠️ Use **ag.gov.np** (attorneygeneral.gov.np is DEAD — expired cert + suspended
host). **NKP is the best machine-readable legal source** — selectable Devanagari
Unicode, advanced search, no OCR. DMLI press releases & the NRA are **scanned →
OCR**. specialcourt.gov.np is down → use supremecourt.gov.np/special/.

### 2d. AML / money-laundering ecosystem (linkage backbone)
Money laundering is the cross-cutting layer — **corruption is a predicate offence
(ALPA §4(i))**, so CIAA and DMLI cases both terminate at the **Special Court** and
the same person can appear in both. Join on **Special Court case number + accused
name + predicate-offence type**.

| Source / document | Owner | Access | Yields | Entities |
|---|---|---|---|---|
| **FIU STR/SAR/TTR stats, typologies, annual reports** | FIU-Nepal (inside NRB) | PDF | `official_reports` (§8) | suspicious-transaction stats, sectors |
| **AML/CFT supervisory directives & enforcement** | NRB MLPSD / SEBON | PDF/HTML | `enforcement_actions` | sanctioned BFIs, firms |
| **Targeted Financial Sanctions / designated persons** | Ministry of Home Affairs (+UNSC/FATF) | HTML/PDF | `sanctions_list` | sanctioned persons/entities |
| **National Risk Assessment (NRA)** | OPMCM / NRB | SCAN→OCR | `official_reports` (§8) | national ML/TF risk profile |
| **APG Mutual Evaluation + Follow-Up Reports** | APG (apgml.org) | PDF | `official_reports` (§8) | institutional findings naming DMLI/FIU/Special Court |
| **CIB loan-defaulter blacklist** | Credit Information Bureau (cibnepal.org.np) | downloadable | `defaulter_blacklist` | defaulting borrowers |

**Context:** Nepal re-added to FATF grey list 21 Feb 2025 (still on it mid-2026) —
the driver behind the 2083 ML ordinance routing ML cases to the Special Court.

## 3. Budgets, projects, procurement & contracts — 🆕 (highest corruption-relevance)

The full money trail: **budget → project → tender → contract/award → payment →
audit**. We pull every layer so a single rupee can be followed end-to-end across
the lake — from the budget line that funds it, to the project, to the firm paid.

### 3a. Budgets (fiscal data, all levels & fiscal years)
| Source / document | Owner | Access | Yields | Entities / refs |
|---|---|---|---|---|
| **Red Book** (federal budget / रातो किताब) | MoF | PDF/SCAN | `budget_lines` | budget heads, ministries, projects |
| **Federal Finance/Appropriation Acts** | MoF | PDF/SCAN | `appropriations` | budget heads |
| **Provincial budgets** (×7) | provincial MoF/MoEAP | PDF/SCAN | `budget_lines` (`level=provincial`) | provincial offices |
| **Local budgets** (×753) | local governments | PDF/SCAN | `budget_lines` (`level=local`) | local offices, wards |
| **Actual expenditure / releases** (TSA, SuTRA, LMBIS) | FCGO / 81 DTCOs | PDF/SCAN/HTML | `expenditures` | offices, budget heads → project link |

Budget *allocation* vs *actual expenditure* is itself a corruption signal (over/
under-spend, year-end dumping). Keyed by **budget sub-head code** + fiscal year.

### 3b. Projects (all types, all levels & fiscal years)
| Source / document | Owner | Access | Yields | Entities |
|---|---|---|---|---|
| **Federal Project Bank** (NPBMIS) | National Planning Commission | API/HTML | `projects` | projects, implementing offices |
| **Provincial project banks** (×7) | Provincial Planning Commissions | HTML/PDF | `projects` (`level=provincial`) | projects, offices |
| **Local project listings** (×753) | local governments | PDF/SCAN | `projects` (`level=local`) | projects, wards |
| **National Pride Projects** (राष्ट्रिय गौरव) | NPC / line ministries | HTML/PDF | `projects` (`subtype=national_pride`) | flagship projects |
| **Donor / foreign-aid projects** (AMP) | MoF (Aid Mgmt Platform) | HTML/PDF | `projects` (`subtype=donor_funded`) | projects, donors |
| **Constituency Development Fund** & conditional-grant projects | line ministries / MoFAGA | PDF/SCAN | `projects` (`subtype=cdf/grant`) | projects, MPs/offices |

All project subtypes land in one `projects` table family (typed by `subtype`),
mapping to NES `project/*` entity types. Karnali province alone ≈ 11,262 registered
projects → tens-to-hundreds of thousands across 761 governments × multiple fiscal
years. (Federal "4,391" figure was REFUTED — confirm at source.) ⚠️ stable
cross-year project ID is the key open problem (see §9); projects are linked to their
funding `budget_lines` (§3a) and downstream `procurement_*` (§3c).

### 3c. Procurement & contracts
| Source / document | Owner | Access | Yields | Entities |
|---|---|---|---|---|
| **e-GP tenders / bid notices** | PPMO / bolpatra | HTML/PDF | `procurement_tenders` | procuring offices, sectors |
| **Contract awards** | PPMO / line agencies | PDF/SCAN | `procurement_awards` | winning contractors, amounts |
| **Contracts** (signed agreements, variations) | line agencies / PPMO | PDF/SCAN | `contracts` | contractor, office, value, dates |
| **Debarment / blacklist** | PPMO | HTML | `contractor_blacklist` (✅ `ppmo_blacklist` exists) | debarred firms, proprietors |
| **Registered contractors** | PPMO / e-GP | PDF/SCAN | `contractors` | construction/supply firms |

⚠️ bolpatra may have migrated/closed (2081) — confirm live access; blacklist
already scraped (`BlacklistedFirm`). **Linkage is the payoff:** `budget_lines` ↔
`expenditures` ↔ `projects` ↔ `procurement_tenders` ↔ `contracts`/`procurement_awards`
↔ `contractors` ↔ `contractor_blacklist` ↔ `audit_findings` ↔ `court_cases`, keyed on
budget sub-head / project id / tender id / firm reg-no — so you can trace a budget
line to the project it funded, the firm paid, its debarment, and the resulting court
case across the whole corpus.

## 4. Audit & asset declarations — 🆕 (core accountability)

| Source / document | Owner | Access | Yields | Entities |
|---|---|---|---|---|
| **OAG audit reports** (महालेखापरीक्षक) | Office of the Auditor General | PDF/SCAN | `audit_findings` | audited offices, irregularity amounts |
| **Property / asset declarations** | CIAA / NVC / line ministries | PDF/SCAN | `asset_declarations` | officials + declared assets |
| **FCGO financial statements** (Red Book / CFS) | FCGO | PDF/SCAN | `fiscal_statements` | govt offices, budget heads |

Asset declarations are sensitive — public-official scope only; respect the NES
privacy boundary (no private-individual asset detail beyond the public-office holder).

## 5. Company & financial registries — 🆕 (org resolution / follow-the-money)

| Source / document | Owner | Access | Yields | Entities |
|---|---|---|---|---|
| **Company registry** | Office of Company Registrar (OCR) | PDF/SCAN | `company_registry` | companies, directors, reg-no |
| **BFI list / licences** | Nepal Rastra Bank | HTML/PDF | `financial_institutions` | banks, MFIs |
| **Listed companies / disclosures** | SEBON / NEPSE | HTML/PDF | `listed_companies` | issuers, board members |
| **Cooperatives** | Dept of Cooperatives / COPOMIS | PDF | `cooperatives` | co-ops, board |

Primary value: resolving *organization* entities and linking firms across
procurement, courts, and ownership.

## 6. Executive & policy record — 🆕

| Source / document | Owner | Access | Yields | Entities |
|---|---|---|---|---|
| **Council of Ministers decisions** (मन्त्रिपरिषद् निर्णय) | OPMCM | PDF/SCAN | `ministerial_decisions` | ministers, beneficiary orgs, appointments |
| **Nepal Gazette** (राजपत्र) — appointments, formations | Dept of Printing | PDF/SCAN | `gazette_notices` | appointees, new bodies |

Ministerial decisions + gazette appointments are high-value: they name *who* got
*what*, and feed both NES (officials) and case discovery. (Budget/Red Book line
items are covered under §3a as `budget_lines` linked to projects.)

## 7. Legal & legislative corpus — 🆕 (the legal backbone)

The authoritative text of the law itself — what cases cite, what charges are framed
under, and what officials are bound by. Mostly born-digital PDF from the official
law portal; a stable corpus (slow-changing) that every other source references.

| Source / document | Owner | Access | Yields | Entities / refs |
|---|---|---|---|---|
| **Constitution of Nepal** (2072) + amendments | Law Commission / MoLJPA | PDF | `legal_documents` (`type=constitution`) | articles, schedules |
| **Acts / laws** (ऐन) | Nepal Law Commission | PDF/HTML | `legal_documents` (`type=act`) | act name, sections |
| **Bills** (विधेयक) — pending & enacted | Federal Parliament Secretariat | PDF/SCAN | `legal_documents` (`type=bill`) | sponsoring ministry, status |
| **Ordinances** (अध्यादेश) | OPMCM / President's Office | PDF/SCAN | `legal_documents` (`type=ordinance`) | issuing authority, dates |
| **Rules / Regulations / Directives** (नियमावली/निर्देशिका) | line ministries | PDF/SCAN | `legal_documents` (`type=regulation`) | parent act, issuing body |
| **Provincial & local laws** | provincial assemblies / local govts | PDF/SCAN | `legal_documents` (`level=…`) | jurisdiction |
| **Treaties / international conventions** ratified | MoFA / Parliament | PDF | `legal_documents` (`type=treaty`) | parties |

All land in one `legal_documents` table family typed by `type` + `level`, with full
likhit-markdown text indexed for search. **Value:** court cases (§1) cite sections;
charge sheets (§2) frame charges under specific acts; this is the lookup target.
Versioning matters — amendments supersede; keep an `enacted_date` / `repealed_date`
and link amendment chains.

## 8. Official reports & publications — 🆕 (pull down systematically)

Recurring official reports across government — the analytical/statistical record.
Most are annual PDFs; this is a broad, ongoing harvest of the "reports" surface.

| Source / document | Owner | Access | Yields | Entities / refs |
|---|---|---|---|---|
| **OAG annual audit report** | Office of the Auditor General | PDF/SCAN | `official_reports` (→ `audit_findings` §4) | offices, irregularities |
| **CIAA annual report** | CIAA | PDF/SCAN | `official_reports` | cases, officials (also §2) |
| **NRB reports** (monetary policy, financial stability, bank supervision) | Nepal Rastra Bank | PDF | `official_reports` | BFIs, sectors |
| **Economic Survey** (आर्थिक सर्वेक्षण) | MoF | PDF | `official_reports` | sector statistics |
| **NSO / census & statistical reports** | National Statistics Office | PDF/HTML | `official_reports` | geography, demographics |
| **Commission reports** (NHRC, NVC, NPC, judicial/probe commissions) | respective commissions | PDF/SCAN | `official_reports` | named persons, findings |
| **Sectoral reports** (CEHRD Flash, DoHS, Dept of Roads, etc.) | line departments | PDF/SCAN | `official_reports` | offices, facilities, projects |
| **Parliamentary committee reports** | Federal Parliament | PDF/SCAN | `official_reports` | committees, subjects |

All land in one `official_reports` table family (publisher + report-type + period),
full text in markdown, indexed for search. **Value:** these are corroborating second
sources (critical for the NES ≥2-source rule), statistical anchors for verification
(known totals), and a rich entity-discovery surface (probe-commission reports in
particular name implicated officials). Cross-link to the specific finding tables
they back (audit, asset, etc.) rather than duplicating.

---

## 9. Prioritization

1. **Now (core):** judicial — courts & tribunals (§1) + prosecution/charge sheets &
   AML (§2) — CIAA/court data already flowing; broaden to all charge-sheet types
   across all courts + DMLI/AML; generalize into silver.
2. **Next (highest corruption-signal + biggest volume):** budgets, projects,
   procurement & contracts (§3) + audit/asset (§4) — the money trail where
   accountability cases live; PPMO blacklist already gives a head start, and
   projects are the largest single volume bucket (tens-to-hundreds of thousands).
3. **Then (resolution backbone):** company/financial registries (§5) — unlocks org
   `nes_id` resolution across the corpus, linking contractors↔companies↔owners.
4. **Foundational corpus (stable, do early & once):** legal & legislative corpus
   (§7) — slow-changing, born-digital, and the citation target for §1/§2.
5. **Ongoing harvest:** executive/policy record (§6) + official reports (§8) —
   appointments, decisions, and annual reports over time; reports also serve as
   corroborating second sources for the NES ≥2-source rule.

## 10. Cross-cutting requirements per source

Every onboarded source must define, before ingestion:
- **Natural key** (for idempotent upsert): case_number+court, budget sub-head+FY,
  project id, tender id, contract id, firm reg-no, decision date+number, audit report id, …
- **Access method + format** (drives acquisition path; flag SCAN→OCR, DOC→LibreOffice).
- **Entity extraction targets** (which persons/orgs to surface, with `nes_id` hooks).
- **Provenance** (source URL, fetch/TLS status, converter, OCR confidence, date).
- **Privacy classification** — public-office scope; private individuals only via the
  plaintiff/defendant carve-out, no private asset/PII beyond that.
- **Refresh cadence** (one-time backfill vs recurring scrape).

## 11. Open questions
- bolpatra/e-GP live status post-2081 migration — is tender/award data still reachable?
- Asset declarations: which are actually public vs FOI-only? Confirm legal basis before ingest.
- OCR quality bar for OAG/Kanun Patrika scans (dense Devanagari tables) — pilot needed.
- Company registry (OCR): bulk access or per-query only? Affects whether org
  resolution can be corpus-wide.
- Historical depth: how far back per source (ties to NES "since BS 2008" historical bucket)?
- Legal corpus: is there a machine-readable feed from the Nepal Law Commission, or
  PDF-only? How to model amendment/repeal chains and section-level citation anchors?
- Official reports: dedupe vs the finding tables they back (audit/asset) — store the
  report once and link, don't duplicate findings.
- Charge sheets: are full formal filings available, or only press-release summaries
  (CIAA) / search-metadata (AG /abhiyog)? CIAA DOC/PDF attachments are closest to full.
- AG /abhiyog: is the JS-rendered table summaries-only, and can it be paged/scraped
  across all office tiers + districts + years?
- ML join key: confirm Special Court case number reliably links CIAA ↔ DMLI ↔ NKP
  records for the same accused/predicate.
- No CSV/JSON/API anywhere in the AML stack — all tabular data embedded in PDFs
  (some scanned). Plan OCR + table extraction accordingly.
