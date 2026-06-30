# NES Sourcing Readiness Matrix (≥2-source rule)

**Status:** Consolidated, review-ready synthesis of all recon + feasibility findings.
**Date:** 2026-06-27 · **For:** bulk review of which buckets are GREEN/AMBER/RED under the hard
**≥2 verifiable, independent sources** gate (`sourcing-plan.md` §1).

> **This is the PLAN.** For what has ACTUALLY been sourced into the live NES DB
> (22.5k+ entities, with data-completeness gaps + access-blocked buckets + next
> areas to explore), see the companion **[`SOURCED-INDEX.md`](./SOURCED-INDEX.md)**.

Synthesized from the recon baselines,
`../../shared/research/nes-sourcing-feasibility.md`, and `sourcing-plan.md`. This is a
*decision matrix*, not a recap — it maps every bucket to its real-world sourcing readiness.

## RAG rule (locked)

- **GREEN** = two viable, accessible, sufficiently-independent sources **+** a stable ID. Ready to source.
- **AMBER** = one solid source; second uncertain / same-publisher / behind access friction (TLS, OCR, PDF-locked). Pilot-able but with caveats.
- **RED** = single-source-only (HELD under the rule) **or** gated/unreachable primary. Capped.

Independence note (from elected-officials recon): "the same registry on a different day is **not**
a second source." Same-publisher document classes count as *supporting*, not as a true 2nd source.

---

## Master matrix

