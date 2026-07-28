"""ppmo_blacklist port: parse (list / detail / BS-date band) + the command upsert.

The pure parse tests run without a network or DB. The command test injects a fake
HTTP transport via ``build_http_client`` (mirrors ``test_scraper_orders.py``) and
asserts the ``BlacklistedFirm`` upsert, detail back-fill, and date-band skip.
"""

from unittest.mock import patch

from django.test import TestCase

from courts.models import BlacklistedFirm
from courts.scraper import ppmo as P

CMD = "courts.management.commands.scrape_ppmo_blacklist"

# A list page: header row (skipped), a firm with a detail link + a date range, a
# firm with a single date and no detail link, a pagination arrow (skipped), and a
# firm-shaped row carrying an AD-format date (parsed here, skipped at write time).
_LIST_HTML = """
<table class="list4">
  <tr><th>Company Name</th><th>Duration</th></tr>
  <tr><td><a href="index.php?route=information/black_lists&id=7">एबीसी निर्माण सेवा</a></td>
      <td>2078-05-08 to 2080-05-07</td></tr>
  <tr><td>XYZ Builders Pvt Ltd</td><td>2079-01-15</td></tr>
  <tr><td>»</td><td>&nbsp;</td></tr>
  <tr><td>Ghost Traders</td><td>2017-09-04</td></tr>
</table>
<div class="pagination">
  <a href="?route=information/black_lists&page=1">1</a>
  <a href="?route=information/black_lists&page=2">&gt;</a>
</div>
"""

_DETAIL_HTML = """
<table class="list3">
  <tr><td>Address</td><td>काठमाडौं, नेपाल</td></tr>
  <tr><td>Cause</td><td>ठेक्का सम्झौता उल्लंघन (मुख्य व्यक्ति: श्री राम बहादुर)
      ( कालो सूचीमा राख्न लेखि पठाउने सार्बजनिक निकायको नाम :श्री सडक डिभिजन, इलाम)</td></tr>
</table>
"""


# ── pure parse ───────────────────────────────────────────────────────────────


def test_parse_list_extracts_firms_and_next_page():
    firms, next_href = P.parse_list(_LIST_HTML)
    names = [f.firm_name for f in firms]
    # header ("Company Name") and the "»" arrow are filtered out
    assert names == ["एबीसी निर्माण सेवा", "XYZ Builders Pvt Ltd", "Ghost Traders"]
    assert firms[0].detail_href == "index.php?route=information/black_lists&id=7"
    assert firms[1].detail_href is None
    assert "page=2" in next_href


def test_parse_list_no_pagination():
    _, next_href = P.parse_list('<table class="list4"><tr><td>ABC co</td><td>2079-01-01</td></tr></table>')
    assert next_href is None


def test_parse_detail_extracts_fields():
    detail = P.parse_detail(_DETAIL_HTML)
    assert detail["address"] == "काठमाडौं, नेपाल"
    assert "ठेक्का" in detail["reason"]
    assert detail["proprietor_name"] == "राम बहादुर"
    assert detail["recommending_office"] == "सडक डिभिजन, इलाम"


def test_parse_detail_non_detail_page_returns_none():
    assert P.parse_detail("<div>pagination, not a detail page</div>") is None


def test_resolve_dates_range_and_single():
    ranged = P.ParsedFirm(firm_name="A", duration="2078-05-08 to 2080-05-07")
    assert P.resolve_dates(ranged) is True
    assert ranged.blacklist_date_bs == "2078-05-08"
    assert ranged.effective_until_bs == "2080-05-07"

    single = P.ParsedFirm(firm_name="B", duration="2079-01-15")
    assert P.resolve_dates(single) is True
    assert single.blacklist_date_bs == "2079-01-15"
    assert single.effective_until_bs is None


def test_resolve_dates_rejects_ad_format_out_of_band():
    firm = P.ParsedFirm(firm_name="Ghost", duration="2017-09-04")
    assert P.resolve_dates(firm) is False
    assert firm.blacklist_date_bs is None


# ── command (fake transport, real ORM) ────────────────────────────────────────


class _FakeClient:
    """Serves the list/detail fixtures by URL shape; records every GET."""

    def __init__(self, list_html, detail_html):
        self._list = list_html
        self._detail = detail_html
        self.gets = []

    def get(self, url):
        self.gets.append(url)
        if "id=" in url:
            return 200, self._detail
        if "page=2" in url:  # terminal page ends the walk
            return 200, "<html><body>no rows</body></html>"
        return 200, self._list


class PpmoCommandTests(TestCase):
    databases = "__all__"

    def _run(self, client, *extra):
        from django.core.management import call_command

        with patch(f"{CMD}.build_http_client", return_value=client), patch("time.sleep"):
            call_command("scrape_ppmo_blacklist", "--write", "--delay", "0", *extra)

    def test_upserts_firms_and_skips_ad_date_row(self):
        self._run(_FakeClient(_LIST_HTML, _DETAIL_HTML))

        # Ghost Traders (AD-format date) is filtered by the BS band → 2 rows.
        assert BlacklistedFirm.objects.using("ngm").count() == 2

        abc = BlacklistedFirm.objects.using("ngm").get(firm_name="एबीसी निर्माण सेवा")
        assert abc.blacklist_date_bs == "2078-05-08"
        assert abc.effective_until_bs == "2080-05-07"
        assert abc.address == "काठमाडौं, नेपाल"
        assert abc.proprietor_name == "राम बहादुर"
        assert abc.recommending_office == "सडक डिभिजन, इलाम"
        assert abc.blacklist_date_ad is not None  # BS→AD converted

        xyz = BlacklistedFirm.objects.using("ngm").get(firm_name="XYZ Builders Pvt Ltd")
        assert xyz.effective_until_bs is None
        assert xyz.address is None  # no detail link → no detail fetched

    def test_rerun_is_idempotent(self):
        client = _FakeClient(_LIST_HTML, _DETAIL_HTML)
        self._run(client)
        self._run(client)
        assert BlacklistedFirm.objects.using("ngm").count() == 2

    def test_backfills_missing_detail_on_existing_row(self):
        BlacklistedFirm.objects.using("ngm").create(
            firm_name="एबीसी निर्माण सेवा", blacklist_date_bs="2078-05-08"
        )
        self._run(_FakeClient(_LIST_HTML, _DETAIL_HTML))
        abc = BlacklistedFirm.objects.using("ngm").get(firm_name="एबीसी निर्माण सेवा")
        assert abc.address == "काठमाडौं, नेपाल"  # back-filled, not duplicated
        assert BlacklistedFirm.objects.using("ngm").filter(
            firm_name="एबीसी निर्माण सेवा"
        ).count() == 1
