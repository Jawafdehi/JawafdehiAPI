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

from ..control_plane import request_control_plane
from ..path_safety import encode_path_segment
from .base import BaseTool, ToolExecutionResult

#: Hard ceiling on the characters of document text one call may return.
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


def _material_path(iri: str, suffix: str = "") -> str | None:
    """``/api/materials/<source>/<ident>`` for a material IRI, or None.

    Parsed rather than string-sliced so a caller cannot smuggle a path through
    the IRI — the segments are re-encoded individually.
    """
    marker = "/material/"
    if not isinstance(iri, str) or marker not in iri:
        return None
    tail = iri.split(marker, 1)[1].strip("/")
    if not tail or "/" not in tail:
        return None
    source, _, ident = tail.rpartition("/")
    try:
        segments = [encode_path_segment(s) for s in source.split("/")] + [
            encode_path_segment(ident)
        ]
    except ValueError:
        # encode_path_segment RAISES on a traversal or separator rather than
        # escaping it. Caught here so a hostile or malformed IRI comes back as a
        # tool error the model can act on, instead of an exception crossing the
        # MCP boundary as a transport failure.
        return None
    return "/api/materials/" + "/".join(segments) + suffix


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
            "can be quoted."
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
        return _text(await request_control_plane("GET", "/api/document-sources/"))


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
        result = await request_control_plane("GET", path)
        doc = (result or {}).get("data")
        if not isinstance(doc, dict):
            return _text(result)

        content = doc.get("jawafdehi:content")
        media = doc.get("associatedMedia")
        overview: dict[str, Any] = {
            "iri": doc.get("@id"),
            "name": doc.get("name"),
            "publisher": (doc.get("publisher") or {}).get("name"),
            "date_published": doc.get("datePublished"),
            "fiscal_year_bs": doc.get("jawafdehi:fiscalYearBS"),
            # The citation chain. A reader has to be able to get back to the
            # publisher's own copy, or nothing quoted from here is checkable.
            "source_links": [
                {"role": m.get("jawafdehi:linkRole"), "url": m.get("contentUrl")}
                for m in (media if isinstance(media, list) else [])
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
            "pages, coverage_state tells you which pages are readable before you "
            "spend a call on them ('anchored' is read; anything else means the "
            "text will be empty or partial)."
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
        return _text(await request_control_plane("GET", path, params={"type": kind}))


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
            "blank. On a figure, every data point carries 'estimated': a true "
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

        result = await request_control_plane("GET", path, params=params)
        return _text(self._bounded(result, arguments.get("iri")))

    def _bounded(self, result: Any, iri: Any) -> Any:
        """Apply the text ceiling, and say so in the payload when it bites.

        Truncation is announced, never silent. A model handed a clipped table
        with no marker summarises it as the whole table, and the reader has no
        way to know a figure is missing.
        """
        if not isinstance(result, dict):
            return result
        data = result.get("data")
        if not isinstance(data, dict):
            return result

        for holder in ([data["item"]] if isinstance(data.get("item"), dict) else []) + [
            i for i in (data.get("items") or []) if isinstance(i, dict)
        ]:
            for field in ("text", "markdown"):
                value = holder.get(field)
                if isinstance(value, str):
                    clipped, did = _truncate(value, MAX_TEXT_CHARS)
                    if did:
                        holder[field] = clipped
                        holder["truncated"] = True
                        holder["full_length_chars"] = len(value)
        data["citation"] = {
            "document": iri,
            "retrieved_from": "jawafdehi.org",
            "note": (
                "Quote with the document IRI and the page or key. Page numbers "
                "are the printed folios, checkable against the original PDF."
            ),
        }
        return result
