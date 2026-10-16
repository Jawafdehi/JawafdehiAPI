"""Tools for walking into a document instead of swallowing it whole.

The archive holds 346,341 documents and the largest are a few hundred pages of
Devanagari. A tool that returns one of those returns something no model can read:
measured, a single Auditor General report is 3.96 MB of JSON, roughly 600k–900k
tokens. So nothing here returns a document. These four return the *shape* of one,
and then bounded slices of it.

    list_document_sources     what the archive holds, and how much of it is READ
    get_document_overview     what is inside one document, and whether it is readable
    list_document_contents    its page map / table captions / figure titles — no text
    read_document_part        one bounded slice: a page range, a table, a figure

The ordering is the intended call sequence, and each step is cheap enough to be
wrong about. Only the last one returns document text.

**Two honesty rules are structural here, not advisory.** A page we hold but could
not transcribe comes back with empty text, and if that renders the same as a
blank page a model reports failed OCR as an absence of findings. A chart value
read off an image is a weaker claim than a printed one, and collapsing that
distinction publishes a guess as a fact. Both ride on every response that can
carry them, and the tool descriptions say so, because the model is the consumer
that will otherwise get it wrong.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

import httpx
import structlog
from jawafdehi_shared.entities.ids import parse_material_iri

from ..control_plane import request_control_plane
from ..path_safety import encode_material_parts
from .base import BaseTool, ToolExecutionResult

logger = structlog.get_logger()

#: Hard ceiling on the characters of document text ONE CALL may return, summed
#: across every item in the response.
#:
#: Not configurable by the caller: a cap a caller can raise is a cap that gets
#: raised, and the whole point of this surface is that the model cannot ask for
#: the document. The API's own 50-page limit bounds a range; this bounds the one
#: thing it does not — a single table, the largest of which is ~170 KiB of
#: markdown.
MAX_TEXT_CHARS = 20_000


def _text(payload: Any) -> list[TextContent]:
    return [
        TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))
    ]


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(_text({"success": False, "error": message}), is_error=True)


def _material_path(iri: Any, suffix: str = "") -> str | None:
    """``/api/materials/<source>/<ident>`` for a material IRI, or None.

    Delegates the grammar to ``parse_material_iri`` and the encoding to
    ``encode_material_parts`` rather than slicing the string: those two are the
    canonical definition of what a material IRI is, and a second hand-rolled
    parser here would drift from them silently.

    Both RAISE on anything malformed — including a double-encoded traversal,
    which survives a naive segment check and is only caught once the path is
    repeatedly unquoted. Everything is funnelled into ``None`` so a hostile or
    malformed IRI reaches the model as a tool error it can act on, rather than
    an exception crossing the MCP boundary as a transport failure.
    """
    if not isinstance(iri, str):
        return None
    try:
        parsed = parse_material_iri(iri)
        source, ident = encode_material_parts(parsed.source, parsed.ident)
    except ValueError:
        return None
    return f"/api/materials/{source}/{ident}{suffix}"


async def _call(tool: str, path: str, **kwargs: Any) -> dict[str, Any] | ToolExecutionResult:
    """Call the control plane, turning every failure into a tool error.

    Three things the raw helper does not do, each of which otherwise reaches the
    model as something other than what it is:

    * a transport failure (timeout, DNS, TLS) raises out of the tool and is
      reported as an MCP protocol fault rather than "the archive is unreachable";
    * ``_validate_api_path`` raises ``ValueError`` on a path that only resolves
      to a traversal after repeated unquoting — same problem;
    * a non-2xx response comes back as a perfectly ordinary envelope, so a 503
      catalogue or a 400 "range too wide" is delivered as a SUCCESSFUL tool call
      and the model treats the error body as data.
    """
    try:
        result = await request_control_plane("GET", path, **kwargs)
    except httpx.TimeoutException:
        logger.warning("document_tool_timeout", tool_name=tool, path=path)
        return _error("The Jawafdehi API did not respond in time. Try again.")
    except httpx.HTTPError as exc:
        logger.error("document_tool_http_error", tool_name=tool, path=path, error=str(exc))
        return _error(f"Could not reach the Jawafdehi API: {exc}")
    except ValueError as exc:
        return _error(f"Refusing to request that path: {exc}")
    if not result.get("success", True):
        return ToolExecutionResult(_text(result), is_error=True)
    return result


def _truncate(value: str, budget: int) -> tuple[str, bool]:
    if len(value) <= budget:
        return value, False
    return value[:budget], True


async def _with_extraction(
    tool: str, path: str, failure: ToolExecutionResult
) -> ToolExecutionResult:
    """Turn "no content for this material" into "parsing has started".

    The content routes answer 404 for two different facts, and the body tells
    them apart: ``extractable`` true means nobody has opened this document yet,
    false means there is nothing here we can open. Only the first is worth
    acting on — and only the first is something the caller can wait out.

    Every other failure (403 on a non-public document, 400 on a bad argument, a
    503 from the archive) is returned untouched. Reaching for a parse on those
    would answer a question nobody asked and hide the one that was.
    """
    payload = _payload_of(failure)
    if payload.get("status_code") != 404 or not _extractable(payload):
        return failure
    started = await _start_extraction(tool, path)
    body = dict(payload)
    body.update(started)
    # Still an error result: the call did not return content, and a model told
    # otherwise will report an empty document as a read one.
    return ToolExecutionResult(_text(body), is_error=True)


def _payload_of(failure: ToolExecutionResult) -> dict[str, Any]:
    """The envelope a ``_call`` failure carries, or an empty dict."""
    try:
        # ToolExecutionResult IS a list of TextContent, not a wrapper around one.
        return json.loads(failure[0].text)
    except (AttributeError, IndexError, TypeError, ValueError):
        return {}


def _extractable(payload: dict[str, Any]) -> bool:
    data = payload.get("data")
    return bool(isinstance(data, dict) and data.get("extractable"))


async def _start_extraction(tool: str, path: str) -> dict[str, Any]:
    """Ask the platform to parse this document, and say what came of it.

    **Reading a document we have never opened is what starts the parse.** There
    is no separate "please extract this" step for a caller to know about: the
    archive is 346k documents and ~94% have never been read, so the first honest
    answer to most reads is "nobody has opened this yet" — and the useful thing
    to do with that answer is to start.

    ``POST …/content`` is the platform's internal trigger, not a route clients
    are expected to call; these tools are what calls it. It returns at once
    (202), because parsing a 1,113-page report is ~10 minutes and no tool call
    should sit on that. Every outcome is reported as a note rather than an error:
    the tool's own answer — "we have not parsed this" — is still true and still
    useful, and a failure to START a parse must not turn a successful read of the
    metadata into a failed tool call.
    """
    result = await request_control_plane("POST", f"{path}/content")
    status = result.get("status_code")
    payload = result.get("data") if isinstance(result.get("data"), dict) else {}
    if status == 202:
        logger.info("document_extraction_started", tool_name=tool, path=path)
        return {
            "extraction": "started",
            "note": (
                "Nobody had opened this document, so parsing it has just been "
                "queued. It takes roughly a minute per hundred pages, and a "
                "large report can take ten. Call this tool again in a few "
                "minutes; until then nothing in the document can be quoted."
            ),
        }
    if status == 200:
        # It was parsed between the read and the trigger — a batch run landed,
        # or another caller asked first.
        return {"extraction": "ready", "note": "This document has just been parsed; call again."}
    if status == 409:
        return {
            "extraction": "unavailable",
            "note": (
                "This document cannot be parsed: it carries no PDF we can open. "
                "Cite it as a holding and link the original instead."
            ),
        }
    if status == 429:
        return {
            "extraction": "busy",
            "note": (
                "Parsing was not queued — too many documents are already waiting. "
                "Try again later."
            ),
        }
    if status in (401, 403):
        return {
            "extraction": "not permitted",
            "note": (
                "This document has not been parsed, and starting a parse needs a "
                "Caseworker role. Ask one to open it, or cite the original PDF."
            ),
        }
    logger.warning(
        "document_extraction_trigger_failed", tool_name=tool, path=path, status=status
    )
    return {
        "extraction": "failed",
        "note": f"Could not start parsing this document ({payload.get('detail') or status}).",
    }


class ListDocumentSourcesTool(BaseTool):
    @property
    def name(self) -> str:
        return "list_document_sources"

    @property
    def description(self) -> str:
        return (
            "What the Jawafdehi archive holds, grouped by source: how many "
            "documents each source has, how many of them have actually been "
            "READ (transcribed), and the distinct series inside a source. "
            "Call this first — the source tokens are not self-explanatory and "
            "two of them are easy to confuse: 'ag' is the office of the "
            "ATTORNEY General (charge sheets), while the AUDITOR General's "
            "reports are under 'official_report'. A source with labelled=false "
            "has no reviewed description; do not guess what it is from the "
            "token's spelling. A high count with readable=0 means we hold those "
            "documents but have not transcribed any of them — nothing in them "
            "can be quoted. readable=null means the figure has not been computed "
            "yet; it does NOT mean zero."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        # ``required: []`` is explicit by repo convention, not redundancy:
        # some MCP clients treat a missing key as "unspecified" and prompt
        # for arguments a no-argument tool does not take.
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        result = await _call(self.name, "/api/document-sources/")
        return result if isinstance(result, ToolExecutionResult) else _text(result)


class GetDocumentOverviewTool(BaseTool):
    @property
    def name(self) -> str:
        return "get_document_overview"

    @property
    def description(self) -> str:
        return (
            "What is inside one document: its title, publisher, date, fiscal "
            "year, the original PDF link, and — when we have parsed it — how "
            "many pages, tables and figures it holds, how many of those pages "
            "were readable, and where the parse came from. Returns no document "
            "text. Call this before reading anything: if 'content' is absent we "
            "have not parsed the document and only its metadata exists, and if "
            "pages_read is lower than pages then some pages failed OCR and are "
            "held but unreadable."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "iri": {
                    "type": "string",
                    "description": "The document's canonical @id IRI.",
                }
            },
            "required": ["iri"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        path = _material_path(arguments.get("iri", ""))
        if path is None:
            return _error("iri must be a material @id IRI (…/material/<source>/<ident>).")
        result = await _call(self.name, path)
        if isinstance(result, ToolExecutionResult):
            # A 404/403 body is {"detail": …} and IS a dict, so reshaping it
            # below would produce a confident overview of a document we cannot
            # see — telling the model it exists but is unparsed.
            return result
        doc = result.get("data")
        if not isinstance(doc, dict):
            return _text(result)

        content = doc.get("jawafdehi:content")
        # associatedMedia is "a single dict or a list" across the corpus (see
        # materials.provenance._media_list); assuming a list drops the citation
        # chain of every single-link document.
        media = doc.get("associatedMedia")
        media = [media] if isinstance(media, dict) else media
        media = media if isinstance(media, list) else []
        # The stored JSON-LD is free-form past @type/@id/name, so publisher is
        # not guaranteed to be an object.
        publisher = doc.get("publisher")
        publisher_name = publisher.get("name") if isinstance(publisher, dict) else publisher
        overview: dict[str, Any] = {
            "iri": doc.get("@id"),
            "name": doc.get("name"),
            "publisher": publisher_name,
            "date_published": doc.get("datePublished"),
            "fiscal_year_bs": doc.get("jawafdehi:fiscalYearBS"),
            # The citation chain. A reader has to be able to get back to the
            # publisher's own copy, or nothing quoted from here is checkable.
            "source_links": [
                {"role": m.get("jawafdehi:linkRole"), "url": m.get("contentUrl")}
                for m in media
                if isinstance(m, dict)
            ],
            "content": content,
        }
        if content is None:
            overview["readable"] = False
            overview["note"] = (
                "This document has NOT been parsed. Only its metadata exists; "
                "nothing in it can be quoted. Cite it as a holding, or link the "
                "original PDF above."
            )
            # Reading it is what starts the parse — see _start_extraction. The
            # overview is still returned either way: the answer "we have not
            # opened this" is true and useful now, and the parse is for the next
            # call.
            started = await _start_extraction(self.name, path)
            # APPEND rather than replace. Both facts hold at once — nothing here
            # can be quoted yet, AND something is now being done about it — and
            # a model that reads only "parsing has started" will wait for a
            # document it was never told it cannot cite.
            overview["note"] = f"{overview['note']} {started.pop('note', '')}".strip()
            overview.update(started)
        else:
            unread = (content.get("pages") or 0) - (content.get("pages_read") or 0)
            overview["readable"] = True
            if unread > 0:
                overview["note"] = (
                    f"{unread} of {content.get('pages')} pages are held but could "
                    "not be transcribed. Their text comes back empty; that is a "
                    "statement about our OCR, not about the pages."
                )
        return _text(overview)


class ListDocumentContentsTool(BaseTool):
    KINDS = ("page", "table", "figure")

    @property
    def name(self) -> str:
        return "list_document_contents"

    @property
    def description(self) -> str:
        return (
            "The navigable index of one document — its page map, its table "
            "captions, or its figure titles. Returns NO page text and NO table "
            "markdown, so it is cheap to call on a large document. Use it to "
            "pick what to read: find the table whose caption matches your "
            "question, then fetch that one table with read_document_part. For "
            "pages, coverage_state tells you what happened to each page before "
            "you spend a call on it: 'anchored' was read from the PDF's own text "
            "layer and 'ocr_vision' was read by a vision model — both have text, "
            "but a vision read is the weaker claim. 'blank', 'gap', 'folded' and "
            "'ocr_unread' have little or no text, and an empty page there is a "
            "statement about our OCR, not about the page."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "iri": {"type": "string", "description": "The document's @id IRI."},
                "kind": {
                    "type": "string",
                    "enum": list(self.KINDS),
                    "description": "Which index to return.",
                },
            },
            "required": ["iri", "kind"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        kind = arguments.get("kind")
        if kind not in self.KINDS:
            return _error(f"kind must be one of {', '.join(self.KINDS)}.")
        path = _material_path(arguments.get("iri", ""), "/content")
        if path is None:
            return _error("iri must be a material @id IRI (…/material/<source>/<ident>).")
        result = await _call(self.name, path, params={"type": kind})
        if isinstance(result, ToolExecutionResult):
            return await _with_extraction(self.name, path, result)
        return _text(result)


class ReadDocumentPartTool(BaseTool):
    @property
    def name(self) -> str:
        return "read_document_part"

    @property
    def description(self) -> str:
        return (
            "Read ONE bounded slice of a document: a page range (at most 50 "
            "pages), one table by its key, or one figure by its key. This is the "
            "only tool that returns document text, and it never returns a whole "
            "document — call list_document_contents first to choose what to "
            "read. Address a table or figure by its 'key' (like 'p0030-t1'), "
            "never by its ordinal: ordinals are recomputed whenever a document "
            "is re-parsed, so the same ordinal can later mean a different table. "
            "A page whose text is empty was held but not successfully "
            "transcribed — check coverage_state before concluding the page is "
            "blank, and treat 'ocr_vision' text as less reliable than "
            "'anchored'. On a figure, every data point carries 'estimated': a true "
            "value was read off the chart image rather than a printed number and "
            "must not be presented as equally reliable."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "iri": {"type": "string", "description": "The document's @id IRI."},
                "page": {"type": "integer", "description": "A single printed page number."},
                "from_page": {"type": "integer", "description": "First page of a range."},
                "to_page": {"type": "integer", "description": "Last page, inclusive."},
                "table": {"type": "string", "description": "A table key, e.g. 'p0030-t1'."},
                "figure": {"type": "string", "description": "A figure key, e.g. 'p0030-f1'."},
            },
            "required": ["iri"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        path = _material_path(arguments.get("iri", ""), "/content")
        if path is None:
            return _error("iri must be a material @id IRI (…/material/<source>/<ident>).")

        page = arguments.get("page")
        first, last = arguments.get("from_page"), arguments.get("to_page")
        table, figure = arguments.get("table"), arguments.get("figure")
        chosen = [k for k, v in (
            ("page", page), ("range", first if first is not None else last),
            ("table", table), ("figure", figure),
        ) if v is not None]
        if len(chosen) != 1:
            return _error(
                "Give exactly one of: page, from_page+to_page, table, figure. "
                f"Got {len(chosen)}."
            )

        if table is not None:
            params: dict[str, Any] = {"type": "table", "id": table}
        elif figure is not None:
            params = {"type": "figure", "id": figure}
        elif page is not None:
            params = {"type": "page", "num": page}
        else:
            if first is None or last is None:
                return _error("A page range needs both from_page and to_page.")
            params = {"type": "page", "from": first, "to": last}

        result = await _call(self.name, path, params=params)
        if isinstance(result, ToolExecutionResult):
            return await _with_extraction(self.name, path, result)
        return _text(self._bounded(result))

    def _bounded(self, result: dict[str, Any]) -> dict[str, Any]:
        """Apply the text ceiling ACROSS the call, and say so when it bites.

        Per-call, not per item. A 50-page range is fifty text fields, so a
        per-item ceiling would be a fifty-fold one — up to ~1M characters, which
        is the context bomb this whole surface exists to prevent.

        Truncation is announced, never silent. A model handed a clipped table
        with no marker summarises it as the whole table, and the reader has no
        way to know a figure is missing.
        """
        data = result.get("data")
        if not isinstance(data, dict):
            return result

        holders = [data["item"]] if isinstance(data.get("item"), dict) else []
        holders += [i for i in (data.get("items") or []) if isinstance(i, dict)]

        budget = MAX_TEXT_CHARS
        clipped_any = False
        for holder in holders:
            for field in ("text", "markdown"):
                value = holder.get(field)
                if not isinstance(value, str) or not value:
                    continue
                if len(value) <= budget:
                    budget -= len(value)
                    continue
                holder[field] = value[:budget]
                holder["truncated"] = True
                holder["full_length_chars"] = len(value)
                budget = 0
                clipped_any = True

        # The canonical IRI from the response, not the caller's argument: the
        # argument is whatever host the caller typed, and a citation that
        # records it would attribute our text to that host.
        data["citation"] = {
            "document": data.get("material"),
            "note": (
                "Quote with the document IRI and the page or key. Page numbers "
                "are the printed folios, checkable against the original PDF."
            ),
        }
        if clipped_any:
            data["citation"]["warning"] = (
                f"Output was capped at {MAX_TEXT_CHARS} characters for this call. "
                "Fields marked truncated are incomplete — narrow the range or "
                "fetch one item at a time rather than summarising what is here."
            )
        return result
