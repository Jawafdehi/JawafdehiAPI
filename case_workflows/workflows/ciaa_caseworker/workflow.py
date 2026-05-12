"""
CIAA Caseworker workflow — processes Special Court cases from the NGM
database and creates Jawafdehi accountability cases.

Template location: ``case_workflows/workflows/ciaa_caseworker/``

Security note
-------------
This workflow exposes a ``run_command`` tool that executes arbitrary shell
commands inside the agent's working directory.  This is intentional: the
workflow is designed to run in a **trusted, isolated environment** (e.g. a
dedicated Cloud Run job with no internet-accessible services and no persistent
credentials beyond what the job needs).

DO NOT enable this workflow on shared hosts or attach it to user-facing
processes.  The tool is deliberately sandboxed to the allowed work directory,
but shell execution is inherently powerful and must be treated accordingly.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, List

from langchain_core.tools import tool

from case_workflows.encoding_tool import create_fix_encoding_tool
from case_workflows.registry import register
from case_workflows.workflow import Workflow, WorkflowStep

if TYPE_CHECKING:
    from case_workflows.models import CaseWorkflowRun
    from cases.models import Case

logger = logging.getLogger(__name__)

_WORK_DIR_ENV = "JAWAFDEHI_ALLOWED_WORK_DIR"
TEMPLATE_DIR = Path(__file__).resolve().parent

_SYSTEM_PROMPT = """\
You are an expert caseworker helping build a fact-based database of corruption \
cases for Nepal. Your role is to process CIAA Special Court corruption cases and \
prepare them for the Jawafdehi platform.

Work methodically and thoroughly. The work directory is provided at the start of \
each task as an absolute path. Always use absolute paths for all file operations, \
including when calling MCP tools that accept a file_path argument.

Work directory structure:
- <work_dir>/MEMORY.md           — agent memory: learnings and notes that persist across steps
- <work_dir>/instructions/      — casework instructions and case draft template
- <work_dir>/data/              — reference CSVs (ciaa-press-releases.csv, ag_index.csv)
- <work_dir>/sources/raw/       — downloaded raw source files (PDFs, HTML, etc.)
- <work_dir>/sources/markdown/  — markdown-converted versions of source documents
- <work_dir>/logs/              — run summaries and working notes

YOU ARE ONLY ALLOWED TO READ AND WRITE FILES WITHIN THE WORK DIRECTORY. Do not attempt to \
access files outside the work directory. Always construct absolute file paths based on the provided work directory path. If you need to call an MCP tool that reads or writes files, use the provided work directory as the base path for any file_path arguments.

Always read the instructions in instructions/INSTRUCTIONS.md, as well as the case details in case_details*.md (if it exists).


You do NOT require to create empty ".keep" or similar files to keep directories — the system will preserve any directories you create within the work directory, even if they are empty.

Update MEMORY.md in the case folder with any learnings or notes that should persist across steps. This is useful for recording discovered facts, insights, and reminders for later steps. And load MEMORY.md at the start of each step to retain context across steps.
"""


@tool
def download_file(url: str, output_path: str) -> str:
    """Download a file from a URL and save it to disk.

    Args:
        url: The URL to download from. Supports http and https. Redirects are
            followed automatically.
        output_path: Absolute path where the downloaded file will be saved.
            Parent directories are created if they do not exist.

    Returns:
        A message reporting the number of bytes written, or an error description.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return f"Unsupported URL scheme for download: {parsed.scheme or '(none)'}"

        out = Path(output_path).resolve()
        allowed_work_dir = os.environ.get(_WORK_DIR_ENV)
        if not allowed_work_dir:
            return f"download_file requires {_WORK_DIR_ENV} to be set"
        base = Path(allowed_work_dir).resolve()
        if not base.is_dir():
            return f"Configured work directory is invalid: {base}"
        try:
            out.relative_to(base)
        except ValueError:
            return (
                f"Output path is outside allowed work directory: {out} (base: {base})"
            )

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; jawafdehi-caseworker/1.0)"
            },
        )
        with urllib.request.urlopen(req) as response:
            data = response.read()

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return f"Downloaded {len(data):,} bytes to {out}"
    except urllib.error.HTTPError as exc:
        return f"HTTP error {exc.code} downloading {url}: {exc.reason}"
    except urllib.error.URLError as exc:
        return f"URL error downloading {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return f"Error downloading {url}: {exc}"


# Instantiate the fix_encoding tool for use in all steps
fix_encoding = create_fix_encoding_tool()


