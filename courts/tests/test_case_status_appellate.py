# SPDX-License-Identifier: Hippocratic-3.0
"""Supreme Court appellate dispositions recovered from ``enrichment_hearings``.

Supreme's typed verdict columns are entirely empty — 107,554 rows, zero
``case_status``, zero ``verdict_date_ad`` — while the dispositions sat in
``extra_data.enrichment_hearings`` the whole time. The chain that produced that:
the portal wrote its column header ``आदेश /फैसलाको किसिम`` into ``case_status``;
DQ-01 correctly NULLed it; DQ-03 could then find no paren-form date to parse; and
``verdict_from_hearings`` only understood the Special Court's trial vocabulary
(सफाई/ठहर/आंशिक), so the appellate words meant nothing to it.

The tests that matter most here are the NEGATIVE ones. ``अन्तिम आदेश`` translates
as "final order" but the portal puts it on plainly interlocutory orders — a call
for a status report, a summons, a request for a written reply. Keying off that
label would stamp a verdict date onto thousands of live appeals, which is a worse
outcome than the gap it fixes.
"""

from datetime import date

import pytest

from courts import case_status as cs


def hearing(status, order_type, when="2082-03-20"):
    """A Supreme-schema enrichment hearing (``status``/``order_type``)."""
    return {"date": when, "type": "hearing", "status": status, "order_type": order_type}


class TestTheLabelFinalOrderIsNotEvidenceOfAVerdict:
    """The core defence. Each of these carries status=अन्तिम आदेश and is still live."""

    @pytest.mark.parametrize(
        "order_type",
        [
            "कैफियत प्रतिवेदन माग्ने",          # 1,289 of the sampled entries
            "लिखित जवाफ माग्ने",
            "प्रत्यर्थी झिकाउने",
            "बयान गराउने",
            "मिसिल झिकाउने",
            "अन्तरिम आदेश जारी",
            "अवहेलना दर्ता गरी लिखित जवाफ माग्ने",
            "धरौटी घटाएको",
        ],
    )
    def test_an_interlocutory_order_yields_no_verdict(self, order_type):
        assert cs.outcome_from_hearings([hearing("अन्तिम आदेश", order_type)]) is None

    @pytest.mark.parametrize(
        "order_type",
        [
            "पूर्ण इजलासमा पेस हुने",
            "संवै‌धानिक इजलासमा पेस हुने",
            "बृहत् पूर्ण इजलासमा पेस हुने",
            "पुनः निर्णयका लागि पठाउने",
        ],
    )
    def test_a_referral_to_another_bench_is_not_a_disposition(self, order_type):
        """Even labelled फैसला: the case continues, just somewhere else."""
        assert cs.outcome_from_hearings([hearing("फैसला", order_type)]) is None

    def test_an_unknown_order_type_yields_nothing_rather_than_a_guess(self):
        outcome = cs.outcome_from_hearings(
            [hearing("अन्तिम आदेश", "केही नयाँ कुरा जुन सूचीमा छैन")]
        )
        assert outcome is None
        assert cs.classify_order_type("केही नयाँ कुरा जुन सूचीमा छैन")[0] == "unmapped"


class TestTheAppellateAxis:
    @pytest.mark.parametrize(
        "order_type,expected",
        [
            ("सदर", cs.AFFIRMED),
            ("आदेश सदर", cs.AFFIRMED),
            ("शुरु सदर", cs.AFFIRMED),
            ("आदेश बदर", cs.REVERSED),
            ("उल्टी", cs.REVERSED),
            ("उल्टि", cs.REVERSED),
            ("केही उल्टी", cs.PARTIALLY_REVERSED),
            ("केहि उल्टी", cs.PARTIALLY_REVERSED),
            ("आंशिक बदर", cs.PARTIALLY_REVERSED),
        ],
    )
    def test_affirm_and_reverse_map(self, order_type, expected):
        """सदर is the commonest Supreme outcome and the trial-court enum had no
        word for it — an appeal that fails leaves the judgment below standing."""
        outcome = cs.outcome_from_hearings([hearing("फैसला", order_type)])
        assert outcome is not None and outcome.verdict_type == expected

    @pytest.mark.parametrize(
        "order_type,expected",
        [
            ("रिट जारी", cs.CLAIM_UPHELD),
            ("परमादेश जारी", cs.CLAIM_UPHELD),
            ("आशिक रिट जारी", cs.PARTIALLY_UPHELD),
            ("रिट खारेज", cs.CLAIM_DENIED),
            ("मिलापत्र", cs.SETTLED),
            ("तामेली", cs.STRUCK_OFF),
            ("मुद्दा फिर्ता", cs.WITHDRAWN),
        ],
    )
    def test_writ_relief_granted_versus_refused(self, order_type, expected):
        outcome = cs.outcome_from_hearings([hearing("फैसला", order_type)])
        assert outcome is not None and outcome.verdict_type == expected

    def test_a_dismissed_writ_denies_the_claim_rather_than_quashing_anything(self):
        """Deliberate divergence from _OUTCOME_MAP's bare खारेज → QUASHED. When a
        writ is खारेज it is the PETITION that falls; nothing is quashed and the
        status quo survives, so QUASHED would invert the meaning."""
        outcome = cs.outcome_from_hearings([hearing("फैसला", "रिट खारेज")])
        assert outcome.verdict_type == cs.CLAIM_DENIED
        assert cs._OUTCOME_MAP["खारेज"] == cs.QUASHED  # the trial-side reading


