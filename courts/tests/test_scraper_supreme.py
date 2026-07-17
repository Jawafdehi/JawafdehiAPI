"""Verifies the ported Supreme-court crawl logic: portal HTML → structured rows
and detail-page HTML → enrichment, composed with the ported case_status
normalization. Pure (no network, no DB), mirroring ``test_scraper_special``."""

from courts.case_status import ACQUITTED
from courts.scraper.supreme import parse_cause_list, parse_supreme_detail

# One weekly-supplementary cause list. 10-column table (matched via the exact
# table attributes → 10-cell header), a DECIDED-shaped row with the ``||`` party
# split + br-separated judges, and a fallback row whose party cell lacks ``||``.
_CAUSE_LIST = """
<html><body>
  <table width="100%" border="0" cellspacing="0" bordercolor="#ffffff">
    <tr bgcolor="#FFCC00">
      <th>क्र.सं.</th><th>फाँट</th><th>दर्ता मिति</th><th>इजलास</th><th>मुद्दा</th>
      <th>मुद्दा नं.</th><th>पक्ष</th><th>सुन्न नमिल्ने</th><th>सुन्न मिल्ने</th><th>कैफियत</th>
    </tr>
    <tr bgcolor="#ffffff">
      <td>१</td><td>- फौजदारी _</td><td>२०८२/०५/१२</td><td>संयुक्त इजलास</td><td>भ्रष्टाचार</td>
      <td>०८२-CR-०१२३ (पुनरावेदन)</td><td>वादी नेपाल सरकार || प्रतिवादी राम बहादुर समेत ३</td>
      <td>मा. न्या. क</td><td>मा. न्या. राम<br>मा. न्या. श्याम</td><td>-</td>
    </tr>
    <tr bgcolor="#ffffff">
      <td>२</td><td>देवानी</td><td>२०८२/०४/०१</td><td>इजलास</td><td>रिट</td>
      <td>०८२-WO-०००५</td><td>निवेदक क विरुद्ध विपक्षी ख</td>
      <td>-</td><td>मा. न्या. गीता</td><td>-</td>
    </tr>
  </table>
</body></html>
"""

# One detail page. The basic-info + parties table is ``class="table-hover"`` so
# parse_basic_info_table / parse_parties match it; the hearing + timeline tables
# are plain <table>s matched by their header text. The case_status cell holds the
# mis-scraped column header (आदेश /फैसलाको किसिम) — it must NOT be stored as a
# status; the verdict is recovered from the terminal फैसला/सफाई hearing instead.
_DETAIL_PAGE = """
<html><body>
  <table class="table-hover">
    <tr><th>विवरण</th><th> </th><th> </th><th> </th></tr>
    <tr><td>दर्ता नँ</td><td>0123</td><td>दर्ता मिती</td><td>२०८२/०५/१२</td></tr>
    <tr><td>मुद्दाको किसिम</td><td>भ्रष्टाचार</td><td>मुद्दाको स्थिती</td><td>आदेश /फैसलाको किसिम</td></tr>
    <tr><td>फैसला गर्ने मा. न्यायाधीश</td><td>मा. न्या. राम</td><td>फाँट</td><td>फौजदारी</td></tr>
    <tr><td>फैसला मिती</td><td>२०८२/०५/२०</td><td>पेशी चढेको संख्या</td><td>५</td></tr>
    <tr><td>वादीहरु</td><td>नेपाल सरकार</td><td>प्रतिवादीहरु</td><td>राम बहादुर, श्याम बहादुर</td></tr>
  </table>
  <table>
    <tr><th>सुनवाइ मिती</th><th>न्यायाधीश</th><th>मुद्दाको स्थिती</th><th>आदेश /फैसलाको किसिम</th></tr>
    <tr><td>२०८२/०५/१८</td><td>मा. न्या. श्याम</td><td>पेशी</td><td>स्थगित</td></tr>
    <tr><td>२०८२/०५/२०</td><td>मा. न्या. राम</td><td>फैसला</td><td>सफाई</td></tr>
  </table>
  <table>
    <tr><th>तारेख मिती</th><th>विवरण</th><th>तारेखको किसिम</th></tr>
    <tr><td>२०८२/०५/१०</td><td>पेशी</td><td>पेशी तारेख</td></tr>
  </table>
</body></html>
"""


