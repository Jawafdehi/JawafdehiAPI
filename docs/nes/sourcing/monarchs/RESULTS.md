# NES deep-enrichment — Nepal's MONARCHS / hereditary rulers

Read-only sourcing wave. Output: `monarchs_records.json` ({"records":[...]}),
produced by `normalize_monarchs.py`. The live NES DB was **not** touched — the
orchestrator runs `bulk_ingest` separately.

## Totals

- **38 ruler records total**, all `@type=Person`, `jawafdehi:branch="monarchy"`.
- **16 NEW** entities, **22 UPGRADE** existing pms-kings persons (reconciled by
  exact `@id` + Wikidata Q-id — verified against the pms-kings wave's
  `pms_kings_records.json`).
- **Q-id coverage: 38/38 (100%)** — every record carries a Wikidata Q-id
  identifier and >=2 independent sources (Wikipedia + Wikidata item). 0 HOLDs.
- **Validation: 38/38 PASS** via `validate_jsonld_entity` + `is_valid_entity_iri`
  (run with `TESTING=true DJANGO_SETTINGS_MODULE=monolith.config.settings uv run python`).
- All 38 `@id`s and 38 Q-ids are unique within the set; no Q-id maps to two `@id`s.

## How far back

The lineage now reaches **1382** (Jayasthiti Malla, Kathmandu Valley) and the
**1559** founding of the Gorkha Shah principality (Dravya Shah) — i.e. ~400 years
before the previous floor of 1768 (Prithvi Narayan Shah). Coverage is continuous
on the Shah branch from Gorkha (1559) through deposition of the monarchy (2008),
plus the Rana hereditary-PM rule (1846-1951) and a documented Malla subset.

## Breakdown by group

### 1. Pre-unification Gorkha-kingdom Shah kings — 9 NEW
The 9 predecessors of Prithvi Narayan Shah on the Gorkha throne. roleName
`King of Gorkha`.

| King | Reign | Q-id |
|---|---|---|
| Dravya Shah | 1559–1570 | Q20107430 |
| Purna Shah | 1570–1605 | Q20961704 |
| Chatra Shah (Chhatra) | 1605–1606 | Q20943902 |
| Ram Shah | 1606–1636 | Q20933609 |
| Dambar Shah | 1636–1645 | Q20933568 |
| Krishna Shah | 1645–1661 | Q20948695 |
| Rudra Shah | 1661–1673 | Q20922439 |
| Prithvipati Shah | 1673–1716 | Q20909707 |
| Nara Bhupal Shah | 1716–1743 | Q6965226 |

### 2. Shah kings of unified Nepal — 12 UPGRADE
Identical `@id`/Q-id to pms-kings, re-emitted so the set is self-contained and
UPSERTs in place. roleName `King of Nepal`. Includes the 2001-massacre
succession (Birendra Q162306 → Dipendra Q311235 → Gyanendra Q201327) and the
two split reigns (Tribhuvan 1911–1950 / 1951–1955; Gyanendra 1950–1951 /
2001–2008). Q-ids: Q574450, Q2482931, Q2714575, Q2714587, Q788541, Q2523476,
Q886987, Q381928, Q313110, Q162306, Q311235, Q201327.

### 3. Malla kings of the Kathmandu Valley — 7 NEW
Well-documented subset (own Wikipedia article + stable Wikidata Q-id). roleName
encodes the sub-kingdom. The Malla set is intentionally a **curated subset**, not
exhaustive — the dynasty spans three overlapping sub-kingdoms (Kantipur,
Lalitpur, Bhaktapur) with many minor/disputed-date rulers; only the cleanly
sourced majors are captured here.

| King | Sub-kingdom | Reign | Q-id |
|---|---|---|---|
| Jayasthiti Malla | Nepal Mandala (Valley) | 1382–1395 | Q6167681 |
| Yaksha Malla | Nepal Mandala (Valley) | 1428–1482 | Q6167707 |
| Siddhi Narasimha Malla | Lalitpur (Patan) | 1619–1661 | Q65395539 |
| Pratap Malla | Kantipur (Kathmandu) | 1641–1674 | Q7238587 |
| Bhupatindra Malla | Bhaktapur | 1696–1722 | Q13184445 |
| Ranajit Malla | Bhaktapur | 1722–1769 | Q7290704 |
| Jaya Prakash Malla | Kantipur (Kathmandu) | 1736–1768 | Q15955168 |

### 4. Rana hereditary PMs as de-facto rulers — 10 UPGRADE
Already in pms-kings as **executive** Prime Ministers; here re-emitted on the
SAME Q-id/`@id` with `jawafdehi:branch="monarchy"` and an ADDED regnal role
`Rana Prime Minister (ruler)` (de-facto-rule span = first PM start → last PM
end), while preserving the underlying PM tenure role(s). The ingest reconciles
on Q-id and upgrades the existing person.

Jung Bahadur Rana (Q2355896) → Bam Bahadur (Q1132107) → Ranodip Singh (Q593609)
→ Bir Shumsher (Q2268145) → Dev Shumsher (Q2267679) → Chandra Shumsher
(Q1061786) → Bhim Shumsher (Q1132119) → Juddha Shumsher (Q1132112) → Padma
Shumsher (Q2268133) → Mohan Shumsher (Q2268114), spanning 1846-09-15 to
1951-11-12.

## Sourcing

Every record carries >=2 independent sources:
- **Gorkha kings:** Wikipedia "Shah dynasty" + Wikidata item.
- **Unified-Nepal kings:** Wikipedia "List of monarchs of Nepal" + Wikidata item.
- **Malla kings:** Wikipedia "Malla dynasty (Nepal)" + Wikidata item.
- **Rana rulers:** Wikipedia "Rana dynasty" + Wikidata item.

## Notes / caveats

- **Reign-year conflicts:** for the early Gorkha kings, the per-king Wikipedia
  articles and the "Shah dynasty" list page disagree on a few boundaries
  (e.g. Ram Shah's accession is given as 1606 on his article vs 1609 on the
  list; Dambar 1636 vs 1633). Per-king-article years were used as the more
  specific source; the conflict is flagged here for downstream review.
- Reign years are emitted as bare year strings (`"1559"`), matching the existing
  pms-kings king records' `jawafdehi:tenureStart/End` style.
- **Licchavi / Gopala / Kirata / Mahispala dynasties: deliberately NOT included.**
  Their ruler lists are largely legendary/semi-mythical with thin, non-independent
  sourcing and unstable dates — they fail the >=2-clean-sources bar and were HELD.
- The Malla set is a documented subset by design (see group 3 note above).
