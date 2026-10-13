"""Contract tests for the document-reading MCP tools.

These tools exist so a model can walk into a document instead of swallowing it,
so the tests that matter are the ones about what the tools REFUSE to do: return
a whole document, return a table with no marker that it was clipped, or hand
back an unreadable page that looks blank.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from jawafdehi_mcp.identity import ANONYMOUS_TOOL_NAMES
from jawafdehi_mcp.server import TOOL_MAP
from jawafdehi_mcp.tools.documents import (
    MAX_TEXT_CHARS,
    GetDocumentOverviewTool,
    ListDocumentContentsTool,
    ListDocumentSourcesTool,
    ReadDocumentPartTool,
    _material_path,
)

IRI = "https://jawafdehi.org/material/ciaa_annual_report/ee0b4f80b24b8665"
TOOLS = [
    ListDocumentSourcesTool(),
    GetDocumentOverviewTool(),
    ListDocumentContentsTool(),
    ReadDocumentPartTool(),
]


def _payload(result):
    return json.loads(result[0].text)


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_schemas_are_valid_and_registered(tool):
    Draft202012Validator.check_schema(tool.input_schema)
    assert TOOL_MAP[tool.name] is not None
    assert tool.description.strip()


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_document_tools_require_a_verified_identity(tool):
    # Deliberately NOT anonymous. A signed-out caller could otherwise walk 446
    # pages of 35 documents all day, and the archive has no per-caller quota yet.
    assert tool.name not in ANONYMOUS_TOOL_NAMES


class TestIriParsing:
    def test_parses_a_multi_segment_source(self):
        path = _material_path("https://jawafdehi.org/material/province/koshi/fy2083-21")
        assert path == "/api/materials/province/koshi/fy2083-21"

    def test_appends_the_suffix(self):
        assert _material_path(IRI, "/content").endswith("/ee0b4f80b24b8665/content")

    @pytest.mark.parametrize(
        "bad",
        [
            "not-an-iri",
            "https://jawafdehi.org/case/some-slug",
            "https://jawafdehi.org/material/",
            "https://jawafdehi.org/material/lonely",
        ],
    )
    def test_rejects_anything_that_is_not_a_material_iri(self, bad):
        assert _material_path(bad) is None

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://jawafdehi.org/material/ag/../../admin",
            "https://jawafdehi.org/material/ag/..",
            "https://jawafdehi.org/material/ag/ident?x=1",
            "https://jawafdehi.org/material/ag/ident#frag",
        ],
    )
    def test_a_hostile_iri_is_rejected_not_raised(self, hostile):
        # encode_path_segment RAISES on a traversal or separator. If that escapes
        # _material_path it crosses the MCP boundary as a transport failure
        # instead of an error the model can act on.
        assert _material_path(hostile) is None

    @pytest.mark.asyncio
    async def test_a_hostile_iri_reaches_the_caller_as_a_tool_error(self):
        caller = AsyncMock()
        with patch("jawafdehi_mcp.tools.documents.request_control_plane", new=caller):
            result = await ReadDocumentPartTool().execute(
                {"iri": "https://jawafdehi.org/material/ag/../../admin", "page": 1}
            )
        assert result.is_error
        caller.assert_not_awaited()


class TestOverview:
    @pytest.mark.asyncio
    async def test_unparsed_document_says_nothing_can_be_quoted(self):
        doc = {"@id": IRI, "name": {"ne": "…"}, "associatedMedia": []}
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"data": doc}),
        ):
            body = _payload(await GetDocumentOverviewTool().execute({"iri": IRI}))
        assert body["readable"] is False
        assert "NOT been parsed" in body["note"]

    @pytest.mark.asyncio
    async def test_unread_pages_are_called_out(self):
        # 446 held, 433 read. A model that misses this reports the 13 missing
        # pages as containing nothing.
        doc = {
            "@id": IRI,
            "associatedMedia": [],
            "jawafdehi:content": {"pages": 446, "pages_read": 433, "tables": 145},
        }
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"data": doc}),
        ):
            body = _payload(await GetDocumentOverviewTool().execute({"iri": IRI}))
        assert body["readable"] is True
        assert "13 of 446" in body["note"]

    @pytest.mark.asyncio
    async def test_carries_the_source_links_for_citation(self):
        doc = {
            "@id": IRI,
            "jawafdehi:content": {"pages": 1, "pages_read": 1},
            "associatedMedia": [
                {"jawafdehi:linkRole": "SOURCE_PAGE", "contentUrl": "https://ciaa.gov.np/x"}
            ],
        }
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"data": doc}),
        ):
            body = _payload(await GetDocumentOverviewTool().execute({"iri": IRI}))
        assert body["source_links"][0]["url"] == "https://ciaa.gov.np/x"

    @pytest.mark.asyncio
    async def test_a_bad_iri_is_an_error_not_a_request(self):
        caller = AsyncMock()
        with patch("jawafdehi_mcp.tools.documents.request_control_plane", new=caller):
            result = await GetDocumentOverviewTool().execute({"iri": "nope"})
        assert result.is_error
        caller.assert_not_awaited()


class TestReadPart:
    @pytest.mark.asyncio
    async def test_exactly_one_selector_is_required(self):
        tool = ReadDocumentPartTool()
        for args in (
            {"iri": IRI},
            {"iri": IRI, "page": 1, "table": "p0030-t1"},
            {"iri": IRI, "page": 1, "from_page": 2, "to_page": 3},
        ):
            result = await tool.execute(args)
            assert result.is_error, args

    @pytest.mark.asyncio
    async def test_half_a_range_is_refused(self):
        result = await ReadDocumentPartTool().execute({"iri": IRI, "from_page": 10})
        assert result.is_error

    @pytest.mark.asyncio
    async def test_selectors_map_to_the_right_query(self):
        cases = [
            ({"page": 30}, {"type": "page", "num": 30}),
            ({"from_page": 30, "to_page": 32}, {"type": "page", "from": 30, "to": 32}),
            ({"table": "p0030-t1"}, {"type": "table", "id": "p0030-t1"}),
            ({"figure": "p0030-f1"}, {"type": "figure", "id": "p0030-f1"}),
        ]
        for args, expected in cases:
            caller = AsyncMock(return_value={"data": {}})
            with patch("jawafdehi_mcp.tools.documents.request_control_plane", new=caller):
                await ReadDocumentPartTool().execute({"iri": IRI, **args})
            assert caller.await_args.kwargs["params"] == expected

    @pytest.mark.asyncio
    async def test_an_oversized_table_is_clipped_and_says_so(self):
        # Silent truncation is the failure: a model handed a clipped table with
        # no marker summarises it as the whole table.
        big = "x" * (MAX_TEXT_CHARS + 500)
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"data": {"item": {"markdown": big}}}),
        ):
            body = _payload(
                await ReadDocumentPartTool().execute({"iri": IRI, "table": "p0030-t1"})
            )
        item = body["data"]["item"]
        assert len(item["markdown"]) == MAX_TEXT_CHARS
        assert item["truncated"] is True
        assert item["full_length_chars"] == MAX_TEXT_CHARS + 500

    @pytest.mark.asyncio
    async def test_text_within_the_cap_is_untouched(self):
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"data": {"item": {"text": "short"}}}),
        ):
            body = _payload(await ReadDocumentPartTool().execute({"iri": IRI, "page": 1}))
        assert body["data"]["item"]["text"] == "short"
        assert "truncated" not in body["data"]["item"]

    @pytest.mark.asyncio
    async def test_a_range_caps_every_page_it_returns(self):
        big = "y" * (MAX_TEXT_CHARS + 1)
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"data": {"items": [{"text": big}, {"text": big}]}}),
        ):
            body = _payload(
                await ReadDocumentPartTool().execute(
                    {"iri": IRI, "from_page": 1, "to_page": 2}
                )
            )
        assert all(i["truncated"] for i in body["data"]["items"])

    @pytest.mark.asyncio
    async def test_every_read_carries_a_citation(self):
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"data": {"item": {"text": "x"}}}),
        ):
            body = _payload(await ReadDocumentPartTool().execute({"iri": IRI, "page": 1}))
        assert body["data"]["citation"]["document"] == IRI


class TestListContents:
    @pytest.mark.asyncio
    async def test_kind_is_validated_before_any_request(self):
        caller = AsyncMock()
        with patch("jawafdehi_mcp.tools.documents.request_control_plane", new=caller):
            result = await ListDocumentContentsTool().execute(
                {"iri": IRI, "kind": "paragraph"}
            )
        assert result.is_error
        caller.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passes_the_kind_through_as_type(self):
        caller = AsyncMock(return_value={"data": {}})
        with patch("jawafdehi_mcp.tools.documents.request_control_plane", new=caller):
            await ListDocumentContentsTool().execute({"iri": IRI, "kind": "table"})
        assert caller.await_args.kwargs["params"] == {"type": "table"}


class TestDescriptions:
    def test_the_two_general_offices_are_disambiguated_for_the_model(self):
        # The model reads this description instead of guessing from the token.
        d = ListDocumentSourcesTool().description
        assert "ATTORNEY" in d and "AUDITOR" in d

    def test_read_tool_warns_against_addressing_by_ordinal(self):
        assert "ordinal" in ReadDocumentPartTool().description

    def test_read_tool_explains_the_estimated_flag(self):
        assert "estimated" in ReadDocumentPartTool().description
