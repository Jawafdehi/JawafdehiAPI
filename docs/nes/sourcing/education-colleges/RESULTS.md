# NES sourcing — Nepal's private / affiliated colleges (the deferred HE remainder)

REAL acquisition, 2026-06-28. Read-only; the live DB was **not** touched.

This wave captures the affiliated-college tier that the universities wave deferred:
the colleges sitting *below* the 31 universities + 64 TU constituent campuses that
the earlier wave already ingested. UGC's universe is **~1,340 affiliated colleges**
(TU ~1,080, PU 116, PokU 62, KU 23, ...). We sourced the **cleanly-enumerable,
publicly-listed subset** and HELD the rest (see *Deferred remainder*).

Pipeline mirrors `education-universities/normalize_universities.py`:
≥2 independent sources → join → schema.org JSON-LD
`{"records":[{entity_prefix, entity_data, sources}]}` → validate → (bulk_ingest).
`normalize_colleges.py` is pure/offline given the `./snapshots`; `fetch_sources.sh`
does the (read-only, TLS-relaxed) acquisition.

## Headline numbers

| | count |
|---|---|
| **College records PUBLISHED (≥2 sources)** | **136** |
| HELD (<2 independent sources) | 50 |
| of ~1,340 UGC-counted affiliated colleges | ~10% of the universe; ~100% of the cleanly-public subset we could reach |

Published by affiliating university (`parentOrganization`):

| University | published |
|---|---|
| Tribhuvan University (`tu`) | 85 |
| Pokhara University (`pu`) | 42 |
| Madhesh / Mid-West Univ. (`mwu`) | 2 |
| Purbanchal University (`purbanchaluniversity`) | 2 |
| Kathmandu University (`ku`) | 3 |
| Sudurpaschim / Far-West (`fwu`) | 1 |
| (foreign affiliation — no Nepal parent) | 1 |

## Sources (all public, read-only — snapshots in `./snapshots`)

- **A. UGC Nepal QAA accredited-HEI directory** —
  `ugcnepal.edu.np/pages/qaa-receiving-heis-8`. A server-rendered HTML table
  (name | estd BS | province | nature | **affiliating university** | accreditation
  dates | status | website) of 106 accredited + 12 in-process HEIs, spanning all
  universities. The recognition authority's own quality-assured list. authority =
  `ugcnepal.edu.np`. Corroborated by the **UGC accredited-HEIs PDF** (`List of
  Accredited HEIs`, ~80 entries) where names recur.
- **B. Pokhara University affiliated-colleges directory** —
  `pu.edu.np/affiliated-colleges` (58 colleges, each with name + full address →
  district + mostly an official `.edu.np` website). authority = `pu.edu.np`.
- **C. Kathmandu University affiliated-colleges directory** —
  `ku.edu.np/affiliated-colleges` (23 colleges, name + city). authority = `ku.edu.np`.
- **D. Each college's own `.edu.np` website** — an independent, self-attesting
  publisher used as the 2nd source for most PU entries and the UGC-QAA entries.

**≥2-source rule.** A college publishes only with ≥2 independent publishers among:
the affiliating-university list (PU/KU), the UGC-QAA list, and the college's own
`.edu.np` site. 135/136 carry the college's own website as a corroborator; 13 carry
≥3 sources (university list **and** UGC-QAA **and** own site). The 50 HELD are
single-source: PU/KU page-only colleges with no own site that also don't appear on
the UGC-QAA list (PU 18, KU 24), plus 8 UGC-QAA rows whose only "website" was a
Google-search URL (not an institutional site).

## OCR

The directive's Bedrock OCR helper script was wired into
`fetch_sources.sh` for the **UGC accredited-HEIs PDF** (`accredited_heis.pdf`, 5
pages). That PDF is **born-digital** — PyMuPDF's text layer extracted it cleanly, so
the helper's OCR fall-back was not needed for it (it would have triggered
automatically for any scanned page). No other source required OCR (the UGC/PU/KU
directories are HTML). The fetch ran with TLS verification relaxed (`-k`) because the
GoN/UGC and several `.edu.np` college certs are broken — tolerated per the directive.