@tool
def run_command(command: str) -> str:  # noqa: S603
    """Run a shell command inside the allowed work directory.

    .. danger::
        This tool executes **arbitrary shell commands**.  It is intentionally
        included because this workflow runs in a **trusted, isolated
        environment** (e.g. a dedicated Cloud Run job with no
        internet-accessible services and no persistent credentials beyond
        what the job needs).  DO NOT enable this workflow on shared hosts or
        attach it to user-facing processes.

    The command is executed with ``shell=False`` via ``shlex.split`` to
    reduce accidental glob expansion and word-splitting surprises, but it
    still has full access to the filesystem paths reachable from the
    allowed work directory.

    Args:
        command: The shell command to run (e.g. ``python3 fetch_bolpatra.py
            --ifb 081-CR-0046``).  The working directory is set to
            ``JAWAFDEHI_ALLOWED_WORK_DIR``.

    Returns:
        Combined stdout + stderr output (truncated to 8 KB if very long),
        or an error description if the command could not be started or
        timed out.
    """
    allowed_work_dir = os.environ.get(_WORK_DIR_ENV)
    if not allowed_work_dir:
        return f"run_command requires {_WORK_DIR_ENV} to be set"
    work_dir = Path(allowed_work_dir).resolve()
    if not work_dir.is_dir():
        return f"Configured work directory is invalid: {work_dir}"

    try:
        args = shlex.split(command)
    except ValueError as exc:
        return f"Invalid command syntax: {exc}"

    try:
        result = subprocess.run(  # noqa: S603
            args,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after 300 s: {command}"
    except FileNotFoundError:
        return f"Command not found: {args[0]!r}"
    except Exception as exc:  # noqa: BLE001
        return f"Error running command: {exc}"

    output = result.stdout + result.stderr
    _TRUNCATE = 8 * 1024
    if len(output) > _TRUNCATE:
        output = output[:_TRUNCATE] + f"\n... [truncated — {len(output):,} bytes total]"
    return output or f"(exit code {result.returncode}, no output)"


# MCP stdio server config for fetch tool
_FETCH_SERVER = {
    "fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch", "--ignore-robots-txt"],
        "transport": "stdio",
    }
}

# MCP stdio server config for open-websearch (multi-engine news search)
_OPEN_WEBSEARCH_SERVER = {
    "open-websearch": {
        "command": "npx",
        "args": ["-y", "open-websearch@latest"],
        "transport": "stdio",
        "env": {"MODE": "stdio", "DEFAULT_SEARCH_ENGINE": "duckduckgo"},
    }
}


def _jawafdehi_server() -> dict:
    token = os.environ.get("JAWAFDEHI_API_TOKEN")
    if not token:
        raise RuntimeError(
            "JAWAFDEHI_API_TOKEN environment variable is not set. "
            "Set it to your Jawafdehi API token before running this workflow."
        )

    env = {"JAWAFDEHI_API_TOKEN": token}

    if "JAWAFDEHI_API_BASE_URL" in os.environ:
        env["JAWAFDEHI_API_BASE_URL"] = os.environ["JAWAFDEHI_API_BASE_URL"]

    return {
        "jawafdehi": {
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/Jawafdehi/jawafdehi-mcp.git",
                "jawafdehi-mcp",
            ],
            "transport": "stdio",
            "env": env,
        }
    }


def _render_instruction_tokens(case_dir: Path, case_number: str) -> None:
    """Replace instruction placeholder tokens with runtime case values."""
    instructions_dir = case_dir / "instructions"
    if not instructions_dir.is_dir():
        return

    for md_file in instructions_dir.rglob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        rendered = text.replace("{{CASE_NUMBER}}", case_number)
        if rendered != text:
            md_file.write_text(rendered, encoding="utf-8")


def _extract_ciaa_case_number(case: "Case") -> str:
    """Return the unique Special Court case number from Case.court_cases."""
    special_case_numbers: list[str] = []

    for ref in case.court_cases or []:
        if not isinstance(ref, str) or ":" not in ref:
            continue
        court_identifier, case_number = ref.split(":", 1)
        if court_identifier == "special" and case_number.strip():
            special_case_numbers.append(case_number.strip())

    if len(special_case_numbers) != 1:
        raise ValueError(
            "Expected exactly one special court case reference for "
            f"case {case.case_id}; found {len(special_case_numbers)} "
            f"in court_cases={case.court_cases!r}"
        )

    return special_case_numbers[0]


