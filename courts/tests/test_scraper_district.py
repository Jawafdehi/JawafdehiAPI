"""Verifies the ported District-court crawl logic: portal HTML → structured rows
and detail-page HTML → enrichment, composed with the ported case_status
normalization. Pure (no network, no DB), mirroring test_scraper_special.py."""

from datetime import date

from courts.case_status import ACQUITTED
from courts.scraper.district import parse_daily_list, parse_district_detail
from jawafdehi_shared.dates import bs_to_ad

# One daily causelist page: a bench header table (bench + judge) followed by its
# record_display table — a header row, a DECIDED row (फैसला) and a PENDING row
# (पेशी). 10 cells per data row, matching the district portal's table layout.
# The case-number cell (cell 1) carries the number on line 1 and a parenthetical
# secondary/original id on line 2.
_DAILY_LIST = """
<html><body>
  <div>
    <table>
      <tr><td class="judge">न्यायाधीश राम</td><td align="right">इजलास नं. १</td></tr>
    </table>
    <table border="1" class="record_display">
      <tr><th>क्र.सं.</th><th>मुद्दा नं.</th><th>दर्ता मिति</th><th>मुद्दा</th><th>वादी</th>
          <th>प्रतिवादी</th><th>दफा</th><th>प्राथमिकता</th><th>कैफियत</th><th>किसिम</th></tr>
      <tr>
        <td>१</td>
        <td>०७९-CR-०१२३<br>(०७९-DC-००४५)</td>
        <td>२०७९/०५/१२</td>
        <td>भ्रष्टाचार</td>
        <td>वादी नेपाल सरकार</td>
        <td>प्रतिवादी राम बहादुर</td>
        <td>दफा ३</td>
        <td>प्राथमिकता २</td>
        <td>-</td>
        <td>फैसला</td>
      </tr>
      <tr>
        <td>२</td>
        <td>०७९-CR-०१२४</td>
        <td>२०७९/०६/०१</td>
        <td>चोरी</td>
        <td>वादी नेपाल सरकार</td>
        <td>प्रतिवादी श्याम</td>
        <td>दफा ५</td>
        <td>सामान्य</td>
        <td></td>
        <td>पेशी</td>
      </tr>
    </table>
  </div>
</body></html>
"""

# One detail page: the labelled <dl> block, plaintiff/defendant tables, a पेशी
# विवरण (hearings) table and a तारेख विवरण (timeline) table. The status uses the
# arrow form so the ported case_status parser can derive a verdict.
_DETAIL_PAGE = """
<html><body>
  <div class="content">
    <dl>
      <dt>रजिष्ट्रेशन नं:</dt><dd>DC-2079-00123</dd>
      <dt>मुद्दाको किसिम:</dt><dd>भ्रष्टाचार</dd>
      <dt>मुद्दाको बिषय:</dt><dd>घुस रिसवत लिएको</dd>
      <dt>मुद्दाको स्थिति:</dt><dd>फैसला >> अभियोग दाबी नपुग्ने</dd>
      <dt>फैसला मिति:</dt><dd>२०८१/०३/१५</dd>
      <dt>फैसला गर्ने मा. न्यायाधीश:</dt><dd>माननीय न्यायाधीश सीता देवी</dd>
      <dt>पेशी चढेको संख्या:</dt><dd>१२</dd>
    </dl>
  </div>
  <table>
    <tr><td><h4>वादी/प्रतिवादीको विवरण</h4></td></tr>
    <tr><td>
      <table class="record_display">
        <tr><th colspan="2">वादी</th></tr>
        <tr><th>नाम</th><th>ठेगाना</th></tr>
        <tr><td>नेपाल सरकार</td><td>काठमाडौं</td></tr>
      </table>
      <table class="record_display">
        <tr><th colspan="2">प्रतिवादी</th></tr>
        <tr><th>नाम</th><th>ठेगाना</th></tr>
        <tr><td>राम बहादुर</td><td>धनुषा</td></tr>
      </table>
    </td></tr>
  </table>
  <table>
    <tr><td><h4>पेशी विवरण</h4></td></tr>
    <tr><td>
      <table class="record_display">
        <tr><th>मिति</th><th>किसिम</th><th>इजलास</th><th>न्यायाधीश</th><th>आदेश</th></tr>
        <tr><td>२०८१/०३/१५</td><td>फैसला</td><td>इजलास १</td><td>सीता देवी</td><td>सफाई</td></tr>
      </table>
    </td></tr>
  </table>
  <table>
    <tr><td><h4>तारेख विवरण</h4></td></tr>
    <tr><td>
      <table class="record_display">
        <tr><th>मिति</th><th>किसिम</th></tr>
        <tr><td>२०८१/०२/१०</td><td>बहस</td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
"""


