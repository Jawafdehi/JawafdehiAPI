# Historical Supreme Court Justices of Nepal — PERSON wave (REAL acquisition)

**Status:** RAN end-to-end with REAL data acquired live from this environment, and
**every record VALIDATED against the live NES validator**
(`nes_service.entities.validation.validate_jsonld_entity` +
`jawafdehi_shared.entities.ids.is_valid_entity_iri`). The live `bulk_ingest` (DB
write) is left to the orchestrator — **the live NES DB was NOT touched.**
**Date:** 2026-06-28.

This wave fills the gap the judges wave left open: that wave captured only the
**Chief-Justice succession** (32), the **current sitting bench** (17), and **HC chief
judges** (7) = 56 entities. The full roster of **ordinary Justices** (माननीय
न्यायाधीश) who served on the Supreme Court — the people who actually heard the cases —
was missing. Here we source the court's **own former-justices register**,
prioritising everyone who served since ~2060 BS (≈2003 AD); the register is ordered
and complete back to #1, so the earlier roster is captured too.

## Sources used + which worked (TLS)

| Source | Role | Authority | TLS | Worked? | Provides |
|---|---|---|---|---|---|
| **Supreme Court former-justices register** `/web/exjustices` | **#1 office authority** | `supremecourt.gov.np` | **mis-chained (fetched `-k`)** | ✅ HTTP 200, 93 KB | the ordered register: 74 former justices (सि.नं./नाम/देखि/सम्म); recent cohort with **BS appointment + retirement dates** |
| **Supreme Court current-bench page** `/web/justices` | #1 bilingual name forms | `supremecourt.gov.np` | mis-chained (`-k`) | ✅ HTTP 200, 113 KB | Devanagari name forms for the recently-retired cohort (register lists them English-only) |
| **Wikidata** (`wbsearchentities` + `wbgetentities`) | **#2 corroborator** | `wikidata.org` | valid | ✅ (GET) | stable **Q-ids**, bilingual labels, **P31=Q5 human** + Nepali-judge verification |
| English Wikipedia "Category:Justices of the Supreme Court of Nepal" | #2 cross-check | `en.wikipedia.org` | valid | ✅ | confirmed the Wikidata Q-id ↔ judge mapping during the verification pass |

**TLS finding (as doctrine predicts):** the SC portal presents a broken chain
(`unable to get local issuer certificate`); fetched TLS-tolerant `-k` (read-only).
Snapshots saved under `sources/`. Wikidata has valid TLS.

**OCR:** the `shared/ocr_bedrock.py` Bedrock pipeline was **available but NOT needed**
— the court's HTML former-justices register is a clean, machine-readable text table
that already supersedes what the scanned annual reports (वार्षिक प्रतिवेदन) would have
yielded for the *roster* question. OCR remains the unlock for **per-year bench
composition + appointment dates for the undated older cohort** (register serials
~24–57 are name-only) — see Deferred.

## Pipeline

1. **Acquisition** (live, `-k`): `sources/sc_exjustices.html`, `sources/sc_justices_current.html`.
2. `parse_sources.py` → `sources/exjustices_parsed.json` (74 rows: serial, English
   name, BS from/to dates, expiry notes) + `sources/current_devanagari.json` (34
   Devanagari name strings off the current page).
3. `enrich_wikidata.py` → `sources/wd_search.json` + `sources/wd_labels.json`
   (**source #2**: per-name best item, accepted only on a Nepali-judge-shaped
   description; bulk P31=Q5 human check). 74 names searched → 9 auto-accepted; a
   hand-verified pass raised confirmed-human-judge items to **12** (one, Anil Kumar
   Sinha, is a *current* justice not on the historical register → out of scope here,
   so **11** land on register rows).
4. `normalize_justices.py` → `justices_records.json` ({"records":[…]}). BS→AD tenure
   dates converted via the platform `convert_date` tool (Asia/Kathmandu) and stored
   as both `{bs, ad}`. Pure + offline.

## Entity resolution / reconciliation (no duplicates)

- **@id keying** identical to the judges/leaders waves: `<name>-q<qid>` when a
  confirmed-human WD item exists, else `<name>-ex-justice-<serial>`.
- **Checked against the 56 already-ingested judge entities**: **0 @id collisions and
  0 Q-id collisions.** The CJ-succession + current-bench + HC-chief entities and this
  former-justices roster are disjoint sets, so **all 74 are NEW entities** — none
  duplicate, none silently overwrite. (A former justice who is *also* in the CJ
  succession would merge by Q-id; none of the 74 are.)
- `hasOccupation` is built so a matched-Q-id person carries a **role list** (the
  ingest layer merges) — the standard Justice→Chief-Justice career consolidation.

## ≥2-source gate (and the HOLD set)

