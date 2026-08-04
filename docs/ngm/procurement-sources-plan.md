# Plan: Bolpatra / PPMO procurement extraction → Jawafdehi Materials

> Companion to the OCR company scraper (`entities/sourcing/ocr/`). OCR gives us
> *who the companies are*; procurement gives us *which win government money*.
> Modeled as **Materials** (user decision), POSTed to `/api/materials/`.

## Context & verified reconnaissance (live, 2026-07-31)

Nepal's e-GP lives at **`bolpatra.gov.np/egp/`** — a **server-rendered legacy
Java/Struts app** (jQuery, `*.action` routes), **NOT** a JSON API, with a **broken
TLS chain** (crawler needs `verify=False`). Confirmed public + enumerable:

- **Search:** `POST /egp/searchOpportunity.action` — paginated HTML table
  (`searchBidsByCriteria('searchBidForm', <pageIndex>)`; form fields
  `currentPageIndex`, `pageSize`, `startIndex`, `pageAction`). Columns: Public
  Entity, IFB/RFP/EOI/PQ No., Project Title, Category, Method, Publication Date,
  Deadline.
- **Scale (from the form's own hidden state):** `totalRecords=204925` tenders over
  `numberOfPages=2050`. Tender ids are **dense from 1 to ~322k** (probed 1, 100,
  1k, 10k, 50k, 100k, 200k, 300k, 321k — all present).
- **Pager is unreliable for enumeration (tuned 2026-07-31).** The server ignores
  `currentPageIndex`, requires `pageAction=next` for `pageSize` to apply at all
  (else it returns its default 10 rows), mirrors state into `*Input` fields, and
  re-bases `startIndex` — different offsets can return overlapping/repeated pages.
  ⇒ **the id-walk owns completeness; `--discover-pages` is a freshness seed only**
  (it now stops as soon as a page adds nothing new). With the corrected params
  discovery yields a true 100 ids/page.
- **Empty-shell trap (fixed):** a non-existent `tenderId` returns the **form shell
  with all labels and no values**. The first parser read the *next label* as the
  value (`procuringEntity="Procurement Category"`) — which would have written
  garbage at scale. The parser now refuses to accept a known label as a value and
  returns `None` → the id is recorded as a **gap**. Regression-tested.
- **Detail:** `POST /egp/getTenderDetails` with `tenderId=<int>` — integer IDs
  (~319,751–322,009 live now → an **enumerable frontier like OCR**). Returns a
  label/value HTML page: Public Entity, Procurement Category (Goods/Works/
  Consultancy), Method (NCB/ICB/…), IFB/RFP/EOI/PQ No, Project Name, **Current
  Status** (e.g. "Bid Published"), Source of Funds, Publication Date, brief
  description, full Bid Schedule (pre-bid/submission/opening dates+addresses),
  fees. (Verified against real tenders 319751 / 321065.)

**Award/winner reality (the honest limit):** the e-GP **public** surface exposes
tender *notices*, not structured **award results** (winning firm + contract
amount). No public award-search route exists (probed — all 404). Awards are
published as **documents**: the PPMO **Procurement Bulletin (खरिद पत्रिका)** and
**annual reports (वार्षिक प्रतिवेदन)** as PDFs on `ppmo.gov.np`. So notices and
awards need **two different mechanisms**.

## Two tracks

### Track A — Bid notices (build now; high confidence)

Package **`materials/sourcing/bolpatra/`** (mirrors `materials/sourcing/nkp/`):

- **`crawl.py`** — TLS-tolerant (`verify=False`), resumable `getTenderDetails`
  integer-ID walk (+ paginated search to discover the live ID range / seed the
  frontier). Same politeness contract as the OCR crawler (small concurrency,
  jittered delay, backoff, `--max-requests`, JSONL checkpoint). HTTP client of the
  material API — POSTs to `/api/materials/`, never ORM-direct (sourcing rule).
- **`parse.py`** — pure `parse_tender_detail(html) -> ParsedTender`: label→value
  extraction (the structure is verified above). Reuses
  `courts.scraper.text.normalize_whitespace`.
- **`shaper.py`** — pure `tender_to_jsonld(tender) -> (doc, material_type)`:
  - New `MaterialType.PROCUREMENT_NOTICE = "procurement_notice"` +
    `_TYPE_MAP` entry `("CreativeWork", "jawafdehi:ProcurementNotice")`
    (follows the existing PRECEDENT/PRESS_RELEASE pattern in `materials/jsonld.py`).
  - `@id` via `build_material_iri("bolpatra", <tenderId>)`.
  - Fields → `name` (project title, ne/en), `jawafdehi:noticeNumber`,
    `jawafdehi:procuringEntity`, `jawafdehi:procurementCategory`,
    `jawafdehi:procurementMethod`, `jawafdehi:currentStatus`,
    `jawafdehi:sourceOfFunds`, `datePublished` (AD from the notice date),
    bid-schedule dates as `jawafdehi:*`. `url` + `sources:[{authority:
    "bolpatra.gov.np"}]`.
  - **Linkage:** when the procuring entity or (later) a bidder maps to a known
    entity `@id`, add `about: [{"@id": <iri>}]` (the materials pattern) — this is
    the hook that ties procurement to OCR company entities.
- **Tests** (mirror `materials/tests/test_*_shaper.py` +
  `test_scrape_ciaa_press_command.py`): pure parse of recorded detail HTML; pure
  shaper → `validate_material_jsonld`; crawler control-flow with fake fetch +
  fake material client via `build_*` seams.

### Track B — Contract awards (via likhit; document-based)

Awards aren't a clean feed, so per the user decision we ingest the **source
documents** and transcribe them:

- Crawl the PPMO **Procurement Bulletin** + **annual report** listings
  (`ppmo.gov.np/category/...`) for their PDF attachments.
- Convert each PDF → Markdown with **`likhit`** (Jawafdehi's Nepali-PDF MarkItDown
  plugin — already used by `review/converter.py`; `_patch_likhit_ocr_dpi()` for
  Devanagari OCR). ⚠️ **likhit is the optional `bigo-enrichment` extra and is NOT
  installed in this workspace** — Track B needs `uv sync --extra bigo-enrichment`
  to run locally.
- Shape each as a `MaterialType.OFFICIAL_REPORT` Material: PDF as
  `associatedMedia` (via `media_objects_from_document_sources`), the transcript in
  the language-tagged `text` field (search-indexable), `datePublished` from the
  issue. Structured winner/amount extraction from the transcript is a **later**
  step; this captures the authoritative source + transcript as-is.

## Sequencing

- **Now:** build + verify Track A against live e-GP and the local SQLite materials
  API (bolpatra is up).
- **Background:** keep retrying OCR's API (currently 401 — their CAMIS gateway
  token is expired server-side) to finish the company crawl.
- **Then:** Track B (needs the `bigo-enrichment` extra for likhit).

## Verification

1. Unit: `uv run pytest materials/tests/test_bolpatra_*` (pure parse + shaper +
   crawler flow; no net/DB).
2. Live smoke: `python -m materials.sourcing.bolpatra.crawl --dry-run
   --id-min 319000 --id-max 319100` → inspect the JSONL cache.
3. E2E local: POST to the local `/api/materials/`; `GET` them back; re-run →
   idempotent upsert-by-`@id` (materials upsert IS idempotent, unlike entities).
