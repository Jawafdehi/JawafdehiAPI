---
name: jawafdehi-document-reader
description: Reads documents in the Jawafdehi archive — CIAA and Auditor General annual reports, audit reports, charge sheets, court orders, law-journal issues — by navigating to one page range, table or chart instead of loading the whole file. Use when answering a question from an archived report or PDF, looking up a figure in a government table, or citing a document in the Jawafdehi corpus.
---

# Reading Jawafdehi documents

## Contents

- Call order and workflow
- Worked example
- Rules you must not break
- Choosing a source
- Finding a document
- Held vs read
- Caps
- If the tools are missing

## Call order and workflow

Five tools, in this order. Everything before the last step is cheap; only
`jawafdehi:read_document_part` returns document text.

| Tool | Returns |
|---|---|
| `jawafdehi:list_document_sources` | every source, its size, and how much has been read |
| `jawafdehi:get_document_overview` | one document: counts, readability, provenance, PDF links |
| `jawafdehi:list_document_contents` | its page map or table captions — no page text, no table markdown |
| `jawafdehi:search_document` | where a term appears inside it: mixed page/table/figure hits with snippets |
| `jawafdehi:read_document_part` | one slice: a page range, one table, or one figure |

**Never request a whole document.** The largest are a few hundred pages of Devanagari —
one Auditor General report is 3.96 MB of JSON, roughly 600k–900k tokens.

Copy this checklist and tick items off:

```
Document lookup:
- [ ] 1. Identify the source  (jawafdehi:list_document_sources)
- [ ] 2. Get the document IRI (jawafdehi:search_control_plane)
- [ ] 3. Check it is readable (jawafdehi:get_document_overview)
- [ ] 4. Find the right part  (jawafdehi:search_document — or
         jawafdehi:list_document_contents to browse the index)
- [ ] 5. Read ONE part        (jawafdehi:read_document_part)
- [ ] 6. Verify any figure against the source PDF before quoting it
```

**Step 4 has two doors, and picking the wrong one wastes calls.** Use
`jawafdehi:search_document` when you know *what you are looking for* — it searches the page
text, the table captions AND their rows, and the figure titles and notes, and hands back the
page number and key to read next. Use `jawafdehi:list_document_contents` when you want to
*browse* — to see the shape of a document, or to scan all 145 captions at once.

A question about a number is almost always answered by **a table**, not by prose. But the
index reaches a table only through its CAPTION, so if the subject sits in the rows — or in a
paragraph — searching finds it and scanning captions does not. Never page through a long
report hunting for a term; that is what step 4's search is for.

## Worked example

**Question:** "How many complaints did the CIAA register in 2081/82, and how did they arrive?"

**Step 1** — `jawafdehi:list_document_sources` → the shelf is `ciaa_annual_report`,
41 documents.

**Step 2** — `jawafdehi:search_control_plane` with `source=ciaa_annual_report` → the
2081/82 report's IRI. **Every later call needs it:**

```
iri = https://jawafdehi.org/material/ciaa_annual_report/ee0b4f80b24b8665
```

**Step 3** — `jawafdehi:get_document_overview(iri=iri)`:

```json
{"iri": "https://jawafdehi.org/material/ciaa_annual_report/ee0b4f80b24b8665",
 "readable": true,
 "source_links": [{"role": "RAW", "url": "https://ngm-store.jawafdehi.org/…pdf"},
                  {"role": "SOURCE_PAGE", "url": "https://ciaa.gov.np/singlePublications/219"}],
 "content": {"pages": 446, "pages_read": 433, "tables": 145, "figures": 46,
             "provenance": {"doc_id": "ciaa-2081-82", "transcript_verdict": "clean"}}}
```

446 pages, 13 of them unreadable. 145 tables — far too many to read.

**Step 4** — `jawafdehi:list_document_contents(iri=iri, kind="table")` → scan captions:

```json
{"key": "p0030-t1", "page_no": 30, "n_rows": 9, "n_cols": 8,
 "caption": "प्राप्तिको माध्यमका आधारमा उजुरीको विवरण"}
```

That caption is "complaints by means of receipt" — the one.

**Step 5** — `jawafdehi:read_document_part(iri=iri, table="p0030-t1")`:

```
| क्र.सं. | उजुरीको माध्यम | उजुरी सङ्ख्या | प्रतिशत |
|  | 1. वेबसाइट        | 10,245 27.67 |
|  | 2. लिखित निवेदन   | 10,180 27.49 |
```

