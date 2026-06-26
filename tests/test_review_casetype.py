"""Regression tests for CIAA vs non-CIAA case-type detection.

The case-type gate must key on the issuer-specific CIAA_PRESS_RELEASE source
type, NOT on the charge sheet (AG_ABHIYOG_PATRA) or the Special-Court venue: the
Attorney General files abhiyog patra for non-CIAA prosecutions (e.g. money
laundering) that are also tried at the Special Court, so neither signal
discriminates CIAA from non-CIAA.

Audit finding behind these tests: 9/51 priority cases — all genuinely CIAA, each
carrying a correctly-typed CIAA_PRESS_RELEASE source — were mis-detected as
NON_CIAA because the old heuristic only matched press-release *title keywords*
(plus OFFICIAL_GOVERNMENT) and never read the CIAA_PRESS_RELEASE source_type.

These are pure-function tests (case dict in, no DB).
"""

from review import casetype
from review.rules_engine import ciaa_press_release

# A real CIAA press-release headline (case 080-CR-0051). Note it contains NO
# "अख्तियार" / "press release" / "प्रेस विज्ञप्ति" substring — it reads
# "… आरोप-पत्र दायर।" — so the title-keyword heuristic alone cannot see it. The
# CIAA_PRESS_RELEASE source_type is what makes it detectable.
PR_0051_TITLE = (
    "जल उत्पन्न प्रकोप नियन्त्रण कार्यालय, लमही, दाङका इन्जिनियर रामबहादुर रानाभाट "
    "उपर बिगो रु.३,१५,८१,०६६।८० कायम गरी गैरकानूनी सम्पत्ति आर्जन गरी भ्रष्टाचार "
    "गरेको सम्बन्धी आरोप-पत्र दायर।"
)


def _src(title, source_type):
    return {"source": {"title": title, "source_type": source_type}}


def _case(*sources, court_cases=None):
    return {"evidence": list(sources), "court_cases": court_cases or []}


def test_press_release_title_lacks_keyword_signals():
    # Guards the premise of the fix: this title is invisible to the old
    # title-keyword gate, so detection MUST come from the source_type.
    t = PR_0051_TITLE.lower()
    assert "अख्तियार" not in t
    assert "press release" not in t
    assert "प्रेस विज्ञप्त" not in t


def test_ciaa_press_release_source_alone_is_ciaa_basic():
    # Shape of 080-CR-0066: CIAA press release + a news item, no verdict doc.
    case = _case(
        _src(PR_0051_TITLE, "CIAA_PRESS_RELEASE"),
        _src("भ्रष्टाचारका दोषी शिक्षकलाई निवृत्तिभरण « Kathmandu Pati", "NEWS"),
        court_cases=["special:080-CR-0066"],
    )
    assert casetype.detect(case)["type"] == "CIAA_BASIC"


def test_press_release_plus_court_order_is_has_verdict():
    # Shape of 080-CR-0051 / 080-CR-0025: press release + special-court order.
    case = _case(
        _src(PR_0051_TITLE, "CIAA_PRESS_RELEASE"),
        _src("Court Order - 080-CR-0051", "COURT_ORDER"),
        court_cases=["special:080-CR-0051"],
    )
    assert casetype.detect(case)["type"] == "CIAA_HAS_VERDICT"


def test_court_order_source_type_alone_is_has_verdict():
    # COURT_ORDER-typed source whose title has NO आदेश/फैसला keyword — HAS_VERDICT
    # must be inferred from the source type, mirroring the court_record detector.
    case = _case(
        _src(PR_0051_TITLE, "CIAA_PRESS_RELEASE"),
        _src("निर्णय दस्तावेज", "COURT_ORDER"),
        court_cases=["special:080-CR-0051"],
    )
    assert casetype.detect(case)["type"] == "CIAA_HAS_VERDICT"


def test_press_release_plus_charge_sheet_is_extended():
    # Press release establishes CIAA; the AG_ABHIYOG_PATRA tiers it up to
    # EXTENDED (no verdict text attached).
    case = _case(
        _src(PR_0051_TITLE, "CIAA_PRESS_RELEASE"),
        _src("अभियोग पत्र — 080-CR-0051", "AG_ABHIYOG_PATRA"),
    )
    assert casetype.detect(case)["type"] == "CIAA_EXTENDED"


def test_money_laundering_with_ag_chargesheet_stays_non_ciaa():
    # Non-CIAA money-laundering case: AG charge sheet + special-court order, no
    # CIAA press release. The charge-sheet title even contains "अभियोग" (which the
    # OLD gate counted as CIAA) and the venue is the Special Court — neither may
    # flip it to CIAA.
    case = _case(
        _src("सम्पत्ति शुद्धीकरण मुद्दा अभियोग पत्र", "AG_ABHIYOG_PATRA"),
        _src("Court Order - 081-CR-9999", "COURT_ORDER"),
        court_cases=["special:081-CR-9999"],
    )
    assert casetype.detect(case)["type"] == "NON_CIAA"


def test_no_ciaa_signals_is_non_ciaa():
    case = _case(_src("कुनै समाचार « Portal", "NEWS"))
    assert casetype.detect(case)["type"] == "NON_CIAA"


def test_rules_engine_ciaa_press_release_reads_source_type():
    # The duplicated press-release detector must also key on the source_type.
    score, issues = ciaa_press_release(_case(_src(PR_0051_TITLE, "CIAA_PRESS_RELEASE")))
    assert score == 100
    assert issues == []
