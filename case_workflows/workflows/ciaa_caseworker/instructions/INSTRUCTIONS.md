## Caseworker instructions

You are an expert agentic workflow runner helping build a fact-based database of corruption
cases for Nepal. You are responsible for integral case drafting into the Jawafdehi system.

You will be invoked once per workflow step. Read this file first, then follow the step
prompt provided by the workflow runner for this invocation. Execute **exactly one step**,
then stop. Do not plan or proceed to subsequent steps in the same invocation.

NOTE: Before starting your work, create a run summary file at
`logs/run-summary-<date-time>.md` (use the current ISO 8601 datetime without colons,
e.g. `logs/run-summary-2026-04-04T172904.md`). Record the step you are about to execute.
After completing the step, update the same file with what you completed, any findings,
and the outcome.

## Casework Folder Structure

Your working environment for each case is isolated within a unique case folder. This
folder contains the following structure:

- `logs/`: A directory for run summaries. Each agent invocation writes one
  `run-summary-<date-time>.md` file here.
- `instructions/`: A directory containing these instructions and other reference materials.
- `sources/raw/`: A directory for storing raw source documents (PDFs, HTML files, or
  images) before processing.
- `sources/markdown/`: A directory for storing converted markdown versions of source
  documents.

Any temporary files, drafts, or exported evidence for the case should be generated within
the case folder to keep the workspace organized.


## Collecting important information

### Case data

Use the `ngm_extract_case_data` tool to extract the full case data from the NGM database. You must save it in the casework folder as `case_details-<case_number>.md`.

### CIAA Press releases
Find the CIAA press release for Special court case {{CASE_NUMBER}}.

The CIAA press releases are located at URLs like https://ciaa.gov.np/pressrelease/2000. The press release ID seems to be sorted in ascending order.

Usually, CIAA press release are on the date when the case is filed to special court.

Download and save it at sources/raw/ciaa-press-release-<press-release-id>.pdf

Sample dates:
- https://ciaa.gov.np/pressrelease/1000: मिति २०७६/०१/१२ गते ।
- https://ciaa.gov.np/pressrelease/2000: मिति २०७८/०६/१९ गते ।
- https://ciaa.gov.np/pressrelease/3000: मिति २०८१/१२/१४ गते।

To download the file, use the `download_file` tool (preferred) or the `run_command` tool
(e.g. `run_command("curl -L -o sources/raw/ciaa-press-release-<id>.pdf <url>")`).

> NOTE: CIAA press releases are also available in `data/ciaa-press-releases.csv` (columns: press_id, publication_date, title, source_url). Use `read_file` to read the CSV — do NOT use grep, as grep only returns file paths and not the matching line content. Find the row whose title contains the defendant name and read the source_url field.

Once you find the URL, check for .doc, .docx, or .pdf files in the web page. Use this url to download the file.

### Charge sheet.

Get the charge sheet for Special court case {{CASE_NUMBER}}.

It needs to be collected from Attorney General website located at https://ag.gov.np/abhiyog.

Save it to sources/raw/charge-sheet-<case-number>.pdf

The publication date is usually when CIAA publishes the press release; it's also the same date when the Special court case is registered.

> NOTE: AG charge sheets are also available in `data/ag_index.csv` (columns: case_number, title, filing_date, pdf_url, court_office). Use `read_file` to read the CSV — do NOT use grep, as grep only returns file paths and not the matching line content. Find the row where case_number matches your case number and read the pdf_url field.

Once you identify the year and month, you can use the `download_file` tool or `run_command`
to download the charge sheet, e.g.:

`run_command("curl -L -o sources/raw/charge-sheet-<case-number>.pdf 'https://ag.gov.np/abhiyogpatras?month_id=50&code=sgao&description=undefined'")`

The month ID is determined as follows:
2078 baisakh = 1
2078 Jestha = 2
...
2079 Baisakh = 13
...

### Bolpatra (Procurement Documents)

Get the bolpatra (procurement documents) for the case if applicable.

Bolpatra documents are procurement records from https://www.bolpatra.gov.np that may be referenced in the CIAA press release or charge sheet (abhiyog patra). These are only relevant for procurement-related corruption cases.