**Step 6** — note `10,245 27.67` is two values merged into one cell. Open the `RAW` link
(the archived PDF) or `SOURCE_PAGE` (the publisher's own page) and confirm page 30 before
stating either number.

Five calls, under 20 KB, one citable table.

### The other door: when the caption will not help

**Question:** "What does the 2081/82 report say about bribery?"

No caption contains that. Scanning 145 of them finds nothing, and the report is 446 pages.
Search instead — `jawafdehi:search_document(iri=iri, q="घुस")`:

```json
{"total": 7, "count": 7, "truncated": false,
 "hits": [
   {"kind": "page",  "page_no": 294, "key": null, "field": "text",
    "match": "literal", "coverage_state": "anchored",
    "snippet": "…३.५.१ घुस (रिसवत) सम्बन्धी मुद्दा आयोगले आर्थिक वर्ष…"},
   {"kind": "table", "page_no": 294, "key": "p0294-t1", "field": "caption",
    "match": "literal",
    "snippet": "घुस (रिसवत) सम्बन्धी मुद्दाका प्रतिवादीको विवरण"}]}
```

The prose and the table that belongs to it, together, in page order. Then
`jawafdehi:read_document_part(iri=iri, page=294)` or `(iri=iri, table="p0294-t1")`.

Romanized queries work (`q="ghus"` finds घुस). A plain number addresses a **page** and is
not looked for in the text — `q="294"` returns what is on page 294, not every mention of
the digits 294 — and `q="ujuri 294"` is that word on page 294 only. Devanagari numerals
(`q="१९४८८"`) are ordinary text and are searched as such. Check `match` before quoting,
per the rules below.

## Rules you must not break

**Address tables and figures by `key`, never by `ordinal`.** A key (`p0030-t1` = page 30,
first table on it) describes where the item physically sits. Ordinals are recomputed every
time a document is re-parsed, so the same ordinal later means a *different* table and a
saved reference silently rots.

**An empty `text` is never evidence that a page was blank.** Read `coverage_state` first.
A genuinely blank page and a page our OCR failed on look identical in the text field and
mean opposite things.

**Verify every figure against the source PDF before publishing it.** This corpus contains
OCR artefacts that nothing in the response flags, on pages marked `anchored` with
`anchor_fidelity: tight` — digits mixed across scripts (`3७,026`), and adjacent values merged into
one cell (`10,245 27.67`). The quality fields describe whether we *read* the page, not
whether the digits are right.

**`truncated: true` means your answer is incomplete.** Narrow the range or fetch one item
at a time. Never summarise a clipped payload as though it were whole. On a search result
the same flag means you are seeing the first hits **by page order, not the best ones** —
`total` is how many the document really holds.

**A search hit's `match` says how strong it is, and they are not interchangeable.**
`literal` is the term as you typed it. `phonetic` matched by SOUND — a Latin-keyboard query
reaching Devanagari (`ghus` → घुस) — and may well be a different word, so read the snippet
before trusting it. `folio` matched the page NUMBER — you asked for a page, so the snippet is just the opening
of it and says nothing about whether your term appears. Quote only what the snippet
actually shows, and never report a phonetic or folio hit as the document using that term.

**A snippet is a window, not a sentence.** It is ~190 characters around the match, and an
ellipsis marks each end that was cut. Fetch the page or key with
`jawafdehi:read_document_part` before quoting anything from it.

**On a chart, `estimated: true` means the value was read off the image**, not from a
printed number. Never present it as equivalent to a printed figure.

**Cite the document IRI plus the page number or key**, and link `SOURCE_PAGE` from
`jawafdehi:get_document_overview`. An uncheckable quote from an accountability archive is worthless.

## Choosing a source

Source tokens are not self-explanatory, and two are a trap:

> `ag` is the **ATTORNEY** General — 99,750 charge sheets.
> The **AUDITOR** General is `official_report`.

Take the token from `jawafdehi:list_document_sources`, which carries the publisher. Never
infer a source from how its token is spelled.

A source with `labelled: false` has no reviewed description — treat it as unknown rather
than guessing from the token.

One source can hold several series. `official_report` is 223 documents across 15 of them:
annual reports (18), province audit reports (56), audit bulletins (29), the audit journal
(21), and more. "The Auditor General's annual report" is one series, not the whole token.

**Narrow to a series with `dataset_bucket`.** `jawafdehi:list_document_sources` returns each
series with its bucket token and count; pass that token to
`jawafdehi:search_control_plane` alongside the source. Searching `official_report` alone
returns province audits and journal issues next to the annual reports, and nothing in the
results distinguishes them.

## Finding a document

There is no search *inside* documents and no way to filter by fiscal year. Use
`jawafdehi:search_control_plane` or `jawafdehi:browse_materials` to get an IRI.

If you cannot find the document that answers the question, say so. Do not substitute a
different year's report because it was easier to reach.

## Held vs read

Holding a document is not having read it; most of the archive has never been transcribed.

| Signal | Meaning |
|---|---|
| `jawafdehi:get_document_overview` has no `content` | Not parsed. Nothing in it can be quoted — cite it as a holding, or link the PDF. |
| `pages_read` < `pages` | Some pages failed OCR; their text returns empty. |
| `readable: 0` on a source | We hold those documents and have transcribed none. |
| `readable: null` | Not computed yet. This is **not** zero. |

Per page, `coverage_state` is the upstream's vocabulary and can grow. Observed in this
corpus: `anchored` (read from the PDF's own text layer — strongest), `blank`, `folded`.
The upstream also counts `ocr_vision` (read by a vision model: has text, weaker claim),
`gap` and `ocr_unread`. Treat an unfamiliar value as "not plainly read".

## Caps

`jawafdehi:read_document_part` returns at most 20,000 characters per call across the whole
response. A page range covering more than 50 pages is refused outright rather than silently
cut.

`jawafdehi:search_document` returns at most 50 hits per call and needs no character cap —
every hit is a short snippet, so a search of the largest report in the archive is a few
thousand characters. `total` always reports the true count, whatever `limit` you pass.

## If the tools are missing

The MCP serves unauthenticated callers a small read-only catalogue and never prompts for
login, so these five will simply be absent. Call `jawafdehi:get_current_user`: "No
authenticated user; public read-only access" means you are anonymous. Reconnecting does not
fix it — see `docs/mcp/README.md`.

An authenticated caseworker or admin can read `PRIVATE` documents, including draft-case
evidence. What you can read is not necessarily what a reader could, and it must not be
quoted publicly.