class TestTheSpacingAndSuffixTraps:
    def test_the_portal_omits_a_space_the_existing_map_requires(self):
        """``_OUTCOME_MAP`` holds "माग बमोजिम हुने"; the portal writes
        "मागबमोजिम हुने". 2,289 sampled entries hang on that one space, and
        ``_norm_outcome`` only collapses runs, so it cannot bridge it."""
        assert "माग बमोजिम हुने" in cs._OUTCOME_MAP
        assert cs._norm_outcome("मागबमोजिम हुने") not in cs._OUTCOME_MAP
        outcome = cs.outcome_from_hearings([hearing("अन्तिम आदेश", "मागबमोजिम हुने")])
        assert outcome is not None and outcome.verdict_type == cs.CLAIM_UPHELD

    @pytest.mark.parametrize(
        "order_type,expected",
        [
            ("आदेश सदर फाइल हेर्नुहोस्", cs.AFFIRMED),
            ("मुद्दा फिर्ता फाइल हेर्नुहोस्", cs.WITHDRAWN),
            ("मागबमोजिम हुने फाइल हेर्नुहोस्", cs.CLAIM_UPHELD),
        ],
    )
    def test_the_ui_see_the_file_suffix_is_stripped(self, order_type, expected):
        outcome = cs.outcome_from_hearings([hearing("फैसला", order_type)])
        assert outcome is not None and outcome.verdict_type == expected


class TestTwoDecadesOfInconsistentSpelling:
    """Where most of the recovered volume actually came from.

    Keying the tables on the modern spelling alone left 24,321 terminal entries
    unmatched. Folding the variants took that to 1,201 — the mapping was nearly
    right all along; the corpus just spells things several ways.
    """

    def test_one_vowel_length_hid_sixteen_thousand_entries(self):
        """अनुमती (older) vs अनुमति (modern): leave to appeal refused. 16,677
        entries in the corpus turn on that single ी/ि."""
        modern = cs.outcome_from_hearings([hearing("अन्तिम आदेश", "अनुमति नहुने")])
        older = cs.outcome_from_hearings([hearing("अन्तिम आदेश", "अनुमती नहुने")])
        assert modern.verdict_type == older.verdict_type == cs.CLAIM_DENIED

    @pytest.mark.parametrize(
        "older,expected",
        [
            ("वदर", cs.REVERSED),                    # व/ब are interchangeable
            ("आदेश वदर", cs.REVERSED),
            ("कानुन वमोजिम गर्नु", cs.PROCEDURAL),   # + कानुन/कानून
            ("माग वमोजिम हुने", cs.CLAIM_UPHELD),
            ("माग वमोजङ्गम हुने", cs.CLAIM_UPHELD),   # mojibake for बमोजिम
            ("शंशोधन हुने", cs.AMENDED),
            ("आशिंक दावी पुग्ने", cs.PARTIALLY_UPHELD),
        ],
    )
    def test_orthographic_variants_resolve(self, older, expected):
        outcome = cs.outcome_from_hearings([hearing("फैसला", older)])
        assert outcome is not None, f"{older!r} should classify"
        assert outcome.verdict_type == expected

    def test_an_invisible_zero_width_joiner_does_not_split_a_match(self):
        """``साधारण तारेखमा राख्‍ने`` and ``…राख्ने`` differ by one U+200D and look
        identical in every log — 83 entries hid behind it."""
        with_zwj = "साधारण तारेखमा राख्‍ने"
        assert with_zwj != "साधारण तारेखमा राख्ने"
        assert cs.classify_order_type(with_zwj)[0] == "interlocutory"
        assert cs.classify_order_type("साधारण तारेखमा राख्ने")[0] == "interlocutory"

    def test_a_variant_of_a_partial_conviction_is_not_discarded(self):
        """आंशीक reaches the trial-court table only because the fallback matches on
        the normalised key; against the raw cell it missed."""
        outcome = cs.outcome_from_hearings([hearing("फैसला", "आंशीक")])
        assert outcome is not None
        assert outcome.verdict_type == cs.PARTIALLY_CONVICTED

    def test_folding_is_applied_to_both_sides_so_a_legitimate_v_still_matches(self):
        """वन्दीप्रत्येक्षीकरण contains a real व. Because the fold runs over the table
        keys too, it is folded identically and the compound still resolves."""
        outcome = cs.outcome_from_hearings(
            [hearing("फैसला", "वन्दीप्रत्येक्षीकरण खारेज, परमादेश जारी")]
        )
        assert outcome is not None and outcome.verdict_type == cs.PARTIALLY_UPHELD