#### Step 1: Find IFB/RFP/EOI/PQ Numbers

Read through the CIAA press release and charge sheet to find procurement contract identifiers. Look for patterns like:

- **IFB/RFP/EOI/PQ No:** followed by a number
- **Contract No:** followed by a number
- **Common formats:**
  - `Re-PPHL2/G/NCB/02/2079-80`
  - `NITC/G/NCB-7-2074/75`
  - `ABC/DEF/123/2079-80`

These numbers typically appear in sections describing the procurement contract or project details.

**Note:** In many procurement-related cases, the Contract No listed in the charge sheet is identical to the IFB number (e.g., case 081-CR-0082 had "Contract No: Re-PPHL2/G/NCB/02/2079-80" which was also the IFB). Use your judgment to determine whether the Contract No is suitable to pass to fetch_bolpatra.py; if it returns no results, try variations or note it as unavailable.

**IMPORTANT - Handle Typos and Formatting Errors:**

IFB numbers in press releases or charge sheets may contain typos or formatting errors. For example:
- Document shows: `NITC/G/NCB-7-074/75` (missing digit in year)
- Correct format: `NITC/G/NCB-7-2074/75` (full 4-digit year)

If your initial search returns no results, try variations:
- Add missing digits to years (`074/75` → `2074/75`)
- Try different separators (dashes vs slashes)
- Check for transposed numbers or missing segments

#### Step 2: Download Procurement Documents

Once you have identified the IFB/RFP/EOI/PQ number, use the `fetch` MCP tool to search
bolpatra.gov.np and the `download_file` tool to save each document directly into your
case folder. The `download_file` tool enforces the workspace sandbox automatically.

1. Use `fetch` to search: `https://www.bolpatra.gov.np/egp/` with the IFB number
2. Extract tender IDs and document download URLs from the search results
3. For each document, use `download_file` to save it:
   - Target: `sources/raw/bolpatra-<ifb-number>-<doc-type>.pdf`
   - Example: `sources/raw/bolpatra-NITC-G-NCB-7-2074-75-bid-document.pdf`
4. Convert each downloaded PDF using `convert_to_markdown`:
   - Target: `sources/markdown/bolpatra-<ifb-number>-<doc-type>.md`

If bolpatra.gov.np is unreachable or returns no results, note the unavailability in
`logs/fetch-summary.md` and continue to the next step.

#### Step 3: Try Variations if No Results

If the script returns "No tenders found", try these variations:
- Add full 4-digit year: `NITC/G/NCB-7-2074/75` instead of `NITC/G/NCB-7-074/75`
- Try with 2-digit year: `NITC/G/NCB-7-74/75`
- Check for typos in the original document

#### Step 4: If No Procurement Documents Are Available

If:
- No IFB/RFP/EOI/PQ number is found in the CIAA or charge sheet documents, or
- The search returns no matching tender, or
- The tender exists but no downloadable files are available,

Then document this in the progress log and continue with the next user story. Not all corruption cases involve procurement.

### Court Orders (Faisala)

Get the court order (faisala) document for the case if available.

Use `court_identifier` and `case_number` from the existing case details file.

#### Step 1: Try Direct URL First (fast path)

Try these URLs first using `<court_identifier>` and `<case_number>`:

- `https://ngm-store.jawafdehi.org/uploads/court-orders/<court_identifier>/<case_number>.1.doc`
- `https://ngm-store.jawafdehi.org/uploads/court-orders/<court_identifier>/<case_number>.1.docx`
- `https://ngm-store.jawafdehi.org/uploads/court-orders/<court_identifier>/<case_number>.1.pdf`

If none exist, also try `.2` variants.

#### Step 2: Use Index Traversal Only If Direct URL Fails (fallback)

Start from:
`https://ngm-store.jawafdehi.org/index-v2.json`

Find `court-orders` and follow `$ref`, then traverse:

