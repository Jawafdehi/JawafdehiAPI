# HELD Entity Promotion Analysis

**What this is:** a fan-out audit of every HELD bucket in the NES sourcing record
files, answering one question — **how many held entities can `promote_held` flip to
published with NO new sourcing?** Companion to [`SOURCED-INDEX.md`](./SOURCED-INDEX.md)
(what's live) and the per-bucket `RESULTS.md` files under `nes/sourcing/`.

**Date:** 2026-06-30. **Method:** one analysis agent per held cluster, each re-applying
the publish gate's publisher-independence rule to the actual record JSON and cross-checking
against `promote_held.py`'s three promotion paths. Read-only; no DB touched.

---

## Bottom line

**`promote_held` would promote 0 of the 930 held records in the per-bucket record files
with no new sourcing.** The tool's three levers — (1) `@id` already published elsewhere,
(2) VAT/PAN cross-publisher pairing, (3) `--election-authority` ECN exception — fire on
**zero** held records across every bucket. The 930 HELD is overwhelmingly a **source-access
problem, not a tooling problem.**

> Count note: 930 is the figure across the *per-bucket record files* (matches SOURCED-INDEX
> §2.3). The SOURCED-INDEX header's **1,900** includes ~970 more staged in later ECN-era
> waves not present in these files.

---

## The gate (how a record gets held)

- An NES entity publishes only with **≥2 independent-publisher sources**. Independence is
  keyed on each source's `authority` (lowercased), else the URL's registrable domain. Two
  URLs from the **same** authority/domain = **one** publisher.
- Below that → **HELD** (staged in `held_entities`), recoverable not discarded.
- `promote_held` (`services/nes/nes_service/entities/management/commands/promote_held.py`)
  promotes ONLY via: (1) the held `@id` is already published in another wave → clear stale
  row; (2) two held records share a VAT/PAN identifier (`propertyID` contains vat/pan/tax)
  **and** their union spans ≥2 distinct publishers → merge + publish; (3) `--election-authority`
  → publish records sourced from the Election Commission (`election.gov.np` /
  `result.election.gov.np`). Anything else needs a genuinely NEW independent publisher fetched.

---

## Per-cluster verdict

| Cluster | Held | Tool-promotable NOW | Promotable WITH a fetch | Blocked on access |
|---|---:|---:|---|---|
| NGOs / INGOs | 318 | **0** | ~18 INGOs (Wikidata/own-site); 300 NGOs need an SWC-independent roster | ~300 |
| Parliament (PR + NA MPs) | 305 | **0** | all 305 via ECN PR-allocation + NA-results fetch | 0 (public, just not done) |
| Historical persons (leaders/secretaries/judges) | 194 | **0** | small minority via Wikidata — already exhausted for these people | majority |
| Org long-tail (profbodies/bfi/aayog/offices/ciaa-leadership) | ~46 | **0** | ~40 (per-entity Wikidata/news) | ~6 structurally single-publisher |
| Contractors | 29 | **0** | 10 bolpatra via `ocr.gov.np` VAT lookup; 19 PPMO need VAT recovered first | PPMO firms w/ no recoverable VAT |
| Schools | 45 | **0** | ~4–9 secondary via NEB/SEE | ~36 (IEMIS geo-fenced to Nepal) |
| **TOTAL (record files)** | **~937** | **0** | mostly fetch-gated | majority |

---

## Why "looks like 2 sources but isn't"

The recurring trap: a held record often carries two source *URLs* that collapse to **one
publisher**.

- **Contractors:** PPMO list-page + detail-page → both `ppmo.gov.np`.
- **Schools:** both sources hardcoded to `raw.githubusercontent.com`
  (`normalize_schools.py:80-81` sets `REGISTRY_AUTHORITY` and `GEO_AUTHORITY` to the same
  string). The independence is in the upstream provenance story, not the fields the gate reads.
- **NGOs:** 300/300 a single `swc.org.np` affiliation PDF.
- **Historical persons:** publishable = "has a Wikidata Q-id"; held = "Wikidata search
  returned nothing." Held ones are precisely the residue Wikidata already missed — re-running
  it yields little.

The gate is working as designed.

---

## Per-cluster detail

### Contractors — 29 held (19 PPMO + 10 bolpatra)
- 19 blacklisted carry only `ppmo-blacklist-id`, **no VAT/PAN** → can't enter the VAT join.
  Sole authority `ppmo.gov.np`.
- 10 award firms each carry a **distinct** `company-vat-pan`, all from `bolpatra.gov.np`
  (one publisher) → no two share a key, no cross-publisher union.
- **`promoted_by_vat_pairing` = 0.** The SOURCED-INDEX §4 claim that contractors are the top
  VAT/PAN promotion target **does not survive the data** — there's no shared key, and the
  PPMO side has no key at all. RESULTS.md says so plainly.
- **Next action:** fetch `ocr.gov.np` company registry for the 10 bolpatra VAT/PANs
  (300010073, 302954993, 301447779, 605970487, 301250032, 300130805, 300132335, 305246055,
  606786366, 609602250) → adds an independent registrar publisher, flips all 10. Highest
  anti-corruption value in the corpus.

### NGOs / INGOs — 318 held
- 300 NGOs: single `swc.org.np` FY2076/77 affiliation PDF; identifier is `swc-affiliation-no`
  (not a VAT/PAN → tool can't join). 18 INGOs: single `swcbeprod.cellapp.co` (iPact).
- iPact API can't corroborate the legacy 300 (null reg numbers, only ~254 new orgs). Wikidata
  near-zero for domestic NGOs. NFN/DAO/PAN rosters proposed but **no demonstrated machine access**.
- Cheaper win: the 18 INGOs (internationally notable → Wikidata/own-site realistic).

### Parliament — 305/307 held
- Authorities are **only** `hr.parliament.gov.np` (208 PR/HoR) and `na.parliament.gov.np`
  (97 NA); 2 carry both (same `parliament.gov.np` registrable domain → still one publisher).
- **Zero are ECN-sourced** → `--election-authority` promotes 0. This was the highest-leverage
  hope and it's a dead end: these came from the parliament secretariat APIs, never the ECN.
- All 305 are PROMOTABLE-WITH-FETCH (public sources): ECN per-cycle **PR seat-allocation** lists
  corroborate ~207; NA indirect-election results / Wikidata for ~97.

### Historical persons — 194 held (leaders 133 + secretaries 35 + judges 26)
- All single-source. Leaders: `en.wikipedia.org` (113) + `supremecourt.gov.np` (20).
  Secretaries: `en.wikipedia.org` (35). Judges: `supremecourt.gov.np` (20) + `en.wikipedia.org` (6).
- **No data-shape bug:** held leaders carry 0 wikidata identifiers; held secretaries/judges carry
  0 identifiers at all. Held = Wikidata genuinely found nothing. Correctly held.
- Best non-Wikidata levers: TLS-tolerant (`curl -k`) fetch of `*.parliament.gov.np` rosters
  (leaders); the official OPMCM `/ex-cs` page (currently 404) would promote ~25 historical Chief
  Secretaries in one shot (different domain → passes the gate). Judges are closest to genuinely
  sourceless (no enumerable independent roster).

### Org long-tail — ~46 held
- professional-bodies 10, bfis 6, aayogs 5, offices-federal 7, ciaa-leadership 11, offices-moha 4,
  offices 3. None has VAT/PAN, ECN, or an already-published `@id` → tool-promotable 0.
- **3 ministries (`offices`)** are the cheapest win in the whole corpus: held purely on a stale
  Wikidata P856 domain alias (mocit/moics/motca) — add 3 verified Q-ids → publish.
- offices-moha 4 and most org records are PROMOTABLE-WITH-FETCH via per-entity Wikidata/news.
- Structurally single-publisher (BLOCKED): 2 unreachable inclusion commissions (aayogs), 3
  federal inclusion commissions (Muslim/Madhesi/Tharu), thin micro-insurers (Star, Trust).

---

## Two real findings to act on

1. **CIAA-leadership gate-vs-flag divergence (logic gap, NOT a data bug).** 7 CIAA records
   carry two *distinct* authorities (`ciaa.gov.np` + `en.wikipedia.org`) yet are correctly
   `held=true` because Wikipedia transcribes the CIAA roster inline (not independent). But
   `promote_held._publishers` counts authority strings only — it would see "2 publishers" and
   treat them as fine. **If the publish gate ever recomputes independence from `sources` alone,
   those 7 would silently publish on a non-independent pair.** The coded rule (distinct host) is
   weaker than the rule the sourcing wave applied (semantic independence). Worth a code note /
   test.

2. **Doc count reconciliation (done 2026-06-30).** `ARCHITECTURE.md` and `README.md`
   formerly said "0 held" (pre-sourcing figure) — now updated to ~1,900. SOURCED-INDEX §2
   was reconciled: header 1,900 vs §2.3's 930 explained (930 = the per-bucket record-file
   count; ~970 more staged in later ECN-era waves), and §2.1/§2.2 percentages flagged as
   pre-ECN snapshots pending a live recount.

---

## Genuinely promotable work (all needs a fetch), ranked by leverage

1. **3 ministries (`offices`)** — add 3 verified Wikidata Q-ids (stale-domain alias fix). *Cheapest.*
2. **10 bolpatra contractors** — 10 keyed `ocr.gov.np` VAT/PAN lookups. *Highest anti-corruption value.*
3. **~40 org long-tail + 18 INGOs** — per-entity verified Wikidata/news 2nd source.
4. **305 parliament MPs** — one ECN PR-seat-allocation fetch (~207) + NA-results pass (~97). *Biggest count.*

Everything else (300 NGOs, ~36 schools, the historical-person majority) is **blocked on source
access** — a Nepal-egress / credentialed / data-sharing path — not on engineering or tooling.