def test_cause_list_maps_rows_and_normalizes():
    rows = parse_cause_list(_CAUSE_LIST, date_bs="2082-05-15")
    assert len(rows) == 2

    case0, hearing0 = rows[0]
    # case number: parenthetical stripped, then transliterated + zero-padded.
    assert case0.case_number == "082-CR-0123"
    assert case0.registration_date_bs == "2082-05-12"
    assert case0.registration_date_ad is not None  # bs_to_ad resolved
    assert case0.case_type == "भ्रष्टाचार"
    # parties split on the ``||`` delimiter.
    assert case0.plaintiff == "वादी नेपाल सरकार"
    assert case0.defendant == "प्रतिवादी राम बहादुर समेत ३"
    # division is NOT a v2 court_cases column — it lands in extra_data, decorated
    # ``- <div> _`` stripped. Kept Devanagari (only the decoration is trimmed).
    assert case0.extra_data["division"] == "फौजदारी"
    assert not hasattr(case0, "division")

    # hearing carries the per-appearance fields.
    assert hearing0.hearing_date_bs == "2082-05-15"
    assert hearing0.hearing_date_ad is not None
    assert hearing0.bench_type == "संयुक्त इजलास"
    assert hearing0.serial_no == "1"  # Devanagari serial transliterated
    # judge_names = the "must hear" cell, br-joined with newlines.
    assert hearing0.judge_names == "मा. न्या. राम\nमा. न्या. श्याम"
    assert hearing0.extra_data["judges_cannot_hear"] == "मा. न्या. क"
    assert hearing0.extra_data["judges_must_hear"] == "मा. न्या. राम\nमा. न्या. श्याम"


def test_cause_list_party_fallback_without_delimiter():
    rows = parse_cause_list(_CAUSE_LIST, date_bs="2082-05-15")
    case1, _ = rows[1]
    assert case1.case_number == "082-WO-0005"
    # No ``||`` in the party cell → whole cell kept as plaintiff, defendant NULL.
    assert case1.plaintiff == "निवेदक क विरुद्ध विपक्षी ख"
    assert case1.defendant is None


def test_empty_page_returns_no_rows():
    assert parse_cause_list("<html><body>no table</body></html>", date_bs="2082-05-15") == []


def test_enrichment_core_fields_and_extra_data():
    enrich = parse_supreme_detail(_DETAIL_PAGE)
    cf = enrich.core_fields

    assert cf["registration_number"] == "0123"
    assert cf["registration_date_bs"] == "2082-05-12"
    assert cf["registration_date_ad"] is not None
    assert cf["case_type"] == "भ्रष्टाचार"
    assert cf["case_subject"] == "भ्रष्टाचार"
    assert cf["verdict_judge"] == "मा. न्या. राम"
    # hearing_count coerced to an int for the IntegerField column.
    assert cf["hearing_count"] == 5
    assert isinstance(cf["hearing_count"], int)
    # verdict date recovered from the "फैसला मिती" label.
    assert cf["verdict_date_bs"] == "2082-05-20"
    assert cf["verdict_date_ad"] is not None

    # division is NOT a column → extra_data; hearings/timeline carried through.
    assert enrich.extra_data["division"] == "फौजदारी"
    assert len(enrich.extra_data["enrichment_hearings"]) == 2
    assert len(enrich.extra_data["enrichment_timeline"]) == 1
    assert enrich.extra_data["enrichment_hearings"][-1]["status"] == "फैसला"
    assert enrich.extra_data["enrichment_hearings"][-1]["order_type"] == "सफाई"


def test_enrichment_header_status_does_not_leak_and_verdict_from_hearings():
    enrich = parse_supreme_detail(_DETAIL_PAGE)
    cf = enrich.core_fields

    # The paren-date/header case_status must NOT be stored as a status (DQ-01):
    # the "आदेश /फैसलाको किसिम" column header is a scraped artifact, not a status.
    assert "case_status" not in cf

    # The status yields no outcome, so the verdict is derived from the terminal
    # फैसला/सफाई hearing via verdict_from_hearings → ACQUITTED.
    assert cf["verdict_type"] == ACQUITTED


def test_enrichment_parties_side_tagged():
    enrich = parse_supreme_detail(_DETAIL_PAGE)
    plaintiffs = [e["name"] for e in enrich.entities if e["side"] == "plaintiff"]
    defendants = [e["name"] for e in enrich.entities if e["side"] == "defendant"]

    assert plaintiffs == ["नेपाल सरकार"]
    # comma-separated defendant cell split into individual parties.
    assert defendants == ["राम बहादुर", "श्याम बहादुर"]
    assert all(e["address"] is None for e in enrich.entities)