def test_parse_daily_list_maps_rows_and_normalizes():
    rows = parse_daily_list(
        _DAILY_LIST, court_identifier="dhanusha_dc", date_bs="2079-06-15"
    )
    assert len(rows) == 2

    (case0, hearing0) = rows[0]
    # case number transliterated + canonicalised from Devanagari
    assert case0.case_number == "079-CR-0123"
    assert case0.court_identifier == "dhanusha_dc"
    assert case0.registration_date_bs == "2079-05-12"
    assert case0.registration_date_ad == date(2022, 8, 28)  # bs_to_ad resolved
    assert case0.case_type == "भ्रष्टाचार"
    assert case0.plaintiff == "वादी नेपाल सरकार"
    assert case0.defendant == "प्रतिवादी राम बहादुर"

    # v2 shape rule: the district legacy fields live in extra_data, NOT as columns.
    assert not hasattr(case0, "section")
    assert not hasattr(case0, "priority")
    assert not hasattr(case0, "case_id")
    # section / priority keep Devanagari digits (ngm never transliterated them —
    # only whitespace-normalised). case_id IS transliterated (but not canonicalised).
    assert case0.extra_data["section"] == "दफा ३"
    assert case0.extra_data["priority"] == "प्राथमिकता २"
    assert case0.extra_data["case_id"] == "079-DC-0045"
    # a second parenthetical line also lands as secondary_case_number (transliterated)
    assert case0.extra_data["secondary_case_number"] == "079-DC-0045"

    # hearing carries the per-appearance decision + bench/judge (bench-constant)
    assert hearing0.hearing_date_bs == "2079-06-15"
    assert hearing0.hearing_date_ad == bs_to_ad("2079-06-15")
    assert hearing0.serial_no == "1"  # transliterated
    assert hearing0.bench == "इजलास नं. १"
    assert hearing0.judge_names == "न्यायाधीश राम"
    assert hearing0.decision_type == "फैसला"
    assert hearing0.remarks == "-"
    # the district causelist has no per-hearing case_status column
    assert hearing0.case_status is None


def test_parse_daily_list_single_line_case_and_inherited_bench():
    rows = parse_daily_list(
        _DAILY_LIST, court_identifier="dhanusha_dc", date_bs="2079-06-15"
    )
    (case1, hearing1) = rows[1]
    assert case1.case_number == "079-CR-0124"
    # single-line case cell → no case_id, no secondary number
    assert case1.extra_data["case_id"] is None
    assert "secondary_case_number" not in case1.extra_data
    # empty कैफियत cell → None
    assert hearing1.remarks is None
    assert hearing1.decision_type == "पेशी"
    # second row inherits the bench/judge from the same bench section
    assert hearing1.bench == "इजलास नं. १"
    assert hearing1.judge_names == "न्यायाधीश राम"


def test_parse_district_detail_enrichment():
    enr = parse_district_detail(_DETAIL_PAGE)

    assert enr.core_fields["registration_number"] == "DC-2079-00123"
    assert enr.core_fields["case_type"] == "भ्रष्टाचार"
    assert enr.core_fields["case_subject"] == "घुस रिसवत लिएको"
    assert enr.core_fields["verdict_judge"] == "माननीय न्यायाधीश सीता देवी"
    # hearing_count coerced to int via coerce_count
    assert enr.core_fields["hearing_count"] == 12
    assert isinstance(enr.core_fields["hearing_count"], int)

    # real status is kept, and drives the typed verdict fields
    assert enr.core_fields["case_status"] == "फैसला >> अभियोग दाबी नपुग्ने"
    assert enr.core_fields["verdict_type"] == ACQUITTED
    # explicit फैसला मिति field → verdict date (BS canonicalised, AD resolved)
    assert enr.core_fields["verdict_date_bs"] == "2081-03-15"
    assert enr.core_fields["verdict_date_ad"] == date(2024, 6, 29)

    # entities are flat {side, name, address} dicts
    assert enr.entities == [
        {"side": "plaintiff", "name": "नेपाल सरकार", "address": "काठमाडौं"},
        {"side": "defendant", "name": "राम बहादुर", "address": "धनुषा"},
    ]

    # hearings/timeline go to extra_data verbatim — Devanagari digits preserved
    # (get_text only; no normalisation on the sub-tables).
    assert enr.extra_data["enrichment_hearings"] == [
        {
            "date": "२०८१/०३/१५",
            "type": "फैसला",
            "division": "इजलास १",
            "judge": "सीता देवी",
            "order": "सफाई",
        }
    ]
    assert enr.extra_data["enrichment_timeline"] == [
        {"date": "२०८१/०२/१०", "type": "बहस"}
    ]