class TestCompoundReliefIsNotReadAsItsFirstWord:
    @pytest.mark.parametrize(
        "order_type",
        [
            "वन्दीप्रत्येक्षीकरण खारेज, परमादेश जारी",
            "रिट खारेज, परमादेश जारी",
            "रिट खारेज, निर्देशनात्मक आदेश जारी",
        ],
    )
    def test_dismissed_but_granted_relief_is_partial_success(self, order_type):
        """A substring match on खारेज would call these outright defeats. The
        petitioner lost the writ and won a directive — partly successful."""
        outcome = cs.outcome_from_hearings([hearing("फैसला", order_type)])
        assert outcome is not None
        assert outcome.verdict_type == cs.PARTIALLY_UPHELD


class TestTheDateComesBackToo:
    def test_the_disposing_sittings_own_date_is_returned_in_both_calendars(self):
        """The whole point: DQ-03 has no paren form to read for Supreme, so the
        date must come from the hearing entry."""
        outcome = cs.outcome_from_hearings([hearing("फैसला", "सदर", when="2082-03-20")])
        assert outcome.verdict_date_bs == "2082-03-20"
        assert outcome.verdict_date_ad == date(2025, 7, 4)

    def test_a_devanagari_or_slashed_date_is_normalised(self):
        outcome = cs.outcome_from_hearings(
            [hearing("फैसला", "सदर", when="२०८२/०३/२०")]
        )
        assert outcome.verdict_date_bs == "2082-03-20"
        assert outcome.verdict_date_ad == date(2025, 7, 4)

    def test_a_disposition_with_an_unparseable_date_still_yields_the_verdict(self):
        outcome = cs.outcome_from_hearings([hearing("फैसला", "सदर", when="")])
        assert outcome is not None and outcome.verdict_type == cs.AFFIRMED
        assert outcome.verdict_date_bs is None and outcome.verdict_date_ad is None

    def test_the_last_disposing_sitting_wins_not_the_first(self):
        """A case can be decided, reopened on review, and decided again."""
        outcome = cs.outcome_from_hearings([
            hearing("फैसला", "सदर", when="2081-01-01"),
            hearing("अन्तिम आदेश", "कैफियत प्रतिवेदन माग्ने", when="2081-06-01"),
            hearing("फैसला", "उल्टी", when="2082-03-20"),
        ])
        assert outcome.verdict_type == cs.REVERSED
        assert outcome.verdict_date_bs == "2082-03-20"

    def test_the_raw_cell_is_preserved_for_audit(self):
        outcome = cs.outcome_from_hearings([hearing("फैसला", "आदेश सदर")])
        assert outcome.order_type_raw == "आदेश सदर"


class TestTheSpecialCourtPathIsUntouched:
    """The trial vocabulary still resolves via its own substring map, on the
    Special schema (``case_status``/``decision_type``)."""

    @pytest.mark.parametrize(
        "decision,expected",
        [
            ("सफाई", cs.ACQUITTED),
            ("अभियोग दाबी ठहर", cs.CONVICTED),
            ("आंशिक ठहर", cs.PARTIALLY_CONVICTED),
        ],
    )
    def test_special_court_decisions_still_map(self, decision, expected):
        rows = [{"date": "2081-02-30", "case_status": "फैसला", "decision_type": decision}]
        assert cs.verdict_from_hearings(rows) == expected

    def test_the_shim_still_returns_a_bare_string(self):
        rows = [hearing("फैसला", "सदर")]
        assert cs.verdict_from_hearings(rows) == cs.AFFIRMED

    @pytest.mark.parametrize("hearings", [None, [], [{}], ["not a dict"]])
    def test_junk_input_is_survivable(self, hearings):
        assert cs.outcome_from_hearings(hearings) is None
        assert cs.verdict_from_hearings(hearings) is None
