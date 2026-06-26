"""Regression tests for the sourcing detectors after the SourceType revamp.

Migration 0027 replaced the old OFFICIAL_GOVERNMENT/LEGAL_* taxonomy with the
issuer-prefixed one (CIAA_PRESS_RELEASE, AG_ABHIYOG_PATRA, OAG_AUDIT_REPORT,
COURT_ORDER, ...). The review engine was the only live code still keyed on the
dead vocabulary, so:

  * `sourcing` flagged EVERY case for "no official/legal source" (its strong-set
    matched OFFICIAL_*/LEGAL_* prefixes that no longer exist), and
  * `charge_sheet` / `court_record` read only Devanagari title keywords, never
    the AG_ABHIYOG_PATRA / COURT_ORDER source types.

Per decisions: the strong set is the strict court/issuer four
{CIAA_PRESS_RELEASE, AG_ABHIYOG_PATRA, OAG_AUDIT_REPORT, COURT_ORDER}, and the
charge-sheet requirement is retired (charge_sheet_attached disabled; the
charge-sheet half of court_record dropped).
"""

from review import rules_engine
from review.rule_defaults import DEFAULT_RULES

OFFICIAL_ISSUE = "No primary official/legal source"


def _src(title, source_type, role="RAW"):
    return {
        "source": {
            "title": title,
            "source_type": source_type,
            "urls": [{"link": "https://example/doc", "role": role}],
        }
    }


def _case(*sources):
    return {"evidence": list(sources)}


# ── sourcing: strong (official/legal) source recognition ──────────────────────


def test_sourcing_recognises_court_and_issuer_types():
    _, issues = rules_engine.sourcing(
        _case(
            _src("CIAA press release", "CIAA_PRESS_RELEASE"),
            _src("Court Order - 080-CR-0051", "COURT_ORDER"),
        )
    )
    assert not any(OFFICIAL_ISSUE in i for i in issues)


def test_sourcing_strong_type_lifts_score():
    weak = rules_engine.sourcing(_case(_src("Some news", "NEWS")))[0]
    strong = rules_engine.sourcing(
        _case(_src("CIAA press release", "CIAA_PRESS_RELEASE"))
    )[0]
    assert strong > weak  # the +15 official/legal bonus is actually awarded now


def test_sourcing_flags_when_only_weak_types():
    _, issues = rules_engine.sourcing(
        _case(
            _src("Some news", "NEWS"),
            _src("A tweet", "SOCIAL_MEDIA"),
        )
    )
    assert any(OFFICIAL_ISSUE in i for i in issues)


def test_sourcing_excludes_law_or_bill_and_other_filing():
    # Per the "strict court/issuer" decision, these are NOT primary sources.
    _, issues = rules_engine.sourcing(
        _case(
            _src("Anti-Corruption Act 2059", "LAW_OR_BILL"),
            _src("Some other court filing", "COURT_FILING_OTHER"),
        )
    )
    assert any(OFFICIAL_ISSUE in i for i in issues)


def test_sourcing_oag_and_ag_count_as_strong():
    # All four members of OFFICIAL_LEGAL_SOURCE_TYPES satisfy the bar, not just
    # the two exercised above (CIAA_PRESS_RELEASE / COURT_ORDER).
    for st in ("OAG_AUDIT_REPORT", "AG_ABHIYOG_PATRA"):
        _, issues = rules_engine.sourcing(_case(_src("कुनै शीर्षक", st)))
        assert not any(OFFICIAL_ISSUE in i for i in issues), st


# ── court_record: verdict/order only (charge-sheet half retired) ──────────────


def test_court_record_passes_on_court_order_source_type():
    # Title carries no फैसला/आदेश keyword; detection must come from source_type.
    score, issues = rules_engine.court_record(
        _case(_src("निर्णय दस्तावेज", "COURT_ORDER"))
    )
    assert score == 100
    assert issues == []


def test_court_record_no_longer_penalises_missing_charge_sheet():
    # A verdict present but no charge sheet -> full marks (old code gave 50/100
    # and a "No charge sheet" issue).
    score, issues = rules_engine.court_record(
        _case(_src("फैसला 080-CR-0051", "COURT_ORDER"))
    )
    assert score == 100
    assert not any("अभियोग" in i for i in issues)


def test_court_record_fails_without_verdict_or_order():
    score, issues = rules_engine.court_record(_case(_src("Some news", "NEWS")))
    assert score == 0
    assert issues


def test_court_record_requires_attached_order_not_title_keyword():
    # A news headline containing फैसला/आदेश no longer satisfies "order attached" —
    # the rule requires an actual COURT_ORDER-typed document (otherwise it would
    # be a tautology: the same keyword is what makes the case HAS_VERDICT).
    score, issues = rules_engine.court_record(
        _case(_src("अदालतको फैसला सार्वजनिक", "NEWS"))
    )
    assert score == 0
    assert issues


# ── charge_sheet detector + retired rule ─────────────────────────────────────


def test_charge_sheet_detector_reads_source_type():
    # No अभियोग keyword in the title; detection via AG_ABHIYOG_PATRA source type.
    score, _ = rules_engine.charge_sheet(_case(_src("कुनै शीर्षक", "AG_ABHIYOG_PATRA")))
    assert score == 100


def test_charge_sheet_rule_is_retired():
    rule = next(r for r in DEFAULT_RULES if r["key"] == "charge_sheet_attached")
    assert rule["enabled"] is False
