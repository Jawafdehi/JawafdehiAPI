"""Verifies the ported High-court crawl logic: portal HTML → structured rows /
enrichment, composed with the ported case_status normalization. High court is
two-stage bench-accumulation like Special. Pure (no network, no DB)."""

from courts.case_status import ACQUITTED
from courts.scraper.high import parse_bench_list, parse_bench_page, parse_high_detail

# Stage-1 bench-list table: two send_data benches + a जम्माः total row (skipped).
_BENCH_LIST = """
<html><body>
  <table class="table table-striped table-bordered table-hover">
    <tbody>
      <tr onclick="send_data('101', '१', '20820105')"><td>१</td><td>न्या. गोपाल राई</td></tr>
      <tr onclick="send_data('102', '२', '20820105')"><td>२</td><td>न्या. सीता देवी</td></tr>
      <tr><td>जम्माः</td><td>२</td></tr>
    </tbody>
  </table>
</body></html>
"""

# Stage-2 cause list for one bench: header <h4> + a data_row table. 9 cells per
# row: [क्र.सं., फाँट, दर्ता मिति, मुद्दा, मुद्दा नं., पक्ष, कानून व्यवसायी, कैफियत, स्थिति].
# Row 0 = both parties (||) + a parenthetical case number; row 1 = single party.
_BENCH_PAGE = """
<html><body>
  <h4>इजलास नं. १</h4>
  <table class="table table-bordered table-hover">
    <tbody>
      <tr class="data_row">
        <td>१</td><td>फौजदारी</td><td>२०८१/०५/१२</td><td>भ्रष्टाचार</td>
        <td><br>०८१-CR-०१२३ (कैद)</td>
        <td>वादी नेपाल सरकार || प्रतिवादी राम बहादुर समेत</td>
        <td>अधिवक्ता क</td><td>-</td><td>फैसला</td>
      </tr>
      <tr class="data_row">
        <td>२</td><td>देवानी</td><td>२०८२/०१/०५</td><td>अंश</td>
        <td>०८२-CR-०४५६</td>
        <td>वादी सीता देवी</td>
        <td>--</td><td></td><td>पेशी</td>
      </tr>
    </tbody>
  </table>
</body></html>
"""

# Detail page (enrichment): col-xs-6 label/value rows, panel-based parties, and a
# सुनवाइ hearings table. Status is the arrow form → verdict derivable directly.
_DETAIL_PAGE = """
<html><body>
  <div class="row"><div class="col-xs-6"><strong>दर्ता नँ.</strong></div>
    <div class="col-xs-6"><p>०८१-CR-०१२३</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>दर्ता मिति</strong></div>
    <div class="col-xs-6"><p>२०८१/०५/१२</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>मुद्दाको किसिम</strong></div>
    <div class="col-xs-6"><p>भ्रष्टाचार</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>मुद्दाको स्थिति</strong></div>
    <div class="col-xs-6"><p>फैसला / अन्तिम आदेश >> अभियोग दाबी नपुग्ने</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>फैसला मिति</strong></div>
    <div class="col-xs-6"><p>२०८२/०२/१०</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>फैसला गर्ने न्यायाधीश</strong></div>
    <div class="col-xs-6"><p>न्या. गोपाल राई</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>पेशी चढेको संख्या</strong></div>
    <div class="col-xs-6"><p>५</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>फाँट</strong></div>
    <div class="col-xs-6"><p>फौजदारी</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>फाँटवाला</strong></div>
    <div class="col-xs-6"><p>हरि प्रसाद</p></div></div>
  <div class="row"><div class="col-xs-6"><strong>अदालत</strong></div>
    <div class="col-xs-6"><p>उच्च अदालत पाटन</p></div></div>

  <div class="panel-heading">वादीको विवरण</div>
  <div class="panel-body">
    <div class="row"><div>नाम</div><div>ठेगाना</div></div>
    <div class="row"><div>नेपाल सरकार</div><div>काठमाडौं</div></div>
  </div>
  <div class="panel-heading">प्रतिवादीहरु</div>
  <div class="panel-body">
    <div class="row"><div>नाम</div><div>ठेगाना</div></div>
    <div class="row"><div>राम बहादुर</div><div>ललितपुर</div></div>
  </div>

  <table class="table">
    <tr><th>सुनवाइ मिती</th><th>न्यायाधीश</th><th>स्थिति</th><th>आदेश</th></tr>
    <tr><td>२०८२/०१/१०</td><td>न्या. गोपाल</td><td>पेशी</td><td>स्थगित</td></tr>
    <tr><td>२०८२/०२/१०</td><td>न्या. गोपाल</td><td>फैसला</td><td>सफाई</td></tr>
  </table>
</body></html>
"""

