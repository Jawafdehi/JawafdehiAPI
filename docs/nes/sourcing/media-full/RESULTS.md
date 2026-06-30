# Media DEEP-ENRICHMENT — Nepal's full registered NEWSPAPER universe

**Wave:** deepen NES media beyond the ~25 majors to the WHOLE registered universe.
**Date:** 2026-06-28. **Read-only** acquisition; the live NES DB was **NOT** touched.
**Output:** `normalize_media_full.py` -> `media_full_records.json` (`{"records":[...]}`),
**897 records, all 897 validation-passing.**
**Raw OCR:** `raw_pc_classification_2079_080.tsv` (895 transcribed register rows).

## Registry access verdict (the headline)

The prior `media` wave concluded a full registered-outlet list was "NOT cleanly
obtainable as structured data" and deferred the long tail. **This wave overturns that
for newspapers**: the Press Council classification register IS obtainable and was OCR'd
into structured data.

- **Press Council Nepal — newspaper CLASSIFICATION (वर्गीकरण) register, FY2079/080**
  (`presscouncilnepal.gov.np .../2025/12/final_-2079-080-_pdf.pdf`, 19 pp, 1.8 MB):
  **OBTAINED + OCR'd.** It is a born-digital PDF whose embedded **text layer is
  legacy-font garbled** (the Devanagari Unicode round-trips to scrambled glyphs, e.g.
  "मोरङ"→"िोरङ"), so `page.get_text()` is unusable. Each page was rendered to a 180-dpi
  image and OCR'd with **Bedrock multimodal Claude** (`us.anthropic.claude-sonnet-4-6`,
  **Converse** API, profile `orion-admin`, `us-west-2`). 19/19 pages, **895 rows**
  transcribed cleanly with the table columns intact (serial | name | district |
  frequency | level | grade). This is the single strongest registry primary for Nepali
  newspapers and is the core of this wave. **~890 newspapers enumerable.**
- **Press Council Nepal — "Listed online media" register** (till 2082-05-05, 122 pp,
  3.8 MB): OBTAINED (same legacy-font garble → OCR-needed). The full 122-page tail is
  very large and almost entirely single-source; a **representative head of 8 notable
  portals** was captured (each with a verified live domain → 2 sources). The deep
  online tail is **deferred to a follow-up bulk OCR wave** (see Deferred tail).
- **DoIB FM/TV licence roster** (`doinepal.gov.np`): the licence-list pages
  (`/content/13144/...` FM records, `/content/13143/...` licenses) render their tables
  via **JavaScript** — there is **no static PDF roster** in the page HTML, so the 1000+
  community-FM list is **NOT cleanly machine-readable** here. This **confirms the prior
  wave's DoIB verdict.** The major FM networks (Kantipur FM, Ujyaalo) are already in the
  prior wave. The community-FM long tail stays **deferred**.

### OCR notes
- Model: `us.anthropic.claude-sonnet-4-6` via **Bedrock Converse** (the shared
  `ocr_bedrock.py` helper's `invoke_model` path hit two Bedrock issues for the newer
  models — on-demand throughput not supported for the bare model id, and the
  `bedrock-2023-06-01` body version rejected — so this wave used the model-agnostic
  **`converse`** API with the **`us.` inference-profile** id; documented here for the
  next agent. A small follow-up to `ocr_bedrock.py` to switch to Converse is advisable.)
- GoN TLS verification was disabled per the directive (certs fine here, but kept off).
- DPI 180 was the sweet spot — clean Devanagari, full row capture (42–51 rows/page),
  ~25 s/page, ~8 min for the 19-page classification.
- Quality: all 895 rows parsed to exactly 6 fields; grades land on क/ख/ग/घ for 820
  rows. The final 2 pages are a re-classification appendix whose grade column held
  ditto/remark tokens ("हुबहु" etc., 69 rows) — those records are emitted with **no
  `pressCouncilGrade`** (grade left null, not guessed) but keep name/district/freq/level.

## Counts

| mediaType  | Captured | Source |
|------------|----------|--------|
| newspaper  | 889 | Press Council classification register FY2079/080 (OCR) |
| online     | 8   | Press Council "Listed online media" register (sampled head) |
| **total**  | **897** | |

**Newspapers by grade (क/ख/ग/घ classification):**

| Grade | Count | Meaning |
|-------|-------|---------|
| क (A) | 198 | top grade |
| ख (B) | 462 | |
| ग (C) | 154 | |
| घ (D) | 6   | lowest grade |
| (none) | 69 | re-classification appendix rows; grade not on the row → left null |
| **total** | **889** | |