def test_detail_drops_status_header_artifact():
    # A scraped column-header leaked into मुद्दाको स्थिति must NOT be stored as a
    # status, and yields no verdict.
    html = """
    <html><body><div class="content"><dl>
      <dt>रजिष्ट्रेशन नं:</dt><dd>DC-2080-0007</dd>
      <dt>मुद्दाको स्थिति:</dt><dd>आदेश /फैसलाको किसिम</dd>
    </dl></div></body></html>
    """
    enr = parse_district_detail(html)
    assert enr.core_fields["registration_number"] == "DC-2080-0007"
    assert "case_status" not in enr.core_fields
    assert "verdict_type" not in enr.core_fields


def test_detail_status_without_verdict_keeps_status_only():
    # A bare decided marker is a real status (kept) but carries no outcome; the
    # district hearing sub-rows use date/type/division/judge/order keys, which
    # verdict_from_hearings does not recognise (it wants case_status/status), so
    # no verdict_type is derived — a faithful nuance of the district port.
    html = """
    <html><body>
      <div class="content"><dl>
        <dt>मुद्दाको स्थिति:</dt><dd>फैसला</dd>
      </dl></div>
      <table>
        <tr><td><h4>पेशी विवरण</h4></td></tr>
        <tr><td>
          <table class="record_display">
            <tr><th>मिति</th><th>किसिम</th><th>इजलास</th><th>न्यायाधीश</th><th>आदेश</th></tr>
            <tr><td>२०८१/०१/०१</td><td>फैसला</td><td>इजलास १</td><td>सीता</td><td>सफाई</td></tr>
          </table>
        </td></tr>
      </table>
    </body></html>
    """
    enr = parse_district_detail(html)
    assert enr.core_fields["case_status"] == "फैसला"
    assert "verdict_type" not in enr.core_fields


def test_empty_page_returns_no_rows():
    assert (
        parse_daily_list(
            "<html><body>Causelist is not available</body></html>",
            court_identifier="dhanusha_dc",
            date_bs="2079-06-15",
        )
        == []
    )


# A district number with a NUMERIC middle segment (082-02-0001): best_effort_normalize
# cannot canonicalise it (_CASE_RE wants a LETTER-led middle like CR), so it returns the
# value UNCHANGED — the parser itself must transliterate the Devanagari digits, or the
# raw Devanagari reaches court-case IRI minting and dead-letters the whole court's scrape.
_DAILY_LIST_NUMERIC_MIDDLE = """
<html><body>
  <div>
    <table>
      <tr><td class="judge">न्यायाधीश राम</td><td align="right">इजलास नं. १</td></tr>
    </table>
    <table border="1" class="record_display">
      <tr><th>क्र.सं.</th><th>मुद्दा नं.</th><th>दर्ता मिति</th><th>मुद्दा</th><th>वादी</th>
          <th>प्रतिवादी</th><th>दफा</th><th>प्राथमिकता</th><th>कैफियत</th><th>किसिम</th></tr>
      <tr>
        <td>१</td>
        <td>०८२-०२-०००१</td>
        <td>२०८२/०२/०१</td>
        <td>अंश</td>
        <td>वादी क</td>
        <td>प्रतिवादी ख</td>
        <td>-</td><td>-</td><td></td>
        <td>पेशी</td>
      </tr>
    </table>
  </div>
</body></html>
"""


def test_parse_daily_list_numeric_middle_case_number_is_ascii():
    """A numeric-middle district number (082-02-0001) transliterates to ASCII and mints
    a valid court-case IRI. Regression: the raw Devanagari digits fell through
    best_effort_normalize unchanged and dead-lettered every district court at IRI minting."""
    from jawafdehi_shared.entities.ids import build_courtcase_iri

    rows = parse_daily_list(
        _DAILY_LIST_NUMERIC_MIDDLE, court_identifier="achham_dc", date_bs="2082-02-15"
    )
    assert len(rows) == 1
    (case, _hearing) = rows[0]
    assert case.case_number == "082-02-0001"  # ASCII, not the raw ०८२-०२-०००१
    # the IRI minter (which rejected the Devanagari form) now accepts it
    assert build_courtcase_iri("achham_dc", case.case_number).endswith(
        "/courtcase/achham_dc/082-02-0001"
    )