# Detail page whose case_status is the scraped table-HEADER artifact (DQ-01);
# the verdict is recoverable only from the final decisive hearing.
_DETAIL_ARTIFACT = """
<html><body>
  <div class="row"><div class="col-xs-6"><strong>मुद्दाको स्थिति</strong></div>
    <div class="col-xs-6"><p>आदेश /फैसलाको किसिम</p></div></div>
  <table class="table">
    <tr><th>सुनवाइ मिती</th><th>न्यायाधीश</th><th>स्थिति</th><th>आदेश</th></tr>
    <tr><td>२०८२/०२/१०</td><td>न्या. श्याम</td><td>फैसला</td><td>सफाई</td></tr>
  </table>
</body></html>
"""


def test_parse_bench_list_discovers_benches():
    benches = parse_bench_list(_BENCH_LIST)
    assert [b["bench_id"] for b in benches] == ["101", "102"]  # जम्माः row skipped
    assert benches[0]["bench_no"] == "१"
    assert benches[0]["judge_name"] == "न्या. गोपाल राई"


def test_parse_bench_page_maps_rows_and_normalizes():
    rows = parse_bench_page(
        _BENCH_PAGE,
        court_identifier="high_patan",
        date_bs="2082-01-05",
        bench_id="101",
        bench_no="१",
        judge_name="न्या. गोपाल राई",
    )
    assert len(rows) == 2

    (case0, hearing0) = rows[0]
    # case number: <br> collapsed, parenthetical suffix stripped, then Devanagari
    # transliterated + canonicalised.
    assert case0.case_number == "081-CR-0123"
    assert case0.court_identifier == "high_patan"
    assert case0.registration_date_bs == "2081-05-12"
    assert case0.registration_date_ad is not None  # bs_to_ad resolved
    assert case0.case_type == "भ्रष्टाचार"
    # parties split on the "||" separator
    assert case0.plaintiff == "वादी नेपाल सरकार"
    assert case0.defendant == "प्रतिवादी राम बहादुर समेत"
    # v2 shape: division (फाँट) lands in extra_data, NOT as a top-level column
    assert case0.extra_data["division"] == "फौजदारी"
    assert not hasattr(case0, "division")

    # bench_no transliterated to roman for the hearing.bench column
    assert hearing0.bench == "1"
    assert hearing0.bench_type == "इजलास नं. १"
    assert hearing0.judge_names == "न्या. गोपाल राई"
    assert hearing0.serial_no == "1"
    assert hearing0.lawyer_names == "अधिवक्ता क"
    assert hearing0.case_status == "फैसला"
    assert hearing0.extra_data == {"bench_id": "101", "bench_no": "१"}

    # row 1: single party (no "||") → plaintiff-only, defendant is None; "--"
    # lawyers normalise to None.
    (case1, hearing1) = rows[1]
    assert case1.case_number == "082-CR-0456"
    assert case1.plaintiff == "वादी सीता देवी"
    assert case1.defendant is None
    assert hearing1.lawyer_names is None
    assert case1.extra_data["division"] == "देवानी"