## Output shape (matches the universities wave + `nes_service` validation)

- `@type = "EducationalOrganization"` (known schema.org type), with the Nepal
  refinement carried as `additionalType = "jawafdehi:College"` **string** (it is not
  in `KNOWN_JAWAFDEHI_TYPES`, so it cannot be an `@type`).
- IRI: `…/entity/organization/education/college/<slug>` (3-segment prefix, ≤4 OK).
  Slug = the college's own `.edu.np` domain stem where present (e.g. `nec` from
  `nec.edu.np`), else a name slug; district-suffixed on collision.
- `parentOrganization → {@id: <university IRI>}` joined by affiliating-university
  name to the **universities-wave** IRIs (`…/education/university/<slug>`).
- `containedInPlace → {@id: <district IRI>}` joined to the **locations-wave**
  district IRIs by parsing the address / name location token.
- `identifier`: `edu-np-domain` (own host), `ugc-nature` (Community/Private/...);
  `foundingDate` (AD year) or `jawafdehi:establishedBS` (BS year verbatim).
- bilingual `name`: only `en` is present (the public directories are English-only;
  no Devanagari college names were available without the auth-gated HEMIS records).

## Validation (TESTING=true, `validate_jsonld_entity` + `is_valid_entity_iri`)

| check | result |
|---|---|
| `validate_jsonld_entity` pass | **136 / 136** |
| `is_valid_entity_iri` pass | 136 / 136 |
| duplicate `@id` | 0 |
| `parentOrganization` resolves to a real university IRI | 135 / 136 (the 1 miss = a foreign-affiliated college with no Nepal parent — correctly omitted) |
| `containedInPlace` resolves to a real district IRI | 125 / 136 (92%) |
| records with ≥2 independent sources | 136 / 136 |

No dangling parent/district references: every emitted `parentOrganization` /
`containedInPlace` IRI exists in the universities / locations records.

## Deferred remainder (~1,200 colleges)

The bulk of the ~1,340 affiliated colleges remains deferred:

- **The full TU (~1,080) and Purbanchal (~150) college rosters are auth-gated.** The
  authoritative national directory is UGC's **HEMIS** SPA (`hemis.ugcnepal.edu.np`,
  API `hemisapi.ugcnepal.edu.np/api`). Its college/institution/`qaahei` endpoints
  return **HTTP 401** (Bearer-token required); only `Address/GetAll` is public. So
  TU/Purbanchal colleges were reachable only through the UGC-QAA *accredited* subset.
  TU has no single public consolidated affiliated-college page (it is split per
  faculty/institute), so it could not be cleanly enumerated without HEMIS auth.
- **PU/KU single-source colleges (42)** are HELD pending a 2nd publisher (own site
  or UGC-QAA appearance).
- To extend: obtain a HEMIS read credential (then ingest the full `qaahei/
  by-university/all` roster as source A with the per-college sites as source B), or
  OCR the per-university affiliated-college PDFs in Purbanchal's `download/list` and
  TU faculty (IOST/IOM/FOM) directories — the `ocr_bedrock.py` helper is ready for
  those scans.

## Files

- `normalize_colleges.py` — the normalizer (offline; reads `./snapshots` + the
  universities & locations records).
- `colleges_records.json` — `{"records":[…136…]}`, ready for bulk_ingest.
- `fetch_sources.sh` — read-only acquisition (TLS-relaxed; wires in the OCR helper).
- `snapshots/` — `ugc_qaa_heis.html`, `accredited_heis.pdf` + `.txt`,
  `pu_affiliated.html`, `ku_affiliated.html`, plus HEMIS recon artifacts
  (`hemis_app.js`, `hemis_root.html`) documenting the 401 gate.