1. `court-orders`
2. `<court_identifier>`
3. year node:
   - If case number starts with a 3-digit BS year (e.g., `081-CR-0046`), use that as the year node (`081`).
   - Otherwise, derive year from `registration_date_bs`: months 1-3 → year-1, months 4-12 → year; use last 3 digits (e.g., `2081-09-22` → `081`).
   - If still not found, try adjacent years (year-1, year+1), then scan available year nodes for a match.
4. exact case node
5. use `manuscripts[].url` as the exact file URL

#### Step 3: Save and Convert

Save raw file to:
- `sources/raw/court-order-<case-number>.<ext>`

Convert and save markdown to:
- `sources/markdown/court-order-<case-number>.md`

#### Step 4: If Not Found

If both direct URL and index fallback fail, note this in the run summary and continue.

## Fetching news items from Web search

Use the `search` MCP tool to find relevant news articles about the case. This builds context and detail around the allegations.

### Search strategy

Start by planning the first query wave before running any searches. Read the case details, identify the primary accused name(s), romanised name variant, case number, and any project or organization keywords, then write the planned query set into `sources/markdown/news-search-progress.md`.

For each query, call `search` with `engines: ["duckduckgo", "bing", "brave"]` to run all three engines in a single parallel call. Run multiple query variations in parallel in the first wave whenever possible.

Use a balanced batching strategy:
- search query variations in parallel first
- de-duplicate result URLs before fetching
- fetch promising URLs in batches of 6 to 8
- convert fetched pages to markdown in parallel for the same batch
- review quality and stop once you have 6 to 10 strong, source-diverse articles

**Stop rule:** collect enough reporting to cover the case well, then stop. In most cases this means **6 to 10 high-quality articles total**. Do **not** collect more than 10 news articles unless you record a concrete reason in `logs/news-search-summary.md`.

Prefer:
- original reporting over short rewrites
- source diversity across outlets
- articles that add factual detail, procedural updates, or case-specific context

Avoid padding the source set with many near-duplicate filing stories that add no new information.

**Rate limiting:** If you receive rate-limit errors (HTTP 429 or consistently empty results from engines that were previously productive), stop parallel searching immediately, note the affected engine(s) in `sources/markdown/news-search-progress.md`, pause briefly, and switch to single-engine queries with a few seconds between calls. Resume parallel mode only after one or two stable search batches with no repeated rate-limit symptoms.

### Queries to run

For each case, run at least these variations:

- CIAA court case number alone, e.g. `{{CASE_NUMBER}} Nepal`
- Primary defendant name(s) in Nepali + `भ्रष्टाचार` or `विशेष अदालत`
- Defendant name(s) in romanised form + `CIAA corruption Nepal`
- Project or organisation name from the charge sheet + `Nepal`

Appending `Nepal` or `नेपाल` to each query helps surface Nepali-language and Nepal-focused results since the `search` tool has no native region parameter.

Do not wait for one query's results before inventing the next basic variation. The four required variations above should be prepared up front and run in the first parallel search wave.

### Fetching, saving, and cleaning each article

Work in batches instead of strictly one article at a time.

1. Run the planned query wave with `search` using `engines: ["duckduckgo", "bing", "brave"]`.

2. Combine and de-duplicate result URLs from that wave. Select the most promising URLs based on outlet quality, relevance, and source diversity.

3. Fetch the selected URLs in parallel batches of 6 to 8 using `fetchWebContent` (or `fetch`).

4. For the fetched batch, call `convert_to_markdown` in parallel and save to `sources/markdown/news-<source-name>.md`.

5. After the conversion batch finishes, update `MEMORY.md` for the accepted articles from that batch. Use the **original search-result URL** (not any URL scraped from inside the converted file). If a publication date is visible in the page content, record it; otherwise write `unknown`.

   The table must have this exact format:
   ```
   ## News Articles
   | filename | original_url | publication_date | title |
   |---|---|---|---|
   | news-ekantipur.md | https://ekantipur.com/... | 2025-05-11 | सब-इन्जिनियरविरुद्ध... |
   | news-beemapost.md | https://www.beemapost.com/... | unknown | सव-इन्जिनियरद्वारा... |
   ```