def test_empty_bench_page_returns_no_rows():
    assert (
        parse_bench_page(
            "<html><body>no table</body></html>",
            court_identifier="high_patan",
            date_bs="2082-01-05",
        )
        == []
    )


def test_parse_high_detail_core_extra_and_entities():
    enr = parse_high_detail(_DETAIL_PAGE)
    cf = enr.core_fields

    assert cf["registration_number"] == "०८१-CR-०१२३"  # kept Devanagari (value[:100])
    assert cf["registration_date_bs"] == "2081-05-12"
    assert cf["registration_date_ad"] is not None
    assert cf["case_type"] == "भ्रष्टाचार"
    # hearing_count coerced from Devanagari "५" to a real int
    assert cf["hearing_count"] == 5
    assert isinstance(cf["hearing_count"], int)
    assert cf["verdict_judge"] == "न्या. गोपाल राई"

    # case_status normalization: arrow-form outcome → verdict_type ACQUITTED
    assert cf["case_status"] == "फैसला / अन्तिम आदेश >> अभियोग दाबी नपुग्ने"
    assert cf["verdict_type"] == ACQUITTED
    # explicit फैसला मिति label supplies the verdict date
    assert cf["verdict_date_bs"] == "2082-02-10"
    assert cf["verdict_date_ad"] is not None

    # v2 shape: division stays in extra_data, NEVER in core_fields
    assert "division" not in cf
    assert enr.extra_data["division"] == "फौजदारी"
    assert enr.extra_data["division_officer"] == "हरि प्रसाद"
    assert enr.extra_data["court_name"] == "उच्च अदालत पाटन"
    assert enr.extra_data["case_type_display"] == "भ्रष्टाचार"

    # entities flattened to {side, name, address}
    assert {"side": "plaintiff", "name": "नेपाल सरकार", "address": "काठमाडौं"} in enr.entities
    assert {"side": "defendant", "name": "राम बहादुर", "address": "ललितपुर"} in enr.entities
    # panel header row (नाम/ठेगाना) not captured as a party
    assert all(e["name"] != "नाम" for e in enr.entities)

    # hearings parsed into extra_data for the downstream verdict resolver
    assert len(enr.extra_data["enrichment_hearings"]) == 2
    assert enr.extra_data["enrichment_hearings"][0]["hearing_date"] == "2082-01-10"


def test_parse_high_detail_drops_status_artifact_and_uses_hearings():
    enr = parse_high_detail(_DETAIL_ARTIFACT)
    # the scraped column header must NOT survive as a real status
    assert "case_status" not in enr.core_fields
    # verdict recovered from the final decisive hearing (फैसला / सफाई → acquittal)
    assert enr.core_fields["verdict_type"] == ACQUITTED


def test_parse_bench_page_caps_overlong_case_type():
    """An overlong/mis-parsed मुद्दा cell truncates to the column width (200) instead of
    dead-lettering the court on a varchar(200) overflow. Regression: the High cause-list
    left case_type uncapped while district/special/the enrich path all cap it."""
    long_type = "क" * 250  # over CourtCase.case_type's varchar(200)
    page = f"""
    <html><body>
      <h4>इजलास नं. १</h4>
      <table class="table table-bordered table-hover">
        <tbody>
          <tr class="data_row">
            <td>१</td><td>फौजदारी</td><td>२०८१/०५/१२</td><td>{long_type}</td>
            <td>०८१-CR-०१२३</td>
            <td>वादी क</td><td>-</td><td>-</td><td>पेशी</td>
          </tr>
        </tbody>
      </table>
    </body></html>
    """
    rows = parse_bench_page(
        page,
        court_identifier="high_patan",
        date_bs="2081-05-12",
        bench_id="101",
        bench_no="१",
        judge_name="न्या. गोपाल राई",
    )
    assert len(rows) == 1
    (case, _hearing) = rows[0]
    assert len(case.case_type) == 200
    assert case.case_number == "081-CR-0123"  # unaffected