@register
class CIAACaseworkerWorkflow(Workflow):
    """
    Workflow for processing CIAA Special Court corruption cases.

    Steps:
    1. initialize-casework      — verify eligibility, extract NGM case data, setup directories
    2. fetch-source-documents   — download CIAA press release, charge sheet, bolpatra; convert to markdown
    3. fetch-news-articles      — web search for news articles
    4. draft-case               — prepare local case draft
    5. create-case              — create a basic Jawafdehi case via the API (or patch existing in update mode)
    6. create-update-entities   — create or update Jawafdehi entities linked to the case
    7. update-case-details      — populate remaining case details collected during research
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.update_existing: bool = False

    @property
    def workflow_id(self) -> str:
        return "ciaa_caseworker"

    @property
    def display_name(self) -> str:
        return "CIAA Caseworker"

    def get_work_dir(self, run: "CaseWorkflowRun") -> Path:
        """Use CIAA Special Court case number (not Jawafdehi case_id) as directory."""
        base_dir = super().get_work_dir(run).parent

        if run.case_id.startswith("case-"):
            from cases.models import Case

            case = Case.objects.only("case_id", "court_cases").get(case_id=run.case_id)
            ciaa_case_number = _extract_ciaa_case_number(case)
        else:
            ciaa_case_number = run.case_id

        return base_dir / ciaa_case_number

    @property
    def steps(self) -> List[WorkflowStep]:
        # _jawafdehi_server() is called lazily inside each step that needs it.
        # This allows `workflow.steps` to be accessed (e.g. for listing or
        # testing) without requiring JAWAFDEHI_API_TOKEN to be set at property
        # access time.  The token is only checked when a step is actually
        # executed by the workflow engine.

        def _jserver() -> dict:
            return _jawafdehi_server()

        return [
            WorkflowStep(
                name="initialize-casework",
                prompt_fn=lambda case_dir: f"""\
The Jawafdehi case ID is: {case_dir.name}

First read {case_dir}/instructions/INSTRUCTIONS.md for detailed caseworker guidance before proceeding.

Verify this case exists in the NGM database and is eligible for processing as a CIAA Special Court corruption case. Use the ngm_extract_case_data MCP tool with file_path={case_dir}/case_details-{case_dir.name}.md. Ensure the required directories exist: {case_dir}/sources/raw/, {case_dir}/sources/markdown/, {case_dir}/logs/. Write a brief initialization note to {case_dir}/logs/case-summary.md summarising the case number and some basic case details. For example, note the defendant name(s), registration date (both in BS and in AD), and any other key details you find. This information will be useful for searching for related documents and news articles in later steps.
""",
                mcp_servers_fn=_jserver,
                mcp_tool_filter=["ngm_extract_case_data"],
                system_prompt=_SYSTEM_PROMPT,
            ),
            WorkflowStep(
                name="fetch-source-documents",
                prompt_fn=lambda case_dir: f"""\
The Jawafdehi case ID / court case number is: {case_dir.name}

Step 1 — Read {case_dir}/case_details-{case_dir.name}.md to get the defendant name (primary defendant) and registration date.

Step 2 — Find the AG charge sheet URL:
  Use read_file and/or grep on {case_dir}/data/ag_index.csv. The CSV columns are: case_number,title,filing_date,pdf_url,court_office. Find the row where case_number = {case_dir.name} and read the pdf_url value from that row. Use the download_file tool to download the PDF to {case_dir}/sources/raw/charge-sheet-<case-number>.pdf, then convert to {case_dir}/sources/markdown/charge-sheet-<case-number>.md.