6. **Clean the saved markdown** — edit each accepted file in-place to remove all website chrome:
   navigation menus, site headers and footers, logo images, social sharing buttons
   (Facebook, Twitter, Viber, etc.), advertising banners/GIFs, related-article blocks,
   subscribe/login prompts, weather widgets, trending-topics bars, and any other content
   that is not part of the article itself. Keep only: the article headline, dateline/byline,
   and the full article body.

   **Verify the cleaning**: read back the first 5 lines of the file. If any line contains a
   logo or icon `<img`/`![` tag, a nav link list, "Sign In", a social-media button, or a
   subscription prompt — the file is not clean yet; edit again. A clean file's first line must
   be the article headline or dateline (e.g., `# सब-इन्जिनियरविरुद्ध भ्रष्टाचार मुद्दा दायर`
   or `**२७ चैत्र २०८२**`), not a logo or nav element.

   If the publication date was recorded as `unknown` in step 5, re-read the now-clean
   dateline and update the MEMORY.md table row with the correct date.

7. **Identify case-relevant images** — scan the cleaned body text for `![` or `<img` tags
   that appear *inside* the article body (skip logos, icons, and ads). For each image that
   is directly relevant to the case — photos of the accused or co-defendants, official CIAA
   or court document photos, property or crime-scene photos — append an entry to `MEMORY.md`
   under a `## Images` section:
   ```
   ## Images
   - ![Kumar Paudyal](https://example.com/paudyal.jpg) — accused sub-engineer, from ekantipur article
   - ![CIAA press conference](https://example.com/ciaa-press.jpg) — CIAA press conference photo, from onlinekhabar article
   ```
   Skip this step if no relevant images are found in the article.

Reject low-value duplicates early. If two articles add essentially the same filing facts from the same outlet family or syndication chain, keep the stronger one and record the rejection reason briefly in `sources/markdown/news-search-progress.md` or `logs/news-search-summary.md`.

### Progress checkpointing

After every search wave and every fetch/conversion batch, write or update `sources/markdown/news-search-progress.md` with:
- queries run
- number of URLs returned
- number of URLs fetched
- number of accepted and rejected articles
- any duplicate filtering decisions
- any rate-limit issues or engine fallback used

If no news items can be found at all, note it in your run summary, write a brief `sources/markdown/news-search-results.md` explaining what was tried, and continue.

Write a final summary of all found articles to `logs/news-search-summary.md` and update `MEMORY.md` with key learnings for later steps. The final summary should mention the total query count, how many fetch batches were used, whether rate-limit fallback was needed, and why the final article set was sufficient.


## Preparing the Case Draft Locally
We'll create a markdown file called case-draft in the casework folder. It will follow the template added in `instructions/case-template.md` in the casework folder.

Use the jawafdehi-caseworker skill to review this case draft. NOTE explicitly that we won't have a case in Jawafdehi.org, so we will have to make do with the local files that we have. The review should be saved in the usual location, naming it review-<CIAA-case-number>-<date-time>.md.

## Creating a Basic Jawafdehi Case

Use the `create_jawafdehi_case` MCP tool with at minimum `title` and `case_type` (`CORRUPTION`).
You should also pass `short_description` if available.

**Title format:** The title must be in Nepali and end with the CIAA/Special Court case number in parentheses, e.g.:
> नागार्जुन नगरपालिकाका नगर प्रमुख मोहन बहादुर बस्नेतविरुद्ध घुस रिसवत र सम्पत्ति शुद्धीकरण सम्बन्धी भ्रष्टाचार केस ({{CASE_NUMBER}})

The tool returns a JSON object. Record the integer `id` field (e.g. `42`) in `MEMORY.md` as the
**numeric Jawafdehi case ID**. This ID is required for all subsequent patch and upload operations.

After creating, immediately patch the case with `patch_jawafdehi_case` to set `key_allegations`,
`timeline`, `case_start_date`, and `case_end_date` from the draft.

## Creating / Updating Jawafdehi Entities

Use the entity MCP tools to link accused and related parties to the case. For each entity in
the draft (accused, organizations, locations):

1. **Search first** — call `search_jawaf_entities` with the entity's name. If a result matches,
   use its integer `id` and skip creation.

