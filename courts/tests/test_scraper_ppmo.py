"""ppmo_blacklist port: parse (list / detail / BS-date band) + the command client.

The pure parse tests run without a network or DB. The command test injects a fake
source transport AND a fake ingestion client (via the ``build_*`` seams) and
asserts the command scrapes, resolves, and POSTs the right payloads — the ORM
upsert itself is exercised server-side in ``test_ingestion_api.py``.
"""

from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

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


def test_to_payload_omits_none_and_isoformats_dates():
    firm = P.ParsedFirm(firm_name="A", duration="2078-05-08")
    P.resolve_dates(firm)
    payload = P.to_payload(firm)
    assert payload["firm_name"] == "A"
    assert payload["blacklist_date_bs"] == "2078-05-08"
    assert isinstance(payload["blacklist_date_ad"], str)  # ISO string
    assert "address" not in payload  # None detail fields omitted
    assert "effective_until_bs" not in payload


# ── command (fake source + fake ingestion client) ─────────────────────────────


class _FakeSource:
    """Serves the list/detail fixtures by URL shape; records every GET."""

    def __init__(self, list_html, detail_html, detail_status=200):
        self._list = list_html
        self._detail = detail_html
        self._detail_status = detail_status
        self.gets = []

    def get(self, url):
        self.gets.append(url)
        if "id=" in url:
            return self._detail_status, (self._detail if self._detail_status == 200 else "")
        if "page=2" in url:  # terminal page ends the walk
            return 200, "<html><body>no rows</body></html>"
        return 200, self._list


class _FakeIngestion:
    """Captures POSTed batches; emulates an all-created server response."""

    def __init__(self):
        self.batches = []

    def post_firms(self, items):
        self.batches.append(items)
        return {"created": len(items), "updated": 0, "unchanged": 0, "failed": 0, "results": []}


class PpmoCommandClientTests(SimpleTestCase):
    def _run(self, source, ingestion, *extra):
        with patch(f"{CMD}.build_scrape_client", return_value=source), patch(
            f"{CMD}.build_ingestion_client", return_value=ingestion
        ), patch("time.sleep"):
            call_command(
                "scrape_ppmo_blacklist", "--delay", "0",
                "--api-token", "t", "--api-base", "http://api", *extra,
            )

    def test_write_posts_resolved_firms_and_skips_ad_date(self):
        ing = _FakeIngestion()
        self._run(_FakeSource(_LIST_HTML, _DETAIL_HTML), ing, "--write")

        posted = [item for batch in ing.batches for item in batch]
        names = {p["firm_name"] for p in posted}
        # Ghost Traders (AD-format date) is filtered by the BS band before POST.
        assert names == {"एबीसी निर्माण सेवा", "XYZ Builders Pvt Ltd"}

        abc = next(p for p in posted if p["firm_name"] == "एबीसी निर्माण सेवा")
        assert abc["blacklist_date_bs"] == "2078-05-08"
        assert abc["effective_until_bs"] == "2080-05-07"
        assert abc["address"] == "काठमाडौं, नेपाल"
        assert abc["proprietor_name"] == "राम बहादुर"
        assert "blacklist_date_ad" in abc  # ISO string

    def test_dry_run_posts_nothing(self):
        ing = _FakeIngestion()
        self._run(_FakeSource(_LIST_HTML, _DETAIL_HTML), ing)  # no --write
        assert ing.batches == []

    def test_detail_fetch_failure_keeps_firm_with_list_data(self):
        ing = _FakeIngestion()
        # detail pages 503 → the firm is kept (list-page data), not dropped.
        self._run(_FakeSource(_LIST_HTML, _DETAIL_HTML, detail_status=503), ing, "--write")
        posted = [item for batch in ing.batches for item in batch]
        abc = next(p for p in posted if p["firm_name"] == "एबीसी निर्माण सेवा")
        assert abc["blacklist_date_bs"] == "2078-05-08"  # still posted
        assert "address" not in abc  # detail failed → no detail fields

    def test_ingestion_batch_failure_is_not_fatal(self):
        class _Raising:
            def post_firms(self, items):
                raise RuntimeError("boom 503")

        # The command must finish (count the batch failed), not raise.
        self._run(_FakeSource(_LIST_HTML, _DETAIL_HTML), _Raising(), "--write")