Step 3 — Find the CIAA press release URL:
  Use read_file and/or grep on {case_dir}/data/ciaa-press-releases.csv. The CSV columns are: press_id,publication_date,title,source_url. Find the row whose title contains the primary defendant name and read the source_url. The source_url is the press release page (e.g. https://ciaa.gov.np/pressrelease/N). Use `fetch` tool to view the page contents, and locate the actual press release (PDF or HTML or .jpg) from the this page. Then use the download_file tool to download the page to {case_dir}/sources/raw/ciaa-press-release-<press-release-id>.pdf, then convert to {case_dir}/sources/markdown/ciaa-press-release-<press-release-id>.md.

Step 4 — If the case details or any downloaded document references an IFB/RFP/EOI/PQ procurement number, download bolpatra documents in the same way to {case_dir}/sources/raw/bolpatra-<number>.pdf and convert them.


Step 5 — Fetch court order (faisala):
 Before probing URLs, read the case status from {case_dir}/case_details-{case_dir.name}.md.
 If the case is clearly ongoing / चलिरहेको and there is no source indicating that a final order already exists, do not probe court-order URLs. Record an intentional skip in {case_dir}/logs/fetch-summary.md explaining that no faisala is expected yet for an ongoing case.
 Otherwise, read court_identifier from {case_dir}/case_details-{case_dir.name}.md.
 Try direct URLs first for {case_dir.name} under <court_identifier> using suffix .1, then .2, and extensions in this order: .doc, .docx, .pdf, .jpg, .jpeg.
 Pattern: https://ngm-store.jawafdehi.org/uploads/court-orders/<court_identifier>/{case_dir.name}.<suffix>.<extension>
 Example: https://ngm-store.jawafdehi.org/uploads/court-orders/special/081-CR-0046.1.doc
 If not found, use index fallback:
 https://ngm-store.jawafdehi.org/index-v2.json -> court-orders -> <court_identifier> -> year (first 3 digits) -> {case_dir.name} -> manuscripts[].url.
 The expected case number pattern is: a 3-digit BS year, a hyphen, a case type (like CR, OA, WH), another hyphen, and a serial (e.g., 081-CR-0046).
 If the case number does not start with a 3-digit BS year, the code falls back to using the registration_date_bs and applies fiscal year logic: months 1-3 → year-1, months 4-12 → year, then uses the last 3 digits of the year(e.g 2068 → 068) as the year part of the court order URL pattern.
 
 Save as {case_dir}/sources/raw/court-order-{case_dir.name}-<n>.<ext> and convert to {case_dir}/sources/markdown/court-order-{case_dir.name}-<n>.md.
 If court_identifier is missing, try special then supreme and record what was used in {case_dir}/logs/fetch-summary.md.
YOU MUST DOWNLOAD the original file (.pdf, .doc, etc) to the {case_dir}/sources/raw/ directory first using the download_file tool. Then, use the `convert_to_markdown` MCP tool to convert each downloaded file. Finally, write a brief summary to {case_dir}/logs/fetch-summary.md listing which documents were found, their urls, and which were skipped.
""",
                tools=[download_file, run_command, fix_encoding],
                mcp_servers=_FETCH_SERVER,
                mcp_servers_fn=_jserver,
                mcp_tool_filter=["convert_to_markdown", "fetch"],
                system_prompt=_SYSTEM_PROMPT,
            ),
            WorkflowStep(
                name="fetch-news-articles",
                prompt_fn=lambda case_dir: f"""\
The Jawafdehi case ID is: {case_dir.name}.

Search the web for news articles about this corruption case.
Read {case_dir}/instructions/INSTRUCTIONS.md for full guidance. Also read case_details*.md,
MEMORY.md, and any source markdown files already in {case_dir}/sources/markdown/.

At the start of the step, generate all required query variations up front and record them in
{case_dir}/sources/markdown/news-search-progress.md before running searches.

Use the `search` MCP tool with engines ["duckduckgo", "bing", "brave"] for each query
to run a parallel multi-engine search. Run the first wave of query variations in parallel,
deduplicate the returned URLs, then fetch promising results in balanced parallel batches of 6-8 URLs.
After each fetch round, run `convert_to_markdown` in parallel for that batch before deciding whether
you need another search or fetch round.

If you encounter rate-limit errors (HTTP 429 or repeatedly empty results from engines that were
previously productive), temporarily switch to single-engine queries, pause a few seconds between
calls, and return to full multi-engine parallel search only after the responses stabilize.

Collect 6-10 high-quality, case-relevant news articles and stop when you have enough source diversity and factual coverage. Treat 10 articles as a strong cap. If you intentionally collect more than 10, document the specific justification in {case_dir}/logs/news-search-summary.md so later steps can understand why the cap was exceeded.

For each relevant result, use `fetchWebContent` (or `fetch`) to retrieve the full page,
then use `convert_to_markdown` to save it to {case_dir}/sources/markdown/news-<source-name>.md.

Update {case_dir}/sources/markdown/news-search-progress.md after each search wave and fetch/conversion round with the queries run, URL counts, accepted articles, and any rate-limit fallback used.
Write a final summary to {case_dir}/logs/news-search-summary.md.
Update MEMORY.md with any key learnings for later steps.
""",
                tools=[download_file, run_command, fix_encoding],
                mcp_servers={
                    **_FETCH_SERVER,
                    **_OPEN_WEBSEARCH_SERVER,
                },
                mcp_servers_fn=_jserver,
                mcp_tool_filter=[
                    "fetch",
                    "fetchWebContent",
                    "search",
                    "convert_to_markdown",
                ],
                system_prompt=_SYSTEM_PROMPT,
            ),
            # WorkflowStep(
            #     name="convert-documents",
            #     prompt_fn=lambda case_dir: (
            #         f"The Jawafdehi case ID is: {case_dir.name}\n\n"
            #         "Check /sources/raw/ for any remaining files that do not yet have "
            #         "a corresponding markdown version in /sources/markdown/. "
            #         "Convert each remaining raw file to markdown and save it in "
            #         "/sources/markdown/ with the same base filename."
            #     ),
            #     mcp_tool_filter=["convert_to_markdown"],
            #     mcp_servers=jawafdehi_server,
            #     system_prompt=_SYSTEM_PROMPT,
            # ),
            WorkflowStep(
                name="draft-case",
                prompt_fn=lambda case_dir: f"""\
The Jawafdehi case ID is: {case_dir.name}

Draft a complete Jawafdehi accountability case using sources in {case_dir}/sources/markdown/ and case details.

YOU MUST MATCH the specifications given in {case_dir}/instructions/case-template.md. You must also follow additional instructions in {case_dir}/instructions/INSTRUCTIONS.md, which include guidance on how to extract key information from the source documents and how to structure the case draft. The draft must be saved in Nepali.

Execution requirements:
1. Create {case_dir}/draft.md immediately with a complete template skeleton before long analysis.
2. Fill the skeleton with verified facts from core sources first (case_details, charge sheet, CIAA release), then enrich with news context.
3. Keep sourcing conservative: if facts conflict, prefer primary legal/official documents and explicitly note uncertainty.
4. Before finishing, confirm the file exists and includes substantive content in every required template section.

Save the final draft to {case_dir}/draft.md.
""",
                tools=[fix_encoding],
                required_outputs={"draft.md": 100},
                retries=1,
                system_prompt=_SYSTEM_PROMPT,
            ),
            WorkflowStep(
                name="review-draft",
                prompt_fn=lambda case_dir: f"""\
The Jawafdehi case ID is: {case_dir.name}

You are performing a structured review of the case draft before it is submitted to the API.
Follow the cross-check methodology from the Jawafdehi Case Reviewer skill.

Execution requirements:
1. Create {case_dir}/draft-review.md immediately with a complete review skeleton before long analysis.
2. Record at least preliminary findings for every major review dimension even if some issues remain uncertain.
3. Review source files methodically and refine the findings as you verify more evidence.
4. Before finishing, confirm {case_dir}/draft-review.md exists, contains substantive review content, and ends with one explicit overall outcome line.

Step 1 — Load the Jawafdehi Knowledge Share document.
Download the plain-text export to {case_dir}/data/knowledge-share.txt:
  URL: https://docs.google.com/document/d/1-AZedWGhcQjRH4E7a6q1CDWpeBcb_CVqa8S_kRLQCx4/export?format=txt
Read the **Common pitfalls** section and treat every item as an additional validation rule
for the cross-check below.

Step 2 — Cross-check {case_dir}/draft.md against all source markdown files in
{case_dir}/sources/markdown/, {case_dir}/instructions/case-template.md, and
{case_dir}/instructions/INSTRUCTIONS.md across these dimensions:

1. Identity — CIAA case number correctly stated; defendant/entity names match source documents.
2. Allegations — published allegations match the charge sheet framing; no overstatement or
   unsupported claims.
3. Amounts and counts — bigo, loss amounts, contract values, and number of accused match sources.
4. Timeline:
   - CRITICAL: final verdict (फैसला) date and outcome — flag as critical if missing.
   - MODERATE: significant orders (stay, acquittal, conviction, reversal, appeal filing).
   - GOOD-TO-HAVE: case filing date and named milestone hearings.
   - Routine procedural dates do not need to be individually verified.
   - Validate AD/BS date conversions where both calendars appear.
5. Procedural status — trial / decided / appealed / stayed / otherwise updated.
6. Sources — each major factual claim is backed by a cited source; flag weak sourcing, dead
   links, or secondary-only sourcing.
7. Template compliance — every required field in {case_dir}/instructions/case-template.md is
   present and correctly formatted.
8. Common pitfalls — apply every item loaded from {case_dir}/data/knowledge-share.txt.

For each markdown file in the sources folder, record whether it provides confirming facts,
conflicting facts, or missing support for the published claims. Do not skip any file.

Step 3 — Write findings to {case_dir}/draft-review.md using this severity model:
  - `critical`: materially false, misleading, or unsupported claims
  - `major`: important omissions or inconsistencies that must be fixed
  - `minor`: wording, formatting, or low-risk completeness issues

For each finding include: severity, affected section, what the draft says, what the source
record indicates, and the revision needed.

End draft-review.md with an overall outcome:
  `approved` | `approved_with_minor_edits` | `needs_revision` | `blocked`
""",
                tools=[download_file, fix_encoding],
                required_outputs={"draft-review.md": 100},
                retries=1,
                system_prompt=_SYSTEM_PROMPT,
            ),
            WorkflowStep(
                name="revise-draft",
                prompt_fn=lambda case_dir: f"""\
The Jawafdehi case ID is: {case_dir.name}

Read the review findings at {case_dir}/draft-review.md and the current draft at
{case_dir}/draft.md.

Address every `critical` and `major` finding. Address `minor` findings where the fix is
straightforward and does not require additional source research.

Overwrite {case_dir}/draft.md with the revised, improved version.

Finally, append a one-line revision note to {case_dir}/logs/case-summary.md summarising
which categories of issues were addressed (e.g. "Revised: corrected verdict date, added
missing bigo amount, fixed template fields").
""",
                tools=[fix_encoding],
                required_outputs={"draft.md": 100},
                system_prompt=_SYSTEM_PROMPT,
            ),
            WorkflowStep(
                name="create-case",
                prompt_fn=self._create_case_prompt,
                tools=[fix_encoding],
                mcp_servers_fn=_jserver,
                mcp_tool_filter=[
                    "create_jawafdehi_case",
                    "patch_jawafdehi_case",
                    "get_jawafdehi_case",
                    "search_jawafdehi_cases",
                    "search_jawaf_entities",
                    "get_jawaf_entity",
                    "create_jawaf_entity",
                    "upload_document_source",
                ],
                system_prompt=_SYSTEM_PROMPT,
            ),
            WorkflowStep(
                name="create-update-entities",
                prompt_fn=lambda case_dir: f"""\
The Jawafdehi case ID is: {case_dir.name}. The numeric Jawafdehi case ID is in {case_dir}/MEMORY.md.

For each accused, defendant, and related entity listed in {case_dir}/draft.md, link them to the
Jawafdehi case. Follow these steps for each entity:

Step 1 — Check if the entity already exists.
  Call search_jawaf_entities with the entity name as the search query.
  If a match is returned, note its integer ID — do not create a duplicate.

Step 2 — Create the entity only if it does not already exist.
  Call create_jawaf_entity with either nes_id (if you have an NES ID from the draft or sources)
  or display_name (if no NES record is known). Record the new entity ID in {case_dir}/MEMORY.md.

Step 3 — Link the entity to the case.
  Call patch_jawafdehi_case with the numeric Jawafdehi case ID from MEMORY.md.
  Use a JSON Patch add operation:
    {{"op": "add", "path": "/entities/-", "value": {{"entity": <entity_id>, "relationship_type": "<TYPE>", "notes": "<notes>"}}}}
    Relationship types: accused (main defendants), alleged (named but unconfirmed), related
    (organizations or third parties), witness, victim, opposition, location. Use the exact lowercase values accepted by the API.

  Set notes to the role the entity played as described in the draft.

Step 4 — Confirm the link.
  Call get_jawaf_entity with the entity ID and verify the returned related_cases field.

Record all entity IDs created or linked in {case_dir}/MEMORY.md.
""",
                tools=[fix_encoding],
                mcp_servers_fn=_jserver,
                mcp_tool_filter=[
                    "search_jawafdehi_cases",
                    "search_jawaf_entities",
                    "get_jawaf_entity",
                    "create_jawaf_entity",
                    "patch_jawafdehi_case",
                    "get_jawafdehi_case",
                    "upload_document_source",
                ],
                system_prompt=_SYSTEM_PROMPT,
            ),
            WorkflowStep(
                name="update-case-details",
                prompt_fn=lambda case_dir: f"""\
The CIAA case number is: {case_dir.name}. The numeric Jawafdehi case ID is in {case_dir}/MEMORY.md.
Read MEMORY.md first — do NOT call search_jawafdehi_cases; use the stored ID only.

--- STEP 1: Upload source documents ---

Upload each primary source file as a DocumentSource.
For every file in {case_dir}/sources/raw/ with extension .pdf, .doc, .docx, .jpg, or .jpeg,
call upload_document_source with:
  - file_path: the absolute path to the file
    - title: a descriptive title
    - description (REQUIRED for legal/official docs): concise source description in Nepali, e.g.
            "अख्तियारद्वारा दायर गरिएको अभियोग पत्र — मुद्दा {case_dir.name}, प्रतिवादी र मुख्य आरोपसम्बन्धी आधिकारिक कागजात।"
  - source_type from this mapping:
      ciaa-press-release-*  →  OFFICIAL_GOVERNMENT
      charge-sheet-*        →  LEGAL_PROCEDURAL
      court-order-*         →  LEGAL_COURT_ORDER
      bolpatra-*            →  OFFICIAL_GOVERNMENT

Also upload news markdown files from {case_dir}/sources/markdown/. For every file matching
news-*.md, call upload_document_source with:
  - file_path: the absolute path to the .md file
  - title: a descriptive title for the article
    - url: ["<original_article_url>"] using the original article URL recorded in {case_dir}/MEMORY.md. Do not use the uploaded markdown path as the external URL.
    - publication_date: YYYY-MM-DD from {case_dir}/MEMORY.md when available
    - description: one-sentence source description in Nepali summarising the article's specific contribution to this case
  - source_type: MEDIA_NEWS

For every news source, preserve both pieces of information: the uploaded markdown transcript stays attached as the file upload, and the original external article URL must be present in the `url` field.

Before moving on, verify that no uploaded news source is missing its original external URL and that source descriptions are generally in Nepali.

Record every returned source_id along with a short description in {case_dir}/MEMORY.md.

--- STEP 2: Attach evidence to the case ---

After all uploads, call patch_jawafdehi_case with a single RFC 6902 JSON Patch request
containing one add operation per evidence entry:
  {{"op": "add", "path": "/evidence/-", "value": {{"source_id": "<source_id>", "description": "<description>"}}}}

Use clear, factual evidence descriptions in Nepali, e.g.:
    "यो अभियोग पत्रले आरोपित गैरकानुनी सम्पत्ति आर्जनको दाबी र बिगो रकम पुष्टि गर्दछ।"
    "यो अदालत आदेशले मिति 2081-09-12 को निर्णय र त्यसको नतिजा पुष्टि गर्दछ।"

Evidence descriptions should generally be in Nepali for both official documents and news articles.

--- STEP 3: Update remaining case fields ---

First call get_jawafdehi_case with the numeric case ID to see which fields are already set.
Then patch missing or incomplete fields from {case_dir}/draft.md:
  - /timeline: list of {{"date": "<ISO date AD>", "title": "<title>", "description": "<description>"}} objects
  - /short_description: one-sentence teaser (if not yet set)
  - /description: full Nepali HTML description (if not yet set)
    - /tags: list of English tags following the recommended controlled vocabulary in {case_dir}/instructions/INSTRUCTIONS.md
  - /key_allegations: list of allegations
  - /case_start_date and /case_end_date: ISO 8601 dates
  - /court_cases: list of strings in "court_identifier:case_number" format, e.g. ["special:081-CR-0123"].
      Read court_identifier and case number from {case_dir}/case_details-{case_dir.name}.md (NGM case data).
      Omit if court_identifier is unknown.
  - /bigo: integer NPR amount from the Bigo Amount field in {case_dir}/draft.md.
      Must be a plain integer (no commas, no currency symbols), e.g. 15880000.
      Omit if blank or unknown.
  - /missing_details: plain text string compiled from unchecked items in the Missing Details
      section of {case_dir}/draft.md. Omit if all items are checked or the section is empty.

All patch operations for these fields can be combined into a single patch_jawafdehi_case call.
""",
                tools=[fix_encoding],
                mcp_servers_fn=_jserver,
                mcp_tool_filter=[
                    "create_jawafdehi_case",
                    "search_jawafdehi_cases",
                    "search_jawaf_entities",
                    "upload_document_source",
                    "patch_jawafdehi_case",
                    "get_jawafdehi_case",
                    "get_jawaf_entity",
                    "create_jawaf_entity",
                ],
                system_prompt=_SYSTEM_PROMPT,
            ),
        ]

    def _create_case_prompt(self, case_dir: Path) -> str:
        if self.update_existing:
            return f"""\
The CIAA case number is: {case_dir.name}

This case already exists as a draft in Jawafdehi. Your job is to find it and patch it
with the details from the draft — do NOT create a new case.

Step 1 — Find the existing draft case.
  Call search_jawafdehi_cases with the CIAA case number {case_dir.name} to locate
  the existing Jawafdehi draft case. If the search does not return results, try
  searching by the primary defendant name from {case_dir}/draft.md.

Step 2 — Record the case ID.
  Once found, record the returned numeric Jawafdehi case ID (1, 2, 3, 4, etc.)
  in {case_dir}/MEMORY.md for use in subsequent steps.

Step 3 — Patch the case with updated details.
  Read the case draft at {case_dir}/draft.md and call patch_jawafdehi_case with the
  numeric case ID to update: title, Key allegations, Timeline, case start date,
  case end date, and any other improved fields from the draft.
"""
        else:
            return f"""\
The Jawafdehi case ID is: {case_dir.name}

Read the case draft at {case_dir}/draft.md and create a basic Jawafdehi case via the API
using the minimum required fields (title, case type, summary). Record the returned numeric Jawafdehi case ID (1, 2, 3, 4, etc.)
in {case_dir}/MEMORY.md for use in subsequent steps.

NOTE: along with the case title, you MUST update the Key allegations, Timeline, case start, case end dates.
"""

    def get_eligible_cases(self, limit: int = 0) -> List[str]:
        """
        Return Jawafdehi Case ``case_id`` values eligible for this workflow.

        A case is eligible if:
        - It is a CORRUPTION case in DRAFT or IN_REVIEW state
        - Its title contains one of the known CIAA Special Court case numbers
        - In normal mode: there is no existing completed CaseWorkflowRun for it
        - In update mode: existing completed runs are included (we are re-enriching)

        When ``limit > 0``, selects the top ``limit`` cases by priority score
        (bigo + defendant count + data richness), excluding already-completed runs
        unless in update-existing mode.
        """
        from case_workflows.models import CaseWorkflowRun
        from cases.models import Case, CaseState, CaseType

        from case_workflows.workflows.ciaa_caseworker.constants import CIAA_CASE_NUMBERS

        rows = (
            Case.objects.filter(
                case_type=CaseType.CORRUPTION,
                state__in=[CaseState.DRAFT, CaseState.IN_REVIEW],
            )
            .values_list("case_id", "title")
        )

        if not self.update_existing:
            completed_case_ids = set(
                CaseWorkflowRun.objects.filter(
                    workflow_id=self.workflow_id,
                    is_complete=True,
                ).values_list("case_id", flat=True)
            )
            rows = rows.exclude(case_id__in=completed_case_ids)

        eligible = [
            case_id
            for case_id, title in rows
            if any(num in title for num in CIAA_CASE_NUMBERS)
        ]

        if limit > 0 and len(eligible) > limit:
            sorted_cases = self._sort_by_priority(eligible)
            eligible = sorted_cases[:limit]

        return eligible

    def _sort_by_priority(self, case_ids: List[str]) -> List[str]:
        """
        Sort ``case_ids`` by descending priority score (bigo + complexity).
        Used when ``--priority N`` selects a subset of eligible cases.
        """
        import math

        from django.db.models import Count, Q
        from cases.models import Case

        cases = (
            Case.objects.filter(case_id__in=case_ids)
            .annotate(
                accused_count=Count(
                    "entity_relationships",
                    filter=Q(entity_relationships__relationship_type="accused"),
                ),
                total_entities=Count("entity_relationships"),
            )
            .values_list("case_id", "bigo", "accused_count", "total_entities",
                         "court_cases", "notes")
        )

        def _score(bigo, acc, total, cc, notes):
            bigo_s = min(1.0, math.log10(float(bigo)) / 12.0) if bigo and bigo > 0 else 0.0
            complexity = min(1.0, (acc or 0) * 0.15 + (total or 0) * 0.05)
            richness = (0.4 if cc else 0.0) + (0.2 if notes and notes.strip() else 0.0)
            return 0.55 * bigo_s + 0.30 * complexity + 0.15 * richness

        scored = []
        for cid, bigo, acc, total, cc, notes in cases:
            scored.append((cid, _score(bigo, acc, total, cc, notes), bigo or 0))

        scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return [cid for cid, _, _ in scored]

    def get_template_dir(self) -> Path:
        return TEMPLATE_DIR

    def on_work_dir_created(self, case_dir: Path) -> None:
        """
        CIAA-specific work directory setup:

        - ``MEMORY.md``         — agent memory (learnings persisted across steps)
        - ``instructions/``     — casework instructions and case draft template
        - ``data/``             — reference CSVs (CIAA press releases, AG index)
        - ``sources/raw/``      — downloaded raw files (PDFs, HTML, etc.)
        - ``sources/markdown/`` — converted markdown versions
        - ``logs/``             — run notes and summaries
        """
        import shutil

        instructions_src = TEMPLATE_DIR / "instructions"
        if instructions_src.is_dir():
            shutil.copytree(instructions_src, case_dir / "instructions")
            _render_instruction_tokens(case_dir, case_dir.name)
            logger.info("Copied instructions to %s/instructions/", case_dir)

        data_src = TEMPLATE_DIR / "data"
        if data_src.is_dir():
            shutil.copytree(data_src, case_dir / "data")
            logger.info("Copied data to %s/data/", case_dir)

        (case_dir / "sources" / "raw").mkdir(parents=True, exist_ok=True)
        (case_dir / "sources" / "markdown").mkdir(parents=True, exist_ok=True)
        (case_dir / "logs").mkdir(parents=True, exist_ok=True)

        memory_file = case_dir / "MEMORY.md"
        if not memory_file.exists():
            memory_file.write_text("""\
# Agent Memory

This file is read at the start of every step and can be updated \
by the agent to record learnings, discovered facts, and notes \
that should persist across steps.

## Learnings

(none yet)
""")
            logger.info("Created MEMORY.md at %s", memory_file)