| Bucket | NES entity type | Source #1 (primary, ID) | Source #2 (corroborator) | Access | Stable ID? | Bilingual? | TLS/liveness | ≥2 READY? | Est. volume | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| **Elected officials — current (federal/prov MPs)** | PERSON | ECN candidate-list / nomination PDFs (NEC candidate no.) | Nepal Gazette (rajpatra.dop.gov.np) | PDF/SCAN + HTML | ⚠️ NEC id in PDFs, not in results HTML | NE only (gen EN by translit) | result portal OK; election.gov.np cert-fail; rajpatra ECONNREFUSED | **AMBER** | ~825 (275 HoR + 550 prov) | ID anchor is in PDFs not the dashboard; gazette down at recon. Clean, small, high-value. |
| **Elected officials — local 2074/2079** | PERSON | ECN results (via portal/PDF) | MoFAGA local-gov rosters / gazette | HTML/PDF/SCAN | ⚠️ positions, name-based fallback | NE only | same as above; long-tail corroborator patchy | **AMBER→RED tail** | ~70,135 positions → deduped persons | Bulk of local winners have **no per-person independent 2nd source** → non-trivial HELD fraction. |
| **Candidates (all cycles, all who ran)** | PERSON | ECN candidate registry PDFs | ECN per-constituency result sheets | PDF/SCAN | ⚠️ name+constituency+party+cycle | NE only | scrape+OCR; portal flaky | **RED** | multiplies winners several-fold (100k+) | Both sources are **same publisher (ECN)** → not truly independent; PDF-locked + heavy cross-cycle dedup. The classic 1M-stretch bucket. |
| **Admin geography — prov/dist/local (753)** | LOCATION | CBS/NSO codes via Open Data Nepal CKAN (CSV) | FCGO CFS / boundary shapefile `.dbf` | API/CSV/SHP | ✅ integer geo-codes | Yes | CKAN API live | **GREEN** | ~837 (7+77+753) | Cleanest bucket: real CKAN API + integer codes. Source first. |
| **Admin geography — 6,743 wards** | LOCATION | shapefile `.dbf` + CBS gazette code tables | community mirror (unverified) JSON/SQL | SHP/PDF/SCAN | ⚠️ codes need extraction | partial | CKAN OK; gazette PDF | **AMBER** | 6,743 | No ready-made nationwide code-bearing ward CSV. Must assemble from `.dbf` + OCR. Confirmed GAP. |
| **Courts + judicial bodies** | INSTITUTION | Supreme Court portal (numeric id + slug) | Judicial Council / gazette appointments | HTML/PDF | ✅ URL ids | Yes | SC 302, **incomplete intermediate-CA chain** (supply CA) | **GREEN** | ~96 (1 SC + 18 HC + 77 DC + tribunals) | Small, stable, bilingual. Watch the TLS chain. Ready. |
| **Judges (persons)** | PERSON | SC portal / judgment metadata | Judicial Council / gazette appointment notices | HTML/PDF | ⚠️ name-based | Yes | as courts | **AMBER** | low thousands | Names from judgments + appointment gazette; no clean person-id. |
| **Charge sheets — CIAA** | DOCUMENT/EVENT | ciaa.gov.np press-release detail (id) + attachments | CIAA annual reports / court case no. | HTML+PDF/DOC | ✅ sequential PR id + case no. | Yes (BS+AD) | LIVE 200, **valid cert**, robots fully open | **GREEN** | ~3,000–3,500 releases + ~35 reports | Best-behaved gov source: born-digital text, valid TLS, crawlable. Filenames not guessable → parse pages. |
| **Charge sheets — AG/OAG** | DOCUMENT/EVENT | ag.gov.np `/search-abhiyogpatra` JSON (case no. + PDF) | court case no. / Supreme Court judgment | **JSON API** + PDF | ✅ court_case_no + file token | Yes | LIVE 200, valid chain in-env | **GREEN** | TBD (97 offices × 6 yrs; active to 2026) | Two-for-one: rich JSON metadata + full charge-sheet PDF. Never issue unfiltered query (times out). |
| **Charge sheets — Special Court / DMLI** | DOCUMENT/EVENT | specialcourt.gov.np | — | (down) | — | — | **specialcourt.gov.np HTTP 000 (DOWN)** | **RED** | unknown | No live source right now. Revisit before Phase 3. |
| **Legal corpus / precedents (NKP)** | DOCUMENT | nkp.gov.np `/full_detail/{id}` (selectable HTML) | SC judgment PDFs (supremecourt.gov.np) | HTML + PDF | ✅ sequential id + case no. | Yes | nkp LIVE 200; SC chain issue | **GREEN** | thousands (id-sequential) | Born-digital HTML, no OCR. Handle "हटाइएको/withdrawn" records. |
| **Cooperatives** | ORGANIZATION | Dept of Cooperatives / **COPOMIS** | MoLMCPA/MoF statistics PDFs / provincial registers | **login-gated** + PDF | ⚠️ reg-no, not public | Yes | COPOMIS 200 but **auth wall** | **RED** | ~32,325 (peaked ~34,837) | Primary is gated, no public directory. Pair stats PDFs + provincial registers, or data-sharing request. Do not bypass login. |
| **NGOs / INGOs** | ORGANIZATION | Social Welfare Council (`NP-SWC-#####`) | org-id.guide / line-ministry / district records | HTML | ✅ NP-SWC registered scheme | likely | **swc.org.np cert EXPIRED** (HTTP path works) | **AMBER** | ~25,760 active / ~58k cumulative | SWC not a complete NGO universe (local NGOs not required to register). 2nd source is a mirror, not GoN-independent. |
| **Schools (community)** | INSTITUTION | CEHRD/IEMIS Flash Report PDFs (aggregate) | local-gov education profiles / approval lists | PDF/SCAN; portal | ⚠️ IEMIS code not confirmed public | GoN norm | **iemis.cehrd.gov.np ECONNREFUSED**; old PDFs OK | **RED** | tens of thousands | Per-school rows locked behind down IEMIS portal; only aggregate PDFs public. Needs data request. |
| **Health facilities** | INSTITUTION | NHFR (nhfr.mohp.gov.np) — Master Inventory + API | DHIS2/HMIS facility lists / MoHP directories | Web app + **API** | ✅ unique facility code | GoN norm | **cert EXPIRED** (couldn't load app) | **AMBER** | UNRESOLVED (registry-scale) | Already partly in NES (mig-006). Has API + per-record id, but TLS blocked load. Confirm overlap + CSV/API export. |
| **Contractors (public-works firms)** | ORGANIZATION | bolpatra e-GP (IFB/contract refs) | PPMO blacklist + OCR company reg | HTML scrape + PDF | ⚠️ derived from contract refs | Yes | bolpatra **LIVE** (e-GP confirmed open, Q16) but **incomplete-CA chain**; downgrades to http /egp/ | **AMBER** | tens of thousands | A "registered contractors" list page not yet located; derive from contract-award rows. Blacklist already ingested. |
| **Procurement / contracts (awards)** | EVENT/DOCUMENT | bolpatra `loadContractRecordsListPublic` (~2,934 rows) | per-contract `ViewContractRecords` detail | HTML scrape | ✅ contract/IFB ref string (structured) | Yes | bolpatra LIVE; supply CA | **GREEN** | ~2,934 awards (FY 2076→2082) + tenders | Strong structured natural key; born-digital tables; highest corruption value. e-GP is live (2081 migration did NOT close it). |
| **Tenders / bid notices** | EVENT/DOCUMENT | bolpatra `searchOpportunity` (IFB no. + tenderId) | award records / PE e-contact | HTML POST + PDF | ✅ IFB no. + tenderId | Yes | as above | **AMBER** | large (scope by PE×FY) | Born-digital tables but notice PDFs may be scanned; pagination via POST. Pair with awards for 2nd source. |
| **Budget-line projects (federal NPBMIS)** | PROJECT | NPBMIS `/api/` (numeric project id) | Red Book / provincial-local budget books | **JSON API gated** | ⚠️ id behind 401 | Yes | **all endpoints HTTP 401 Unauthorized** | **RED** | tens of thousands (4,391 fed figure REFUTED) | Project list NOT freely queryable (401). Data exists/structured but gated. Weak cross-year id. Needs token/approval. |
| **Budget-line projects (provincial/local)** | PROJECT | provincial PPC project banks | local budget books | HTML/PDF | ⚠️ | unknown | **hostnames NXDOMAIN / not located** | **RED** | hundreds of thousands (Karnali alone ~11,262) | Canonical per-province hostnames unknown; not enumerated. The other big 1M-stretch bucket. |
| **BFIs (NRB-licensed banks)** | ORGANIZATION | NRB BFI list `/category/list-of-bfis/` | NEPSE/SEBON + OCR company reg | HTML + PDF | ✅ class + name (license in PDF) | Yes (sep EN/NP) | NRB valid 200 | **GREEN** | 107 (20 class-A) | Best-organized registry; cross-corroborates with NEPSE/SEBON/OCR. Small, ready. |
| **Listed companies (NEPSE/SEBON)** | ORGANIZATION | NEPSE (ticker + numeric id) | NRB (BFIs) / SEBON / OCR reg no. | HTML; 3rd-party CSV | ✅ ticker + numeric id | EN primary | NEPSE not directly tested; mirrors reachable | **AMBER** | ~532 securities | No confirmed official CSV/API; relies on 3rd-party mirrors (ListBase/GitHub). SEBON format unconfirmed. |
| **Company registry (OCR)** | ORGANIZATION | OCR `CompanyDetails.jsp` (company reg no.) | NRB / NEPSE / court+gazette notices | HTML per-company lookup | ✅ company reg no. (cleanest org key) | Yes | main OK; **`application.ocr.gov.np` cert-chain fails**; no bulk export | **AMBER** | registry-scale | Single-company lookup only, **no enumerable list / bulk export**. Per-name JSF scrape; can't range-walk. Strong as corroborator, weak as bulk primary. |
| **PMs + Kings of Nepal** | PERSON | Wikipedia / published histories | official records / Nepal Rajpatra | HTML/PDF | name-based | mixed | reachable | **GREEN** | ~hundreds | Well-documented historical figures; multiple independent published sources exist. |
| **Govt leaders since BS 2008 (~1951)** | PERSON | Nepal Gazette / official histories | Wikipedia / press archives | PDF/SCAN/HTML | name-based | mixed | rajpatra was down | **AMBER** | thousands | Ministers/CJs/governors/party heads; gazette access is the friction; multi-source for prominent figures. |
| **Ministerial decisions** | DOCUMENT/EVENT | Cabinet/ministry decision publications | Nepal Gazette | PDF/HTML | ⚠️ | NE | varies | **AMBER** | unknown | Not recon'd in depth; gazette-dependent. |
| **Official reports** | DOCUMENT | publisher portals (CIAA pubs, NPC, NRB, FCGO) | — (self-evidencing primary docs) | PDF | doc-level | mixed | per-publisher | **GREEN** | hundreds | Reports are primary artifacts (CIAA ~35 annual, guidelines, surveys) — born-digital mostly; corroborate via cross-citation. |
| **Govt office tree (ministries→dept→local offices)** | INSTITUTION | — (no public enumerated source) | — | — | ❌ no public office id | — | — | **RED** | UNRESOLVED — biggest gap | No authoritative source publishes a count or stable office id. Must be *reconstructed* from FCGO/LMBIS/Red Book/MoFAGA. Deferred from pilot. |
| **Political parties** | ORGANIZATION | ECN party registry | party-registration gazette | HTML/PDF | ⚠️ | NE | as ECN | **AMBER** | ~hundreds | Same-publisher-ish pairing (ECN + gazette); not deeply recon'd. |
| **SOEs** | ORGANIZATION | MoF/line-ministry SOE reports | OCR company reg | PDF/HTML | ⚠️ | mixed | not tested | **AMBER** | dozens | Not in verified set; corroborate via OCR + ministry reports. |

---

## GREEN — ready to source first (pilot/scale now)

These have two accessible sources **and** a stable ID today. Start here:

1. **Admin geography (prov/dist/753 local)** — CKAN API + integer codes. Trivial, cleanest entry.
2. **Courts + judicial bodies (~96)** — SC portal URL ids + gazette; small, bilingual (mind TLS CA).
3. **CIAA charge sheets (~3,000–3,500)** — valid TLS, robots-open, born-digital; PR id + case no.
4. **OAG/AG charge sheets** — JSON API gives metadata + PDF in one; court_case_no anchor.
5. **NKP legal corpus** — selectable HTML, sequential id, SC PDFs as 2nd source. No OCR.
6. **Procurement contract-awards (~2,934)** — structured IFB/contract ref key; highest corruption value.
7. **BFIs (107)** — NRB list + NEPSE/SEBON/OCR cross-corroboration; small, clean.
8. **PMs + Kings, official reports** — well-documented, multi-source historical/primary artifacts.

**Pilot recommendation:** geography (warm-up CKAN path) → courts (small bilingual person+org) →
CIAA/OAG (exercises the money-trail + document pipeline with valid 2-source pairs).

## RED — capped under the ≥2-source rule (will be HELD — the honest 1M gap)

These are single-source-only or gated, so they **cannot** clear the gate as-is. They are exactly
the high-volume buckets the 1M target depended on — confirming the feasibility doc's headline:

- **Election candidates (all cycles)** — both sources are ECN (not independent) + PDF-locked. 100k+ HELD.
- **Local elected officials (long tail)** — bulk of 35k×2 winners have no per-person independent 2nd source.
- **Cooperatives (~32k)** — COPOMIS login-gated, no public directory.
- **Community schools (tens of thousands)** — IEMIS portal down; only aggregate PDFs public.
- **Federal projects (NPBMIS)** — API returns 401 across all endpoints; gated.
- **Provincial/local projects (100k+)** — hostnames not even located (NXDOMAIN).
- **Govt office tree** — no public count, no stable id; must be reconstructed, not sourced.
- **Special Court / DMLI charge sheets** — host DOWN (HTTP 000).

These collapse to **HELD, not inserted** (single-source). This is the concrete consequence: the
volume buckets that would carry 1M are precisely the ones that fail the 2-source gate.

## Data-access blockers (need data-sharing / API-access requests)

Gated or unreachable primaries — pursue formal requests; **do not bypass auth or disable TLS on prod**:

| Blocker | Symptom | Path forward |
|---|---|---|
| **COPOMIS** (cooperatives) | Login wall; no public directory | Data-sharing request to Dept of Cooperatives / MoLMCPA; interim = stats PDFs + provincial registers. |
| **IEMIS** (schools) | `iemis.cehrd.gov.np` ECONNREFUSED | Direct IEMIS data request to CEHRD; interim = Flash Report aggregates + palika profiles. |
| **NPBMIS** (federal projects) | All `/api/` endpoints **HTTP 401** | Token/approved-access request to NPC; clarify ToS before any token reverse-engineering. |
| **NHFR** (health) | Cert EXPIRED, app wouldn't load | Reach out re: API/CSV export; confirm facility-code format + NES overlap. |
| **OCR / CAMIS** (companies) | `application.ocr.gov.np` cert-chain fail; no bulk export | Per-company JSF scrape only; pursue any open-data/bulk path. CAMIS filings gated. |
| **Provincial project banks** | Hostnames NXDOMAIN | Locate canonical per-province PPC hosts from OPMCM/province portals first. |
| **TLS chain (multiple)** | bolpatra / SC / OCR-sub: incomplete intermediate-CA (verify=20); ECN/SWC/NHFR expired | Supply missing intermediate CA per host; relax verify for *public reads only* after manual review. Expected GoN condition. |

## Revised realistic-reachable estimate (given RAG status)

Applying the gate (GREEN counts; RED is HELD; AMBER counts only the corroboratable fraction):

| Tier | Buckets | RAG-adjusted reachable |
|---|---|---|
| GREEN core | geography, courts, CIAA, OAG, NKP, contract-awards, BFIs, PMs/Kings, reports | **~13k–15k** entities/documents (mostly orgs, locations, courts, doc-events) |
| AMBER (partial) | current MPs+prov (~825), wards (6,743), NGOs (~26k, mirror-corroborated), health (NES overlap), listed cos (~532), OCR-as-corroborator, judges, govt-leaders | **~40k–80k** once corroboration confirmed |
| RED (HELD) | candidates, local-official tail, cooperatives, schools, all projects, office tree, special court | **~0 admissible** under the rule (held) |

**Realistic reachable under the strict ≥2-source rule today: ~50k–100k distinct admissible
entities** — *below* the feasibility doc's ~250k–450k authoritative ceiling, because the gate
removes the gated/single-source volume buckets. The ~250k–450k floor is only recoverable as the
**AMBER/RED blockers are unlocked** (data-sharing requests for COPOMIS/IEMIS/NPBMIS, a real
independent 2nd source for local officials, ward-code assembly). **1M remains unreachable** under
the rule — the buckets that would deliver it (candidates × cycles, projects × governments × FYs)
are exactly the RED ones with no independent 2nd source.

**Honest bottom line:** the ≥2-source rule converts the 1M aspiration into a ~50–100k near-term
admissible floor, expandable toward the mid-hundreds-of-thousands only by unlocking the listed
data-access blockers — never to 1M without relaxing either the rule or the entity definition.