SC register (#1) + a **confirmed-human Nepali-judge Wikidata item** (#2) → PUBLISHABLE;
register-only → **HELD** (1 source). Honest outcome for a historical roster: **most
justices are single-source.**

| | total | publishable (≥2 src) | **HELD** (1 src) |
|---|---|---|---|
| Former Supreme Court justices | **74** | **11** | **63** |

The 11 publishable (Q-id-corroborated): Paramananda Jha (Q2482925; SC justice→VP),
Sushila Singh Shilu (Q21459157; first woman SC justice), Bharat Raj Upreti
(Q19970838), Sarada Prasad Ghimire (Q60286796), Deepak Raj Joshee (Q55585066), Kedar
Prasad Chalise (Q60286793), Damber Bahadur Shahi (Q94744004), Mira Khadka
(Q60215863), Deepak Kumar Karki (Q60215909), Ishwar Prasad Khatiwada (Q60675614),
Ananda Mohan Bhattarai (Q60915514). The remaining 63 are HELD — they have **no
human Wikidata item** (Nepali judges are largely Wikipedia redlinks), correctly held
at 1 source rather than force-published. They flip to publishable the moment a 2nd
independent source attaches (a gazette/Judicial-Council appointment notice, a Nepali
news profile, or a future Wikidata item). Nothing was dropped.

**Two same-name FALSE POSITIVES were adversarially REFUTED and EXCLUDED** (verified
via P31/P27/P106):
- *Arjun Prasad Singh* → Q64003537 = an **Indian politician** (P27=India), not the
  Nepali SC justice. EXCLUDED.
- *Gyanendra Bahadur Karki* → Q30300972 = a Nepali **finance minister** (occupation
  politician, ministerial P39s), not the judge. EXCLUDED.
Also avoided: Deepak Kumar Karki *researcher* Q91840579; "Anil Sinha" CBI-director-
India Q18600312.

## Coverage estimate vs the true count

- **How far back:** the register is **complete and ordered from #1** (Babbar Prasad
  Singh, the earliest justices of the apex court) through **#73 Kumar Chudal** (retired
  2082-07-09 BS = 2025-10-26). So the wave reaches the full historical roster, well
  beyond the 2060 BS floor.
- **Post-2060-BS cohort (the priority window, ≈2003 AD onward):** the register's
  boundary marker is **#23 Harishchandra Prasad Upadhayaya (expired 2060.9.3 BS)**;
  every justice from ~#24 onward (≈50 people) served at or after 2060 BS. **All are
  captured.** The dated subset (#58–#73, 15 justices) carries full BS appointment +
  retirement dates → converted to AD.
- **vs the true count:** the official register IS the authoritative count of *former*
  justices (74). Adding the 17 currently-sitting + the CJ-line already in NES, the
  modern (post-2060) SC bench is fully enumerated. **Estimated coverage of the
  post-2060 justice roster: ~100% of named persons**; the gap is *tenure dates* for
  the undated older cohort (#24–#57), not missing people.

## Validation (run against the live validator)

```
records: 74
validate_jsonld_entity PASS: 74   FAIL: 0
is_valid_entity_iri OK:      74   BAD:  0
unique IRIs:                 74   (no collisions)
memberOf -> supreme-court:   74/74
@id/Q-id collisions vs existing 56 judges: 0 / 0
```

**74/74 pass** both the JSON-LD entity validator and the canonical-IRI check; every
record's `hasOccupation.memberOf` links the existing Supreme Court org anchor
(`…/organization/judiciary/supreme-court`, already in NES — not duplicated).

## JSON-LD shape (matches the judges wave)

```jsonc
{
  "@context": "https://schema.org",
  "@type": "Person",
  "@id": "https://jawafdehi.org/entity/person/deepak-raj-joshee-q55585066",
  "name": { "en": "Deepak Raj Joshee", "ne": "दिपकराज जोशी" },
  "hasOccupation": {
    "@type": "Role",
    "roleName": "Justice of the Supreme Court of Nepal",
    "jobTitle": { "en": "Justice of the Supreme Court of Nepal", "ne": "न्यायाधीश" },
    "memberOf": { "@id": ".../organization/judiciary/supreme-court", "name": {…} },
    "startDate": "2014-05-27",
    "jawafdehi:tenureStart": { "bs": "2071-02-13", "ad": "2014-05-27" },
    "endDate": "2019-02-18",
    "jawafdehi:tenureEnd": { "bs": "2075-11-06", "ad": "2019-02-18" }
  },
  "jawafdehi:branch": "judiciary",
  "identifier": [{ "@type": "PropertyValue", "propertyID": "wikidata", "value": "Q55585066" }],
  "sameAs": "https://www.wikidata.org/wiki/Q55585066"
}
```

## Deferred (honest gaps)

- **Tenure dates for the undated older cohort** (register serials ~24–57): the
  register lists them name-only. The unlock is **OCR of the SC annual reports**
  (वार्षिक प्रतिवेदन, scanned PDFs → `shared/ocr_bedrock.py`), which print the bench
  composition per year, and/or **Nepal Gazette appointment notices** (rajpatra). Not
  run this pass because it does not change the *who* (the register already names
  everyone) — only enriches dates.
- **2nd source for the 63 HELD justices:** Judicial Council (jcs.gov.np) appointment
  records or gazette notices would corroborate independently of the SC portal and
  flip HELD→published. (jcs.gov.np not exercised this pass.)
- **Devanagari for the undated cohort:** only 14/74 carry `name.ne` (the recent
  cohort + Q-id labels). Backfill from the gazette/annual-report OCR would raise
  bilingual coverage.
```
