"""Tools for walking into a document instead of swallowing it whole.

The archive holds 346,341 documents and the largest are a few hundred pages of
Devanagari. A tool that returns one of those returns something no model can read:
measured, a single Auditor General report is 3.96 MB of JSON, roughly 600k–900k
tokens. So nothing here returns a document. These five return the *shape* of one,
and then bounded slices of it.

    list_document_sources     what the archive holds, and how much of it is READ
    get_document_overview     what is inside one document, and whether it is readable
    list_document_contents    its page map / table captions / figure titles — no text
    search_document           where a term appears inside it — snippets, mixed kinds
    read_document_part        one bounded slice: a page range, a table, a figure

The ordering is the intended call sequence, and each step is cheap enough to be
wrong about. Only the last one returns document text.

``search_document`` is the branch in that sequence, and the one worth reaching
for first on a long document: ``list_document_contents`` answers "what is in
here" and only reaches a table through its CAPTION, so a model looking for what
a document *says* had no move except reading page ranges until it found
something. Search answers that directly and returns snippets, so the expensive
``read_document_part`` call that follows is aimed rather than exploratory.

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
            # No trigger here. Every document that arrives is queued for
            # reading on arrival, and the sweep reconciles whatever that missed,
            # so a tool call asking for one is work the platform has already
            # scheduled — see materials/signals.py. What the tool owes a model is
            # the true statement that nothing here can be quoted YET.
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
        material_path = _material_path(arguments.get("iri", ""))
        if material_path is None:
            return _error("iri must be a material @id IRI (…/material/<source>/<ident>).")
        path = f"{material_path}/content"
        result = await _call(self.name, path, params={"type": kind})
        return result if isinstance(result, ToolExecutionResult) else _text(result)


class SearchDocumentTool(BaseTool):
    #: Mirrors the API's own ceiling (``materials.content_api.MAX_SEARCH_HITS``).
    #: Stated here so the schema can bound the argument and the model is told
    #: the limit before it spends a call discovering it as a 400.
    MAX_LIMIT = 50

    @property
    def name(self) -> str:
        return "search_document"

    @property
    def description(self) -> str:
        return (
            "Find where a term appears INSIDE one document, without reading "
            "the document. Searches the page text, the table captions and "
            "table contents, and the figure titles and notes, and returns "
            "mixed hits — a page, a table and a chart can all come back "
            "together — in page order, each with a short snippet, its page "
            "number and the key to fetch it with read_document_part. Use this "
            "before read_document_part on anything longer than a few pages: "
            "list_document_contents only reaches a table through its caption, "
            "so it cannot find a paragraph by what it says. Matches "
            "phonetically as well as literally, so a Latin-keyboard query "
            "reaches Devanagari text ('ghus' finds घुस); a bare number means "
            "that exact printed page. Every hit carries 'match': 'literal' is "
            "the term as typed, 'phonetic' matched by SOUND and may be a "
            "different word, and 'folio' matched the page NUMBER and says "
            "nothing about the text — do not quote a phonetic or folio hit as "
            "though the document used that term. 'total' is the real number of "
            "hits; if 'truncated' is true you are seeing the first few of many, "
            "so narrow the query rather than concluding these are all of them."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "iri": {"type": "string", "description": "The document's @id IRI."},
                "q": {
                    "type": "string",
                    "description": (
                        "What to look for. A word or short phrase, in "
                        "Devanagari or romanized."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self.MAX_LIMIT,
                    "description": (
                        f"How many hits to return (1-{self.MAX_LIMIT}). Defaults "
                        "to the maximum; 'total' reports the full count either way."
                    ),
                },
            },
            "required": ["iri", "q"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        material_path = _material_path(arguments.get("iri", ""))
        if material_path is None:
            return _error("iri must be a material @id IRI (…/material/<source>/<ident>).")
        query = arguments.get("q")
        if not isinstance(query, str) or not query.strip():
            return _error("q is required; it is what to search for.")

        params: dict[str, Any] = {"q": query}
        limit = arguments.get("limit")
        if limit is not None:
            # Validated here as well as by the API, so a bad value is a tool
            # error naming the bound rather than a 400 the model has to parse.
            if not isinstance(limit, int) or isinstance(limit, bool):
                return _error("limit must be an integer.")
            if not 1 <= limit <= self.MAX_LIMIT:
                return _error(f"limit must be between 1 and {self.MAX_LIMIT}.")
            params["limit"] = limit

        result = await _call(self.name, f"{material_path}/search", params=params)
        if isinstance(result, ToolExecutionResult):
            return result
        return _text(self._cited(result))

    def _cited(self, result: dict[str, Any]) -> dict[str, Any]:
        """Attach the citation contract, and say when a result is partial.

        No text ceiling is applied and none is needed: a snippet is bounded by
        the API at ~190 characters and the hit count is bounded at
        ``MAX_LIMIT``, so the whole response is a few thousand characters by
        construction. ``read_document_part``'s budget exists because a single
        table can be 170 KiB; nothing here can be.

        A snippet is a WINDOW, and that has to be said. Elided ends are marked
        with an ellipsis in the text, but a model quoting a snippet as a
        sentence of the document would be quoting a fragment as a whole.
        """
        data = result.get("data")
        if not isinstance(data, dict):
            return result
        data["citation"] = {
            "document": data.get("material"),
            "note": (
                "Each snippet is a short window around the match, not the full "
                "page or table — an ellipsis marks an end that was cut. Fetch "
                "the page_no or key with read_document_part before quoting. "
                "Page numbers are the printed folios, checkable against the "
                "original PDF."
            ),
        }
        if data.get("truncated"):
            data["citation"]["warning"] = (
                f"This document holds {data.get('total')} hits and only "
                f"{data.get('count')} are shown. These are the first by page "
                "order, NOT the best — narrow the query rather than "
                "summarising the document from this sample."
            )
        return result


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
            "document — call search_document (to find where a term appears) or "
            "list_document_contents (to see the index) first, and come here "
            "with the page number or key it gave you rather than reading "
            "ranges until you find something. Address a table or figure by its "
            "'key' (like 'p0030-t1'), "
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
        material_path = _material_path(arguments.get("iri", ""))
        # Guarded on the material path itself rather than on the derived one, so
        # both are known non-None below — the trigger takes the material path.
        if material_path is None:
            return _error("iri must be a material @id IRI (…/material/<source>/<ident>).")
        path = f"{material_path}/content"

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
            return result
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