By frequency (register field): weekly ~510, daily ~210, monthly ~81, plus literary /
fortnightly / quarterly / bimonthly / language editions.
By level: local ~595, provincial ~156, national ~143.
**HQ districts covered: 66 of 77** (the register is genuinely nationwide — the prior
wave was Kathmandu-only).

## From OCR'd registry lists

- **889 newspapers** come directly from the OCR'd Press Council classification PDF
  (the 895 transcribed rows minus 6 that collided with already-ingested majors).
- **8 online portals** come from the Press Council "Listed online media" register
  (sampled), each cross-checked against a **live official domain** (all 8 resolve;
  `imagekhabar.com`/`nepalpress.com` return 403 = live-but-bot-blocked).
- 0 from DoIB (roster not machine-readable; see verdict).

## Skipped as already-ingested

The prior `media` wave's 23 records + 3 state-media SOEs are NOT re-emitted. Dedup is
on Nepali-name token **and** ASCII slug. **6 register rows were skipped** as collisions
with prior-wave majors (e.g. Kantipur/Nagarik/Rajdhani-family mastheads that also
appear in the classification list). The 3 state SOEs (Gorkhapatra Corporation,
Nepal Television, Radio Nepal) and RSS are corporation/agency entities, not newspaper
mastheads, so they do not appear in this newspaper register and need no further skip.

## >=2-source vs HOLD

Per the brief, the **Press Council classification register is itself a strong registry
primary** (an authoritative GoN register carrying a real per-entity classification id
`PCN-2079/080-<serial>` + grade). Every record carries it as **Source #1**. The
independent per-entity **Source #2** (own site / Wikidata) exists only for the notable
few — and the genuine majors are already in the prior wave — so:

- **8 records have >=2 independent per-entity sources** (the 8 online portals: register
  + verified own portal domain).
- **889 newspaper records are HOLD** (`"hold": true` + `hold_reason`) — single registry
  primary, no independent per-entity 2nd source. This is the **honest** long-tail bar:
  the records are emitted (the register is a legitimate primary and gives a real
  classification id), but they should not auto-publish until a 2nd independent
  per-entity source is attached. This mirrors the SOE/prior-media HOLD discipline.

## Target shape / @type decision (unchanged from prior wave)

`@type` = schema.org **`"Organization"`**; refinement = `additionalType` STRING
**`"jawafdehi:MediaOrganization"`** (not in `KNOWN_JAWAFDEHI_TYPES`, so kept out of
`@type`). IRI prefix `organization/media/newspaper/<slug>` (3 segments). Slug =
ASCII transliteration of the Devanagari masthead + `-pc<serial>` (register serial as a
stable, collision-free suffix; display `name.ne` keeps the exact Devanagari).
Identifiers: `press-council-classification-id` (`PCN-2079/080-<serial>`),
`press-council-grade`, `media-domain` (online only), `wikidata-qid` (none on the
newspaper tail). Custom props: `jawafdehi:mediaType`, `jawafdehi:pressCouncilGrade`,
`jawafdehi:pressCouncilLevel` (national/provincial/local), `jawafdehi:frequency`,
`jawafdehi:language` (Nepali). `containedInPlace` -> the ingested HQ **district** IRI,
mapped from the register's Nepali district cell (incl. OCR-variant aliases).

## Validation

`PYTHONPATH=shared:services/nes` -> `validate_jsonld_entity` + `is_valid_entity_iri`
on all 897 records (the real `nes_service.entities.validation` harness):

```
records=897  validate_jsonld_entity PASS=897  is_valid_entity_iri PASS=897
invalid containedInPlace district refs: 0
unique IRIs: 897 / 897 (no dupes)   bad slugs: 0
```

**897 / 897 pass.**

## Deferred tail

- **Press Council "Listed online media" register — the deep 122-page tail.** Obtained
  and OCR-able the same way; only a representative head of 8 was captured this wave.
  A follow-up should OCR all 122 pages (≈ same per-page cost; ~50 min) for the full
  online-portal universe (hundreds–thousands of entries, mostly single-source → HOLD).
- **DoIB FM/TV licence roster (1000+ community FMs).** Not statically machine-readable
  from the JS-rendered DoIB site; needs either a rendered-DOM scrape, a FOIA/data
  request, or a future DoIB PDF roster. Deferred (the major FM networks are already in
  NES).
- **Older classification editions** (FY2074/75 … 2076/77 PDFs are also on the site) —
  useful for historical grade tracking / dedup, not for net-new outlets.
- **Wikidata/own-site enrichment of the newspaper tail** — to lift HOLD newspapers to
  2-source as their Q-ids / official sites are confirmed one by one.