2. **Create if missing** — call `create_jawaf_entity` with either:
   - `nes_id`: the NES entity identifier (e.g. `entity:person/ram-prasad-sharma`) if known
   - `display_name`: a plain name string when no NES record exists
   Record the returned `id` in `MEMORY.md`.

3. **Link to the case** — call `patch_jawafdehi_case` with a JSON Patch `add` operation:
   ```json
   {"op": "add", "path": "/entities/-", "value": {"entity": <id>, "relationship_type": "<type>", "notes": "<notes>"}}
   ```
   Valid `relationship_type` values (always lowercase):

   | Value | Use for |
   |---|---|
   | `accused` | Main defendants / confirmed perpetrators |
   | `alleged` | Named but unconfirmed involvement |
   | `related` | Organizations, third parties, context |
   | `witness` | Witnesses |
   | `victim` | Victims |
   | `opposition` | Opposing parties |
   | `location` | Geographic locations where the case occurred |

   The `notes` field is optional.

   > **System note — LOCATION entities:** In this system, locations are not stored in a
   > separate location model. They are saved as regular Jawaf Entities (just like people and
   > organizations) and linked to the case using `relationship_type: "location"`. This is a
   > deliberate design choice in the Jawafdehi platform.

4. **Confirm** — call `get_jawaf_entity` with the entity ID and verify `related_cases` includes
   this case.

## Updating Remaining Case Details

### Step 1 — Upload source documents

Use `upload_document_source` to create a `DocumentSource` for each raw file. The tool reads the
file directly from disk — pass an absolute `file_path`, not base64 content.

> **Source description vs evidence description:**
> - `description` passed to `upload_document_source` is the **source description** — it describes
>   what the underlying document *is* (its content, origin, and key metadata).
> - `description` in the evidence JSON Patch operation (Step 2) is the **evidence description** —
>   it explains *how* this particular source connects to and supports this specific case.
>
> **Language:** Both descriptions must be written in **Nepali**. Technical terms, proper nouns,
> case numbers, URLs, and numeric values may remain in English or their original form.
>
> Example for a charge sheet:
> - Source description: `"अख्तियार दुरुपयोग अनुसन्धान आयोगद्वारा विशेष अदालतमा दायर गरिएको अभियोग पत्र — मुद्दा {{CASE_NUMBER}}, मिति २०८१-०५-१५, प्रतिवादी Ram Prasad Sharma"`
> - Evidence description: `"यो अभियोग पत्रले घुसखोरीको आरोप र रु. ९.२२ करोडको बिगो रकम पुष्टि गर्दछ।"`

**Supported extensions for raw sources**: `.pdf`, `.doc`, `.docx`, `.jpg`, `.jpeg`

For each file in `sources/raw/` with one of those extensions, call `upload_document_source`:

| filename prefix         | `source_type`        | `description` required? | `publication_date` required? |
|-------------------------|----------------------|-------------------------|------------------------------|
| `ciaa-press-release-*`  | `CIAA_PRESS_RELEASE` | Yes                     | No                           |
| `charge-sheet-*`        | `AG_ABHIYOG_PATRA`   | Yes                     | No                           |
| `court-order-*`         | `COURT_ORDER`        | Yes                     | No                           |
| `bolpatra-*`            | `MISC`               | Yes                     | No                           |

For news articles (`sources/markdown/news-*.md`), call `upload_document_source` with:
- `source_type`: `NEWS`
- `file_path`: absolute path to the cleaned `.md` file
- keep the uploaded `.md` transcript as the uploaded file attachment
- `url`: `["<original_article_url>"]` — use the URL recorded in `MEMORY.md ## News Articles`
  for this file. **Do NOT scrape URLs from inside the markdown file itself** (they will be
  logo or navigation links, not the article URL).
- the external `url` field must preserve the **original article URL**. The markdown transcript path is **not** the external URL.
- `publication_date`: `YYYY-MM-DD` — use the date recorded in `MEMORY.md ## News Articles`.
  If it was recorded as `unknown`, re-read the first 10 lines of the cleaned file and extract
  the dateline. **Do not substitute the case filing date** when the real date is unknown — leave
  the field blank instead.
