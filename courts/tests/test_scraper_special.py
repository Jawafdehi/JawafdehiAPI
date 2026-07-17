"""Verifies the ported Special-court crawl logic: portal HTML → structured rows
→ composed with the ported case_status normalization. Pure (no network, no DB)."""

from courts.case_status import ACQUITTED, verdict_from_hearings
from courts.scraper.special import parse_bench_options, parse_bench_page, parse_detail

# One bench page: header row + a DECIDED row (फैसला / सफाई = acquittal) and a
# PENDING row (पेशी). 11 cells per row, matching the portal's table layout.
_BENCH_PAGE = """
<html><body>
  <font>इजलास नं. १</font>
  <table><tr><td><font size="2">अध्यक्ष माननीय न्यायाधीश राम<br>सदस्य माननीय न्यायाधीश श्याम</font></td></tr></table>
  <table width="100%" border="1">
    <tr><th>क्र.सं.</th><th>फाँट</th><th>दर्ता मिति</th><th>मुद्दा</th><th>मुद्दा नं.</th>
        <th>वादी</th><th>प्रतिवादी</th><th>मूल मुद्दा नं.</th><th>कैफियत</th><th>स्थिति</th><th>किसिम</th></tr>
    <tr><td>१</td><td>फाँट क</td><td>२०८१/०५/१२</td><td>भ्रष्टाचार</td><td>०८२-CR-००१५</td>
        <td>वादी नेपाल सरकार</td><td>प्रतिवादी क समेत ३</td><td>०८०-CR-०००१( पुनरावेदन)</td>
        <td>-</td><td>फैसला</td><td>सफाई</td></tr>
    <tr><td>२</td><td>फाँट ख</td><td>२०८२/०१/०५</td><td>भ्रष्टाचार</td><td>०८२-CR-००१६</td>
        <td>वादी नेपाल सरकार</td><td>प्रतिवादी ख</td><td></td>
        <td></td><td>पेशी</td><td>स्थगित</td></tr>
  </table>
</body></html>
"""

_BENCH_SELECT = """
<html><body><select name="bench_type">
  <option value="">-- select --</option>
  <option value="1">इजलास १</option>
  <option value="2">इजलास २</option>
</select></body></html>
"""


def test_parse_bench_options_discovers_benches():
    benches = parse_bench_options(_BENCH_SELECT)
    assert [b["value"] for b in benches] == ["1", "2"]  # blank option skipped


def test_parse_bench_page_maps_rows_and_normalizes():
    rows = parse_bench_page(_BENCH_PAGE, date_bs="2082-01-05", bench_type="1")
    assert len(rows) == 2

    (case0, hearing0) = rows[0]
    # case number transliterated + canonicalised from Devanagari
    assert case0.case_number == "082-CR-0015"
    assert case0.registration_date_bs == "2081-05-12"
    assert case0.registration_date_ad is not None  # bs_to_ad resolved
    assert case0.case_type == "भ्रष्टाचार"
    assert case0.plaintiff == "वादी नेपाल सरकार"
    assert case0.defendant == "प्रतिवादी क समेत ३"
    # low-value legacy fields land in extra_data, NOT as v2 columns
    assert case0.extra_data["category"] == "फाँट क"
    # original_case_number keeps Devanagari digits (faithful to ngm: only
    # parenthesis-spacing is fixed, no transliteration) — just the spacing normalised.
    assert case0.extra_data["original_case_number"] == "०८०-CR-०००१ (पुनरावेदन)"
    assert not hasattr(case0, "category")

    # hearing carries the per-appearance status + judges (bench-constant)
    assert hearing0.case_status == "फैसला"
    assert hearing0.decision_type == "सफाई"
    assert hearing0.judge_names and "राम" in hearing0.judge_names
    assert hearing0.extra_data["court_number"] == "इजलास नं. १"


def test_ported_normalization_composes_with_parse():
    # The whole point of the port: parsed hearings feed the case_status
    # normalization to derive a verdict the cause-list never states directly.
    rows = parse_bench_page(_BENCH_PAGE, date_bs="2082-01-05", bench_type="1")
    decided_hearing = rows[0][1]
    pending_hearing = rows[1][1]

    assert (
        verdict_from_hearings(
            [{"case_status": decided_hearing.case_status,
              "decision_type": decided_hearing.decision_type}]
        )
        == ACQUITTED
    )
    assert (
        verdict_from_hearings(
            [{"case_status": pending_hearing.case_status,
              "decision_type": pending_hearing.decision_type}]
        )
        is None
    )


def test_empty_table_returns_no_rows():
    assert parse_bench_page("<html><body>no table</body></html>", date_bs="2082-01-05") == []


_DETAIL = """
<html><body>
<table width="100%" border="0" cellspacing="0" cellpadding="1">
  <tr><td class="caption">दर्ता नँ .</td><td>१२३</td>
      <td class="caption">मुद्दा</td><td>भ्रष्टाचार</td></tr>
  <tr><td class="caption">दर्ता मिती</td><td>२०८१/०५/१२</td>
      <td class="caption">फाँट</td><td>रिट १</td></tr>
  <tr><td class="caption">मुद्दाको स्थिती</td><td>आदेश /फैसलाको किसिम</td>
      <td class="caption">मुद्दाको किसिम</td><td>देवानी</td></tr>
  <tr><td class="caption">वादीहरु</td><td>नेपाल सरकार</td>
      <td class="caption">प्रतिवादीहरु</td><td>क, ख</td></tr>
</table>
<table>
  <tr><td>पेशी को विवरण</td></tr>
  <tr><td><table class="utivtbl">
    <tr><th>मिति</th><th>न्यायाधीश</th><th>स्थिति</th><th>किसिम</th></tr>
    <tr><td>२०८२/०९/२८</td><td>राम</td><td>फैसला</td><td>सफाई</td></tr>
  </table></td></tr>
</table>
</body></html>
"""


def test_parse_detail_maps_core_extra_entities_hearings():
    enr = parse_detail(_DETAIL)
    assert enr.core_fields["registration_number"] == "१२३"
    assert enr.core_fields["case_type"] == "भ्रष्टाचार"
    assert enr.core_fields["registration_date_bs"] == "2081-05-12"
    assert enr.core_fields["registration_date_ad"] is not None
    # raw status stored by the parser (header dropped later at write time), not a column
    assert enr.core_fields["case_status"] == "आदेश /फैसलाको किसिम"
    # legacy fields → extra_data, never columns
    assert enr.extra_data["division"] == "रिट १"
    assert enr.extra_data["category"] == "देवानी"
    # parties split on comma; sides tagged
    sides = sorted((e["side"], e["name"]) for e in enr.entities)
    assert sides == [("defendant", "क"), ("defendant", "ख"), ("plaintiff", "नेपाल सरकार")]
    # hearing section → enrichment_hearings, which the write path turns into a verdict
    assert enr.extra_data["enrichment_hearings"][0]["decision_type"] == "सफाई"
    assert verdict_from_hearings(enr.extra_data["enrichment_hearings"]) == ACQUITTED
    assert enr.core_fields["hearing_count"] == 1
