"""Searching inside one document.

``GET /api/materials/<source>/<ident>/search?q=…`` answers the question the
content plane could not: *where does this document say this?* ``…/content?q=``
reached a table through its caption and a figure through its title, so a model
could find a table by what it was CALLED and could not find a paragraph by what
it SAID — on a 446-page report, with no move left but reading page ranges until
something turned up.

The cases that matter here are the ones that would be silently wrong: a
Devanagari query that has to survive the ETag header, a hit in page text that no
caption mentions, a snippet that is a window rather than the page, the
distinction between matching the words and matching the page NUMBER, and the
fact that what is searched is the stored (redacted) text and never the original.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_content_search.py
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from jawafdehi_shared.search.transliterate import backend_available
from materials.content_api import MAX_SEARCH_HITS, locate_match, snippet_for
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import (
    DocumentContent,
    DocumentFigure,
    DocumentPage,
    DocumentTable,
    Material,
    Policy,
    Visibility,
)

SOURCE = "ciaa_annual_report"
IDENT = "ee0b4f80b24b8665"
IRI = f"https://jawafdehi.org/material/{SOURCE}/{IDENT}"
URL = f"/api/materials/{SOURCE}/{IDENT}/search"

#: The acceptance scenario, and it is the REAL one: page 294 of the published
#: 35th CIAA annual report carries the heading "३.५.१ घुस (रिसवत) सम्बन्धी
#: मुद्दा" three times over, and table ``p0294-t1`` beside it is captioned
#: "घुस (रिसवत) सम्बन्धी मुद्दाका प्रतिवादीको विवरण". Checked against
#: ``api.jawafdehi.org`` through the existing ``?type=page&num=294`` route, so
#: the fixture is a trimmed copy of the document rather than an invention that
#: happens to pass.
BRIBERY_PAGE = (
    "३.५ आर्थिक वर्ष २०८१/८२ मा दायर गरिएका मुद्दाको कसुरगत प्रवृत्ति\n"
    "३.५.१ घुस (रिसवत) सम्बन्धी मुद्दा\n"
    "आयोगले आर्थिक वर्ष २०८१/८२ मा विशेष अदालतमा दायर गरेका मुद्दामध्ये "
    "सबैभन्दा बढी घुस रिसवत सम्बन्धी रहेका छन्। यस सम्बन्धमा आयोगले विशेष "
    "अनुगमन गरेको थियो।"
)
BRIBERY_CAPTION = "घुस (रिसवत) सम्बन्धी मुद्दाका प्रतिवादीको विवरण"
#: The subject of the rows is NOT in the caption, which is what the markdown
#: pass is for — in the real table the defendants' offices are listed here and
#: named nowhere else.
BRIBERY_TABLE = (
    "| विषय | संख्या |\n| --- | --- |\n| घुस रिसवत | ८९ |\n| गैरकानुनी सम्पत्ति | ४२ |"
)


def _seed(**material_kwargs) -> Material:
    material = Material.objects.create(
        iri=IRI,
        material_type="document",
        source=SOURCE,
        ident=IDENT,
        data={
            "@context": MATERIAL_CONTEXT,
            "@type": "DigitalDocument",
            "@id": IRI,
            "name": {"ne": "पैँतिसौँ वार्षिक प्रतिवेदन"},
        },
        **material_kwargs,
    )
    content = DocumentContent.objects.create(
        material=material,
        doc_id="ciaa-2081-82",
        dataset="damo-da/ciaa-annual-reports",
        dataset_revision="c3c6c673",
        page_count=446,
        text_source="likhit+redacted",
        transcript_verdict="clean",
        figures_verdict="trustworthy",
        n_tables=2,
        n_figures=1,
        n_points=2,
        pages_with_text=2,
    )
    DocumentPage.objects.create(
        content=content,
        page_no=31,
        uid="ciaa-2081-82#p0031",
        text="आयोगमा दर्ता भएका उजुरीको संख्या १९४८८ रहेको छ।",
        coverage_state="anchored",
        anchor_fidelity="tight",
        ocr_source="pdf_text_layer",
        chars=46,
    )
    DocumentPage.objects.create(
        content=content,
        page_no=294,
        uid="ciaa-2081-82#p0294",
        text=BRIBERY_PAGE,
        coverage_state="anchored",
        anchor_fidelity="tight",
        ocr_source="pdf_text_layer",
        n_table_blocks=1,
        chars=len(BRIBERY_PAGE),
    )
    # Held but unread. Its empty text must not be mistaken for a blank page, and
    # a folio query still has to reach it.
    DocumentPage.objects.create(
        content=content,
        page_no=295,
        uid="ciaa-2081-82#p0295",
        text="",
        coverage_state="ocr_unread",
        chars=0,
    )
    DocumentTable.objects.create(
        content=content,
        ordinal=1,
        key="p0031-t1",
        uid="ciaa-2081-82#p0031#t1",
        page_no=31,
        caption="तालिका २.१ आयोगमा दर्ता भएका उजुरीको संख्या",
        header=["आ.व.", "संख्या"],
        n_rows=9,
        n_cols=2,
        fidelity="tight",
        markdown="| आ.व. | संख्या |\n| --- | --- |\n| २०८१/८२ | १९४८८ |",
    )
    # The bribery table, with the caption the published report actually gives
    # it. Its ROWS carry subjects the caption does not name, which is what the
    # markdown pass reaches and a caption-only filter cannot.
    DocumentTable.objects.create(
        content=content,
        ordinal=2,
        key="p0294-t1",
        uid="ciaa-2081-82#p0294#t1",
        page_no=294,
        caption=BRIBERY_CAPTION,
        header=["विषय", "संख्या"],
        n_rows=2,
        n_cols=2,
        fidelity="tight",
        markdown=BRIBERY_TABLE,
    )
    DocumentFigure.objects.create(
        content=content,
        ordinal=1,
        key="p0294-f1",
        uid="ciaa-2081-82#p0294#f1",
        page_no=294,
        title="मुद्दाको प्रकृति",
        chart_type="pie",
        notes="घुस रिसवतको हिस्सा छापिएको तालिकासँग मिलाइएको।",
    )
    return material


class SearchHitTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        _seed()

    def _hits(self, **params):
        response = self.client.get(URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_a_devanagari_query_lands_on_the_page_and_its_table(self):
        """The acceptance case: घुस finds page 294 AND the table beside it."""
        body = self._hits(q="घुस")
        found = {(hit["kind"], hit["page_no"], hit["key"]) for hit in body["hits"]}
        self.assertIn(("page", 294, None), found)
        self.assertIn(("table", 294, "p0294-t1"), found)
        # Mixed kinds in ONE response is the point — three listings would make
        # the caller reassemble the answer, and a caller that only knew to ask
        # for tables would never learn the paragraph exists.
        self.assertEqual({hit["kind"] for hit in body["hits"]}, {"page", "table", "figure"})

    def test_every_hit_carries_a_snippet(self):
        for hit in self._hits(q="घुस")["hits"]:
            self.assertTrue(hit["snippet"], hit)
            self.assertIn("घुस", hit["snippet"])

    def test_the_search_does_not_return_the_document(self):
        """Snippets, never pages — the whole reason this endpoint exists."""
        body = self._hits(q="घुस")
        page_hit = next(h for h in body["hits"] if h["kind"] == "page")
        # No transcript field at all, and the snippet is a strict window into a
        # page that is itself longer. A hit that carried `text` would be the
        # 3.96 MB fetch this route replaces, one page at a time.
        self.assertNotIn("text", page_hit)
        self.assertNotIn("markdown", page_hit)
        self.assertLess(len(page_hit["snippet"]), len(BRIBERY_PAGE))
        self.assertTrue(page_hit["snippet"].endswith("…"))

    def test_a_hit_in_page_text_alone_is_found(self):
        """The gap being closed: prose no caption mentions."""
        body = self._hits(q="अनुगमन")
        self.assertEqual([h["kind"] for h in body["hits"]], ["page"])
        hit = body["hits"][0]
        self.assertEqual((hit["page_no"], hit["field"], hit["match"]), (294, "text", "literal"))
        self.assertIn("अनुगमन", hit["snippet"])

    def test_a_hit_in_table_rows_alone_is_found(self):
        """A table whose caption is just "तालिका ४.२" is still reachable."""
        body = self._hits(q="गैरकानुनी")
        self.assertEqual([h["key"] for h in body["hits"]], ["p0294-t1"])
        self.assertEqual(body["hits"][0]["field"], "markdown")

    def test_a_caption_hit_is_reported_as_a_caption(self):
        """The existing caption behaviour, now with a field saying so."""
        body = self._hits(q="तालिका")
        tables = [h for h in body["hits"] if h["kind"] == "table"]
        self.assertEqual([(h["key"], h["field"]) for h in tables], [("p0031-t1", "caption")])

    def test_the_caption_is_preferred_over_the_rows_as_the_quotation(self):
        """Both carry घुस; the caption is the better thing to quote."""
        table = next(h for h in self._hits(q="घुस")["hits"] if h["kind"] == "table")
        self.assertEqual(table["field"], "caption")
        self.assertEqual(table["snippet"], BRIBERY_CAPTION)

    def test_one_hit_per_row_even_when_two_fields_match(self):
        # Otherwise a report's 12 bribery tables read as 24 findings.
        keys = [h["key"] for h in self._hits(q="घुस")["hits"] if h["kind"] == "table"]
        self.assertEqual(keys, ["p0294-t1"])

    def test_a_figure_is_reachable_through_its_notes(self):
        body = self._hits(q="हिस्सा")
        self.assertEqual([(h["kind"], h["field"]) for h in body["hits"]], [("figure", "notes")])

    def test_hits_come_back_in_page_order(self):
        body = self._hits(q="संख्या")
        pages = [hit["page_no"] for hit in body["hits"]]
        self.assertEqual(pages, sorted(pages))

    def test_the_page_is_reported_before_what_is_printed_on_it(self):
        kinds = [h["kind"] for h in self._hits(q="घुस")["hits"] if h["page_no"] == 294]
        self.assertEqual(kinds, ["page", "table", "figure"])

    def test_a_hit_carries_what_it_takes_to_fetch_it_whole(self):
        """page_no and key are the arguments ``…/content`` accepts."""
        body = self._hits(q="घुस")
        table = next(h for h in body["hits"] if h["kind"] == "table")
        follow_up = self.client.get(
            f"/api/materials/{SOURCE}/{IDENT}/content",
            {"type": "table", "id": table["key"]},
        )
        self.assertEqual(follow_up.status_code, status.HTTP_200_OK)
        self.assertEqual(follow_up.data["item"]["markdown"], BRIBERY_TABLE)

    def test_a_page_hit_carries_its_coverage_state(self):
        """A hit on vision-read text is a weaker claim and has to say so."""
        hit = next(h for h in self._hits(q="घुस")["hits"] if h["kind"] == "page")
        self.assertEqual(hit["coverage_state"], "anchored")
        self.assertEqual(hit["ocr_source"], "pdf_text_layer")

    def test_a_term_the_document_does_not_hold_is_an_empty_result(self):
        body = self._hits(q="काठमाडौं महानगरपालिका")
        self.assertEqual((body["total"], body["hits"]), (0, []))
        # Echoed, so an empty result reads as "this document does not say that"
        # rather than as a request that never carried a query.
        self.assertEqual(body["q"], "काठमाडौं महानगरपालिका")


class MatchKindTests(APITestCase):
    """Literal, phonetic and folio are three different claims."""
    databases = "__all__"


    def setUp(self):
        _seed()

    def test_a_latin_query_reaches_devanagari_text(self):
        if not backend_available():
            self.skipTest("transliteration backend unavailable")
        response = self.client.get(URL, {"q": "ghus"})
        hits = response.data["hits"]
        self.assertTrue(hits, "a romanized query should reach the Devanagari text")
        self.assertIn(294, {hit["page_no"] for hit in hits})
        self.assertEqual({hit["match"] for hit in hits}, {"phonetic"})

    def test_a_phonetic_hit_snippets_the_word_it_matched(self):
        if not backend_available():
            self.skipTest("transliteration backend unavailable")
        page = next(
            h for h in self.client.get(URL, {"q": "ghus"}).data["hits"]
            if h["kind"] == "page"
        )
        # Not the opening of the page — the span is recovered word by word, so
        # the snippet is centred on the word that actually matched.
        self.assertIn("घुस", page["snippet"])

    def test_a_bare_number_is_an_exact_folio(self):
        body = self.client.get(URL, {"q": "294"}).data
        self.assertEqual({hit["page_no"] for hit in body["hits"]}, {294})
        self.assertEqual({hit["match"] for hit in body["hits"]}, {"folio"})

    def test_a_folio_query_reaches_a_page_we_could_not_read(self):
        """An unread page has no text to match; its number still addresses it."""
        body = self.client.get(URL, {"q": "295"}).data
        hit = next(h for h in body["hits"] if h["kind"] == "page")
        self.assertEqual(hit["coverage_state"], "ocr_unread")
        self.assertEqual(hit["snippet"], "")

    def test_word_plus_folio_still_narrows(self):
        """The ``ujuri 31`` form ``_matches`` has always had, preserved here."""
        body = self.client.get(URL, {"q": "उजुरीको 31"}).data
        self.assertEqual({hit["page_no"] for hit in body["hits"]}, {31})
        self.assertTrue(body["hits"])

    def test_word_plus_folio_does_not_match_the_word_on_another_page(self):
        body = self.client.get(URL, {"q": "घुस 31"}).data
        self.assertEqual(body["total"], 0)


class LegacyListingFilterTests(APITestCase):
    """``…/content?q=`` is unchanged by the refactor underneath it."""
    databases = "__all__"


    def setUp(self):
        _seed()

    def _tables(self, query):
        response = self.client.get(
            f"/api/materials/{SOURCE}/{IDENT}/content", {"type": "table", "q": query}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [item["key"] for item in response.data["items"]]

    def test_a_caption_still_narrows_the_table_listing(self):
        self.assertEqual(self._tables("उजुरीको"), ["p0031-t1"])

    def test_a_bare_folio_still_narrows_the_table_listing(self):
        self.assertEqual(self._tables("294"), ["p0294-t1"])

    def test_the_word_plus_folio_form_still_works(self):
        self.assertEqual(self._tables("उजुरीको 31"), ["p0031-t1"])

    def test_the_listing_filter_does_not_gain_the_table_body(self):
        """``…/content?q=`` matches CAPTIONS; only ``…/search`` reads markdown.

        Widening the listing filter to the markdown would have been a silent
        behaviour change on a shipped route — "गैरकानुनी" is in the bribery
        table's rows and in no caption.
        """
        self.assertEqual(self._tables("गैरकानुनी"), [])

    def test_a_page_listing_still_refuses_q(self):
        response = self.client.get(
            f"/api/materials/{SOURCE}/{IDENT}/content", {"type": "page", "q": "घुस"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The refusal now has somewhere to send the caller.
        self.assertIn("q narrows tables and figures", response.data["detail"])


class SearchArgumentTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        _seed()

    def test_q_is_required(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("q is required", response.data["detail"])

    def test_a_blank_q_is_refused_rather_than_meaning_everything(self):
        response = self.client.get(URL, {"q": "   "})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_overlong_q_is_refused(self):
        response = self.client.get(URL, {"q": "घ" * 201})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("at most 200 characters", response.data["detail"])

    def test_limit_bounds_the_hits_but_not_the_total(self):
        body = self.client.get(URL, {"q": "संख्या", "limit": 1}).data
        self.assertEqual(len(body["hits"]), 1)
        self.assertEqual(body["count"], 1)
        self.assertGreater(body["total"], 1)
        self.assertTrue(body["truncated"])

    def test_an_untruncated_result_says_so(self):
        body = self.client.get(URL, {"q": "अनुगमन"}).data
        self.assertFalse(body["truncated"])
        self.assertEqual(body["total"], body["count"])

    def test_a_limit_over_the_cap_is_refused_not_lowered(self):
        response = self.client.get(URL, {"q": "घुस", "limit": MAX_SEARCH_HITS + 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(str(MAX_SEARCH_HITS), response.data["detail"])

    def test_a_nonsense_limit_is_refused(self):
        for bad in ("0", "-3", "many"):
            with self.subTest(limit=bad):
                response = self.client.get(URL, {"q": "घुस", "limit": bad})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_published_schema_documents_the_arguments(self):
        # Hand-written OpenApiParameters on a plain @api_view, same as the
        # content route: nothing but this notices them drifting from the view.
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        operation = schema["paths"]["/api/materials/{source}/{ident}/search/"]["get"]
        documented = {p["name"] for p in operation["parameters"]}
        self.assertEqual(documented, {"source", "ident", "q", "limit"})


class SearchCachingTests(APITestCase):
    databases = "__all__"

    def setUp(self):
        self.material = _seed()

    def test_the_etag_is_ascii_for_a_devanagari_query(self):
        """The trap the content route already documents, on a route whose
        query is Devanagari by default rather than by exception."""
        etag = self.client.get(URL, {"q": "घुस"})["ETag"]
        etag.encode("ascii")
        self.assertNotIn("=?utf-8?", etag)

    def test_the_etag_comes_back_as_a_304(self):
        etag = self.client.get(URL, {"q": "घुस"})["ETag"]
        again = self.client.get(URL, {"q": "घुस"}, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(again.status_code, status.HTTP_304_NOT_MODIFIED)

    def test_a_different_query_is_a_different_representation(self):
        first = self.client.get(URL, {"q": "घुस"})["ETag"]
        second = self.client.get(URL, {"q": "उजुरीको"})["ETag"]
        self.assertNotEqual(first, second)

    def test_a_search_tag_never_matches_a_content_tag(self):
        """One document, one ``ingested_at`` — the variant is what keeps the two
        routes apart for a client echoing a tag by hand."""
        content = self.client.get(
            f"/api/materials/{SOURCE}/{IDENT}/content", {"type": "table", "q": "घुस"}
        )["ETag"]
        response = self.client.get(URL, {"q": "घुस"}, HTTP_IF_NONE_MATCH=content)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_malformed_request_is_still_400_not_304(self):
        etag = self.client.get(URL, {"q": "घुस"})["ETag"]
        response = self.client.get(URL, {"q": ""}, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SearchAbsenceAndVisibilityTests(APITestCase):
    databases = "__all__"

    def test_a_material_with_no_content_is_404(self):
        Material.objects.create(
            iri="https://jawafdehi.org/material/ag/plain",
            material_type="document",
            source="ag",
            ident="plain",
            data={"@context": MATERIAL_CONTEXT, "@type": "DigitalDocument"},
        )
        response = self.client.get("/api/materials/ag/plain/search", {"q": "घुस"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Same distinction the content route draws: *we have not looked inside
        # this* is not *we looked and it does not say that*.
        self.assertIn("extractable", response.data)

    def test_an_unknown_material_is_404(self):
        response = self.client.get("/api/materials/ag/nope/search", {"q": "घुस"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_private_document_is_not_searchable_by_anon(self):
        _seed(visibility_policy=Policy.CASE_GATED, visibility=Visibility.PRIVATE)
        response = self.client.get(URL, {"q": "घुस"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Authorization", response["Vary"])

    def test_a_caseworker_can_search_what_anon_cannot(self):
        _seed(visibility_policy=Policy.CASE_GATED, visibility=Visibility.PRIVATE)
        user = get_user_model().objects.create_user(
            username="sita", password="x", email="sita@example.org"
        )
        user.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
        self.client.force_authenticate(user=user)
        response = self.client.get(URL, {"q": "घुस"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["hits"])


class RedactionTests(APITestCase):
    """What is searched is the stored text, and the stored text is the redacted one."""
    databases = "__all__"


    def test_the_original_is_not_searched(self):
        material = _seed()
        # ``source_text`` is the unredacted original, kept behind
        # ``materials.view_unredacted``. A search that reached it would publish
        # through a snippet exactly what the redactor removed from the page.
        material.source_text = BRIBERY_PAGE + " सम्पर्क ९८४१००००००।"
        material.save(update_fields=["source_text"])
        body = self.client.get(URL, {"q": "९८४१००००००"}).data
        self.assertEqual(body["total"], 0)

    def test_a_search_can_only_surface_what_the_page_route_already_serves(self):
        _seed()
        snippet = next(
            h for h in self.client.get(URL, {"q": "घुस"}).data["hits"]
            if h["kind"] == "page"
        )["snippet"]
        page = self.client.get(
            f"/api/materials/{SOURCE}/{IDENT}/content", {"type": "page", "num": 294}
        ).data["item"]["text"]
        # Compared with the document's own line breaks collapsed, because that
        # is the one thing a snippet changes about the text it quotes.
        self.assertIn(snippet.strip("…"), " ".join(page.split()))


class McpSearchToolTests(APITestCase):
    """The same search, through the MCP tool, against the same live view.

    End to end on purpose. The tool's own unit tests (``tests/mcp/``) mock the
    control plane, which proves the tool builds the right request and nothing
    about whether that request answers — and the gap between those two is
    exactly where a parameter name drifts and a model is told a document says
    nothing.

    It runs in two passes rather than forwarding from inside the coroutine, and
    that is forced rather than chosen: ``connections`` is an ``asgiref.local``
    keyed by the running TASK, so an ORM call made inside ``asyncio.run`` opens
    a SECOND sqlite connection while the test's own transaction is still open,
    and the write it is trying to read is locked. The first pass learns what
    request the tool builds; that exact request is then executed against the
    real route on this thread; the second pass hands the real answer back to
    the tool to shape. Nothing is assumed about the request in between — it is
    the recorded one — so a rename on either side still fails this.
    """

    databases = "__all__"

    def setUp(self):
        _seed()

    def _run(self, iri: str = IRI, **arguments):
        """``(recorded request, tool output)`` for one real call."""
        from jawafdehi_mcp.tools.documents import SearchDocumentTool

        tool = SearchDocumentTool()
        args = {"iri": iri, **arguments}
        seen: dict = {}

        def _stub(envelope):
            async def _call(method, path, *, params=None, **_kwargs):
                seen.update(method=method, path=path, params=params)
                return envelope

            return _call

        target = "jawafdehi_mcp.tools.documents.request_control_plane"
        with patch(target, new=_stub({"success": True, "data": {}})):
            asyncio.run(tool.execute(args))
        if not seen:
            # The tool refused before it ever called out (a bad IRI, a missing
            # q). That is an answer too, and re-running it would hide it.
            with patch(target, new=_stub({"success": True, "data": {}})):
                return seen, asyncio.run(tool.execute(args))

        response = self.client.get(seen["path"], seen["params"] or {})
        envelope = {
            "success": response.status_code < 400,
            "status_code": response.status_code,
            "data": response.data,
        }
        with patch(target, new=_stub(envelope)):
            return seen, asyncio.run(tool.execute(args))

    def test_the_tool_finds_the_same_page_and_table(self):
        seen, result = self._run(q="घुस")
        self.assertEqual(seen["path"], f"/api/materials/{SOURCE}/{IDENT}/search")
        self.assertEqual(seen["params"], {"q": "घुस"})
        hits = json.loads(result[0].text)["data"]["hits"]
        found = {(hit["kind"], hit["page_no"], hit["key"]) for hit in hits}
        self.assertIn(("page", 294, None), found)
        self.assertIn(("table", 294, "p0294-t1"), found)

    def test_the_tool_returns_snippets_not_the_document(self):
        _, result = self._run(q="घुस")
        for hit in json.loads(result[0].text)["data"]["hits"]:
            self.assertTrue(hit["snippet"])
            self.assertNotIn("text", hit)
            self.assertNotIn("markdown", hit)

    def test_the_tool_attaches_the_citation_contract(self):
        _, result = self._run(q="घुस")
        citation = json.loads(result[0].text)["data"]["citation"]
        self.assertEqual(citation["document"], IRI)
        # A snippet is a window, and a model quoting one as a sentence of the
        # document would be quoting a fragment as a whole.
        self.assertIn("window", citation["note"])
        self.assertNotIn("warning", citation)

    def test_the_tool_forwards_a_limit(self):
        seen, result = self._run(q="संख्या", limit=1)
        self.assertEqual(seen["params"], {"q": "संख्या", "limit": 1})
        data = json.loads(result[0].text)["data"]
        self.assertTrue(data["truncated"])
        self.assertIn("narrow the query", data["citation"]["warning"])

    def test_a_404_reaches_the_model_as_an_error_not_as_data(self):
        # Otherwise the model treats "Material not found." as a search result
        # and reports the document as holding nothing.
        _, result = self._run(iri="https://jawafdehi.org/material/ag/nope", q="घुस")
        self.assertTrue(result.is_error)


class SnippetTests(APITestCase):
    """The snippet helper, away from the HTTP envelope."""
    databases = "__all__"


    def test_a_window_is_marked_at_both_cut_ends(self):
        body = "क" * 400 + "घुस" + "ख" * 400
        span = locate_match(body, 1, "घुस")
        snippet = snippet_for(body, span)
        self.assertTrue(snippet.startswith("…") and snippet.endswith("…"))
        self.assertIn("घुस", snippet)
        self.assertLess(len(snippet), len(body))

    def test_a_short_field_is_quoted_whole_with_no_ellipsis(self):
        span = locate_match("तालिका ४.२ घुस", 1, "घुस")
        self.assertEqual(snippet_for("तालिका ४.२ घुस", span), "तालिका ४.२ घुस")

    def test_the_document_s_own_line_breaks_are_collapsed(self):
        span = locate_match(BRIBERY_TABLE, 294, "गैरकानुनी")
        self.assertNotIn("\n", snippet_for(BRIBERY_TABLE, span))

    def test_an_unlocated_span_snippets_the_opening(self):
        """A folio match is not a claim about the text, so it shows the head."""
        body = "क" * 40 + "घुस"
        span = locate_match(body, 294, "294")
        self.assertEqual(span.how, "folio")
        self.assertTrue(snippet_for(body, span).startswith("क"))

    def test_an_empty_field_snippets_to_nothing_rather_than_an_ellipsis(self):
        self.assertEqual(snippet_for("", locate_match("", 294, "294")), "")