- `description`: summarise the article's specific claim about this case in one sentence, **in
  Nepali**. To do this, read the first 200 words of the cleaned article body. Example:
  `"Ekantipur (२०८२-०१-२८) ले सव-इन्जिनियर कुमार पौड्यालविरुद्ध रु. ३.६३ करोडको अकुत सम्पत्ति आर्जन सम्बन्धी अख्तियारको मुद्दाबारे समाचार प्रकाशित गरेको।"`

Record every returned `source_id` and its description in `MEMORY.md`.

Before leaving the upload step, verify for each news `DocumentSource` that:
- the uploaded file is the cleaned markdown transcript
- the external `url` field contains the original article URL
- the source description is written mainly in Nepali

### Step 2 — Attach evidence to the case

After all uploads, call `patch_jawafdehi_case` with one RFC 6902 `add` operation per source:

```json
{"op": "add", "path": "/evidence/-", "value": {"source_id": "<source_id>", "description": "<description>"}}
```

**Every uploaded source must be added as an evidence entry** — including all news articles,
official documents, and court orders. Do not selectively attach only a subset.

For **official documents** (charge sheet, press release, court order), the evidence description
should state specifically which allegation(s) or facts the document proves, **in Nepali**.

For **news articles**, the evidence description should name the outlet, publication date, and
which allegation(s) the article corroborates, **in Nepali**. Example:
`"Ekantipur (२०८२-०१-२८) ले अभियोग दायरी र रु. ३.६३ करोडको बिगो रकम पुष्टि गर्दछ।"`

If a source description or evidence description is mostly in English when Nepali is practical, treat that as a quality problem and fix it before completing the step.

Combine all evidence `add` ops into a single `patch_jawafdehi_case` call.

### Step 3 — Update remaining case fields

Call `get_jawafdehi_case` first to see which fields are already populated. Then patch whatever
is missing from `draft.md`:

- `/timeline` — list of `{"date": "<ISO date AD>", "title": "...", "description": "..."}` objects
- `/short_description` — one-sentence teaser
- `/description` — full Nepali HTML description
- `/tags` — list of English tags
- `/key_allegations` — list of allegations
- `/case_start_date`, `/case_end_date` — ISO 8601 dates
- `/court_cases` — list of strings in `"{court_identifier}:{case_number}"` format, e.g. `["special:{{CASE_NUMBER}}"]`. Read `court_identifier` and the case number from the NGM case data file (`case_details-*.md`). Omit if `court_identifier` is unknown.
- `/bigo` — integer NPR amount from the **Bigo Amount** field in `draft.md`. Must be a plain integer (no commas or currency symbols), e.g. `15880000`. Omit if blank or unknown.
- `/missing_details` — freetext string compiled from the unchecked items in the **Missing Details** section of `draft.md`. Omit if all items are checked or the section is empty.

Combine all field updates into a single `patch_jawafdehi_case` call.

### Recommended tags

Use a small, consistent set of English tags. Prefer the most relevant tags rather than tagging everything.

Common core tags:
- `CIAA`
- `Special Court`
- `Corruption`

Use one or more allegation/context tags when applicable:
- `Illegal Property Acquisition`
- `Bribery`
- `Procurement Irregularities`
- `Public Office Abuse`
- `Witness Tampering`
- `Forged Documents`

Use sector or institution tags when they help identify the case context:
- `Local Government`
- `Municipality`
- `Kathmandu Metropolitan City`
- ministry, department, project, or public body name in English when it is central to the case

Use location tags sparingly and only when they add search value:
- district or city names in English such as `Kathmandu`
- broader geographic tags only if they are directly relevant

Avoid:
- redundant synonyms
- very generic tags that add no filtering value
- more than about 5 to 8 tags unless the case genuinely spans multiple distinct contexts

### Step 4 — Patch case notes with images

If `MEMORY.md` contains a `## Images` section with at least one entry, patch the case notes
field with the collected image list:

```json
{"op": "replace", "path": "/notes", "value": "## Relevant Images\n\n- ![Caption](url) — context\n..."}
```

Skip this step if no images were recorded in `MEMORY.md ## Images`.
