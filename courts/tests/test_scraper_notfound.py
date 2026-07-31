"""Not-found detection across all four court portals.

The register sweep asks a portal "does docket N exist?" thousands of times, so
mistaking a not-found page for a hit is the single most damaging failure mode
available: it does not error, it mass-creates empty cases and (via
``_replace_entities``) deletes real parties.

Every fixture here is a real response captured from the live portal — one found
and one not-found per tier — so the predicate is tested against what the courts
actually serve rather than a guess at it.
"""

import pathlib

import pytest

from courts.scraper import district, high, special, supreme
from courts.scraper.errors import UnexpectedPage
from courts.scraper.registry import REGISTRY

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "notfound"


def _html(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# tier -> (parser, found fixture, not-found fixture)
_TIERS = {
    "special": (special.parse_detail, "special_found.html", "special_missing.html"),
    "district": (district.parse_district_detail, "district_found.html", "district_missing.html"),
    "high": (high.parse_high_detail, "high_found.html", "high_missing.html"),
    # Supreme's not-found is the search LIST with "Total 0 Records Found"; its
    # found page is only reachable via the stage-2 link (see TestSupremeTwoStage).
    "supreme": (supreme.parse_supreme_detail, "supreme_detail_found.html", "supreme_search_missing.html"),
}


@pytest.mark.parametrize("tier", sorted(_TIERS))
def test_found_page_identifies_a_case(tier):
    parser, found, _ = _TIERS[tier]
    assert parser(_html(found)).identifies_a_case() is True


@pytest.mark.parametrize("tier", sorted(_TIERS))
def test_not_found_page_does_not_identify_a_case(tier):
    parser, _, missing = _TIERS[tier]
    assert parser(_html(missing)).identifies_a_case() is False


@pytest.mark.parametrize("tier", sorted(_TIERS))
def test_extra_data_alone_would_have_passed_a_not_found_page(tier):
    """Why ``identifies_a_case`` ignores extra_data.

    supreme/district/high always emit ``enrichment_hearings`` /
    ``enrichment_timeline`` keys, so an empty parse still has a TRUTHY
    ``extra_data``. The old ``core or extra or entities`` guard therefore passed
    a not-found page on three of the four courts — marking the case enriched and
    dropping its parties. This test pins that distinction so the weaker guard
    cannot come back.
    """
    parser, _, missing = _TIERS[tier]
    parsed = parser(_html(missing))
    old_guard_would_apply = bool(parsed.core_fields or parsed.extra_data or parsed.entities)
    assert parsed.identifies_a_case() is False
    if tier != "special":
        assert old_guard_would_apply is True, "fixture no longer exercises the regression"


class TestSupremeTwoStage:
    """Searching Supreme by case number returns a result LIST, not the detail page."""

    def test_search_result_yields_a_detail_link(self):
        href = supreme.parse_search_result_link(_html("supreme_search_found.html"))
        assert href and "caseno=" in href and "mode=view" in href

    def test_no_records_found_yields_no_link(self):
        assert supreme.parse_search_result_link(_html("supreme_search_missing.html")) is None

    def test_follows_the_row_that_matches_the_docket_not_the_first(self):
        """Row selection keys on the docket cell, not document order.

        The result list is N-row capable — each href carries its own ``num=``
        ordinal. Taking row 1 blind would enrich the requested case with a
        DIFFERENT docket's parties and verdict, which no error would surface.
        """
        html = _html("supreme_search_multi.html")
        first = supreme.parse_search_result_link(html, "081-WO-0081")
        second = supreme.parse_search_result_link(html, "081-WO-0001")
        assert "caseno=307125" in first
        assert "caseno=305697" in second

    def test_a_list_without_our_docket_is_a_miss(self):
        # The portal answering with rows that are all other cases means it does
        # not have ours — not "take whatever is on top".
        assert supreme.parse_search_result_link(
            _html("supreme_search_multi.html"), "081-WO-9999"
        ) is None

    def test_a_page_that_is_not_a_result_list_raises(self):
        """A WAF challenge / maintenance page must not read as "no such docket".

        Both arrive as HTTP 200. If they were conflated, one soft block would
        write a whole sweep budget's worth of false absences into RegisterProbe
        and report a clean run. The detail page stands in for any non-list 200:
        it carries no record count.
        """
        with pytest.raises(UnexpectedPage):
            supreme.parse_search_result_link(_html("supreme_detail_found.html"))
        with pytest.raises(UnexpectedPage):
            supreme.parse_search_result_link("<html><body>blocked</body></html>")

    def test_parsing_the_search_list_as_a_detail_page_finds_nothing(self):
        # The pre-fix behaviour: stage 1's output is not a detail page, so the
        # enrichment came back empty for every Supreme case.
        parsed = supreme.parse_supreme_detail(_html("supreme_search_found.html"))
        assert parsed.identifies_a_case() is False

    def test_crawl_detail_follows_the_link_and_parses(self):
        calls = []

        def fake_fetch(url, data=None):
            calls.append(url)
            return _html("supreme_search_found.html") if data else _html("supreme_detail_found.html")

        parsed = REGISTRY["supreme"].crawl_detail(fake_fetch, "supreme", "081-WO-0001")
        assert len(calls) == 2, "must search, then follow the result link"
        assert parsed is not None and parsed.identifies_a_case()
        assert parsed.core_fields.get("registration_date_bs") == "2081-04-03"

    def test_crawl_detail_stops_at_stage_one_when_nothing_matches(self):
        calls = []

        def fake_fetch(url, data=None):
            calls.append(url)
            return _html("supreme_search_missing.html")

        assert REGISTRY["supreme"].crawl_detail(fake_fetch, "supreme", "081-WO-99999") is None
        assert len(calls) == 1, "a 0-record search must not fetch a detail page"


class TestSupremeChargeVsClass:
    """Supreme publishes BOTH a coarse class and the actual charge.

    ``मुद्दाको किसिम`` is फौजदारी/देवानी — two values across ~104k rows. ``मुद्दा``
    is the charge (कर्तव्य ज्यान, भ्रष्टाचार, …). They shared one first-wins branch
    and किसिम appears first on the page, so the charge was dropped on every Supreme
    case. That left case_type='फौजदारी' corpus-wide, which courts.search_visibility
    can only map to OTHER_CRIMINAL — so a Supreme corruption case looked exactly
    like a homicide and could never reach the public index.
    """

    _PAGE = """
    <table class="table-hover"><tr><td>
      <table class="table-hover">
        <tr><td>दर्ता नँ . :</td><td>081-CR-1641</td></tr>
        <tr><td>मुद्दाको किसिम:</td><td>फौजदारी</td></tr>
        <tr><td>मुद्दा:</td><td>कर्तव्य ज्यान</td></tr>
      </table>
    </td></tr></table>"""

    def test_the_charge_wins_over_the_coarse_class(self):
        core = supreme.parse_supreme_detail(self._PAGE).core_fields
        assert core["case_type"] == "कर्तव्य ज्यान"
        assert core["case_subject"] == "कर्तव्य ज्यान"

    def test_the_coarse_class_is_kept_not_discarded(self):
        assert supreme.parse_supreme_detail(self._PAGE).extra_data["case_class"] == "फौजदारी"

    def test_a_page_without_a_charge_falls_back_to_the_class(self):
        page = self._PAGE.replace("<tr><td>मुद्दा:</td><td>कर्तव्य ज्यान</td></tr>", "")
        assert supreme.parse_supreme_detail(page).core_fields["case_type"] == "फौजदारी"

    def test_a_corruption_charge_survives_to_case_type(self):
        # The whole point: this is what the visibility gate needs to see.
        page = self._PAGE.replace("कर्तव्य ज्यान", "भ्रष्टाचार")
        assert supreme.parse_supreme_detail(page).core_fields["case_type"] == "भ्रष्टाचार"
