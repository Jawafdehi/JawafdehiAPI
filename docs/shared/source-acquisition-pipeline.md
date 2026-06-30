# Source-Acquisition Pipeline (cross-bucket)

Every NES/NGM sourcing bucket pulls from Nepal Government websites and document
dumps. These share the same access pathologies, so the acquisition layer is
defined once here and reused by all buckets.

## Operating reality: Nepal-gov TLS is unreliable (expected, not an error)

GoN sites (`*.gov.np`) frequently present **expired / mis-chained / self-signed
TLS certificates** — the deep-research run hit this on NHFR (`nhfr.mohp.gov.np`)
and ECN (`election.gov.np`). **This is normal and must not be treated as a
fetch failure.** The pipeline must:

- Retry on TLS errors with cert verification relaxed **for fetching only**
  (these are public documents; we are reading, not transacting — no secrets sent).
- Record the TLS condition in provenance metadata (so a reviewer knows the fetch
  bypassed verification), but do NOT drop the source.
- Prefer a cached / archive copy (Wayback, the source's own PDF mirror) when the
  live host is down, and note which copy was used.

## Document normalization: everything goes through likhit

All acquired documents are normalized through the **likhit** conversion pipeline
(the `convert_to_markdown` MCP tool — MarkItDown + the `likhit` plugin), which is
already the project's standard converter. Note the output is **Markdown**, not PDF;
"convert everything via likhit" = run every source document through this pipeline
to get clean, structured, extractable text. Handling by input type:

| Input | Path |
|---|---|
| Born-digital PDF (Nepali) | `convert_to_markdown` → likhit plugin (Nepal-specific PDF handling, page ranges supported) |
| Legacy `.doc` / `.docx` / `.xls` | **LibreOffice** (headless) for the office-format → render/extract step, then likhit/MarkItDown. (likhit also handles legacy `.doc`; LibreOffice is the fallback/normalizer for office formats.) |
| Scanned / image-only PDF | **OCR required** — use a latest-generation, high-quality OCR (Nepali Devanagari + English) before/within conversion. Born-digital extraction will silently return garbage on scans, so detect "no extractable text layer" and route to OCR. |
| HTML pages / tables | `convert_to_markdown` on the URL directly |

### OCR requirements
- Must handle **Devanagari + Romanized Nepali + English** on the same page.
- Use a current top-tier OCR engine (quality over speed) — many GoN PDFs are
  scanned government letterhead with stamps/signatures; low-grade OCR corrupts
  names, dates, and amounts, which then poison entity records.
- OCR confidence should be captured; low-confidence extractions are flagged for
  human audit rather than auto-inserted (ties into the verification gate).

### LibreOffice
- Headless LibreOffice (`soffice --headless`) for `.doc/.docx/.xls/.xlsx/.ppt`
  conversion/normalization. (Project already references a LibreOffice path for
  legacy office docs — reuse it; see CIAA description tooling.)

## Why this matters for the two-source rule

The hard ≥2-source rule means we frequently corroborate a primary registry against
a secondary doc (gazette, budget book, audit report) that is **scan-only PDF**.
Robust OCR is therefore not optional polish — it is what makes the *second* source
machine-usable for the highest-volume buckets (schools, contractors, projects,
candidates), which are exactly the PDF-locked ones the research flagged. Weak
acquisition = those buckets fail the gate and the 1M expansion stalls.

## Pipeline contract (per document)
1. Fetch (TLS-tolerant; fall back to archive/mirror; record method).
2. Detect type + text layer (born-digital vs scanned vs office format).
3. Normalize: likhit/MarkItDown, with LibreOffice (office) or OCR (scans) upstream.
4. Capture provenance: source URL, fetch method, TLS status, converter path,
   OCR engine + confidence (if used), retrieval date.
5. Emit structured text + provenance to the bucket's extractor.

This provenance block is stored on every sourced entity's attribution, satisfying
the audit/verification requirements in the sourcing program.
