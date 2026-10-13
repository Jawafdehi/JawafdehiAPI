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
import httpx

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
        # encode_material_parts quotes the source whole, so the separator in a
        # multi-segment source comes out percent-encoded. Verified against
        # production that both spellings resolve 200 — Django matches on the
        # decoded path and the edge passes %2F through — so the shared encoder
        # is used as-is rather than re-implemented per segment here.
        path = _material_path("https://jawafdehi.org/material/province/koshi/fy2083-21")
        assert path == "/api/materials/province%2Fkoshi/fy2083-21"

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
            new=AsyncMock(return_value={"success": True, "data": doc}),
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
            new=AsyncMock(return_value={"success": True, "data": doc}),
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
            new=AsyncMock(return_value={"success": True, "data": doc}),
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
            new=AsyncMock(return_value={"success": True, "data": {"item": {"markdown": big}}}),
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
            new=AsyncMock(return_value={"success": True, "data": {"item": {"text": "short"}}}),
        ):
            body = _payload(await ReadDocumentPartTool().execute({"iri": IRI, "page": 1}))
        assert body["data"]["item"]["text"] == "short"
        assert "truncated" not in body["data"]["item"]

    @pytest.mark.asyncio
    async def test_the_cap_is_per_call_not_per_item(self):
        # A 50-page range is fifty text fields. A per-item ceiling would be a
        # fifty-fold one — ~1M characters, the context bomb this surface exists
        # to prevent.
        each = "y" * (MAX_TEXT_CHARS // 2 + 100)
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "data": {"items": [{"text": each}, {"text": each}, {"text": each}]}
                }
            ),
        ):
            body = _payload(
                await ReadDocumentPartTool().execute(
                    {"iri": IRI, "from_page": 1, "to_page": 3}
                )
            )
        items = body["data"]["items"]
        assert sum(len(i["text"]) for i in items) <= MAX_TEXT_CHARS
        assert any(i.get("truncated") for i in items)
        assert "warning" in body["data"]["citation"]

    @pytest.mark.asyncio
    async def test_every_read_carries_a_citation(self):
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"success": True, "data": {"material": IRI, "item": {"text": "x"}}}),
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


class TestFailuresReachTheModelAsFailures:
    """Every failure must arrive as a tool error, not as data or a crash.

    A model cannot tell an error body from a payload unless the protocol says
    so, and an exception crossing the MCP boundary is reported as a transport
    fault rather than "the archive said no".
    """

    @pytest.mark.asyncio
    async def test_a_404_is_not_reshaped_into_an_overview(self):
        # The worst version of this: a 403 on a non-public document becoming
        # "this document has NOT been parsed, cite it as a holding" — confident,
        # wrong, and leaking that the document exists.
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(
                return_value={
                    "success": False,
                    "status_code": 404,
                    "data": {"detail": "Material not found."},
                }
            ),
        ):
            result = await GetDocumentOverviewTool().execute({"iri": IRI})
        assert result.is_error
        assert "readable" not in _payload(result)

    @pytest.mark.asyncio
    async def test_a_400_from_the_api_is_an_error_not_a_result(self):
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(
                return_value={
                    "success": False,
                    "status_code": 400,
                    "data": {"detail": "A page range may cover at most 50 pages."},
                }
            ),
        ):
            result = await ReadDocumentPartTool().execute(
                {"iri": IRI, "from_page": 1, "to_page": 60}
            )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_a_503_catalogue_is_an_error(self):
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"success": False, "status_code": 503, "data": {}}),
        ):
            assert (await ListDocumentSourcesTool().execute({})).is_error

    @pytest.mark.asyncio
    async def test_a_timeout_is_a_tool_error_not_a_raise(self):
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(side_effect=httpx.TimeoutException("slow")),
        ):
            result = await ListDocumentSourcesTool().execute({})
        assert result.is_error
        assert "did not respond in time" in _payload(result)["error"]

    @pytest.mark.asyncio
    async def test_a_transport_error_is_a_tool_error_not_a_raise(self):
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(side_effect=httpx.ConnectError("no route")),
        ):
            assert (await ListDocumentSourcesTool().execute({})).is_error

    @pytest.mark.asyncio
    async def test_a_path_rejected_downstream_is_a_tool_error(self):
        # _validate_api_path raises on a path that only resolves to a traversal
        # after repeated unquoting — it must not escape as a transport fault.
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(side_effect=ValueError("traversal segment")),
        ):
            assert (await ListDocumentSourcesTool().execute({})).is_error


class TestToleratesRealWorldShapes:
    @pytest.mark.asyncio
    async def test_a_single_associated_media_dict_keeps_its_citation(self):
        # associatedMedia is "a single dict or a list" across the corpus; a list
        # assumption drops the citation chain of every one-link document.
        doc = {
            "@id": IRI,
            "jawafdehi:content": {"pages": 1, "pages_read": 1},
            "associatedMedia": {"jawafdehi:linkRole": "RAW", "contentUrl": "https://x/y.pdf"},
        }
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"success": True, "data": doc}),
        ):
            body = _payload(await GetDocumentOverviewTool().execute({"iri": IRI}))
        assert body["source_links"][0]["url"] == "https://x/y.pdf"

    @pytest.mark.asyncio
    async def test_a_string_publisher_does_not_crash_the_tool(self):
        # The stored JSON-LD is free-form past @type/@id/name.
        doc = {"@id": IRI, "publisher": "Some Office", "associatedMedia": []}
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(return_value={"success": True, "data": doc}),
        ):
            body = _payload(await GetDocumentOverviewTool().execute({"iri": IRI}))
        assert body["publisher"] == "Some Office"

    @pytest.mark.asyncio
    async def test_the_citation_records_the_canonical_iri_not_the_argument(self):
        # The argument is whatever host the caller typed; attributing our text
        # to that host would be a false citation.
        with patch(
            "jawafdehi_mcp.tools.documents.request_control_plane",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "data": {"material": IRI, "item": {"text": "x"}},
                }
            ),
        ):
            body = _payload(
                await ReadDocumentPartTool().execute(
                    {"iri": "https://evil.example/material/ag/1", "page": 1}
                )
            )
        assert body["data"]["citation"]["document"] == IRI


class TestDescriptionsAreAccurate:
    def test_ocr_vision_pages_are_not_described_as_unreadable(self):
        # They ARE transcribed, by a vision model. Describing anything but
        # 'anchored' as empty makes a model skip readable pages.
        d = ListDocumentContentsTool().description
        assert "ocr_vision" in d and "both have text" in d

    def test_null_readable_is_explained_as_unknown(self):
        assert "does NOT mean zero" in ListDocumentSourcesTool().description
