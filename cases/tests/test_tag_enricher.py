"""Tests for tag enricher rule-based classification."""

from django.test import TestCase
from cases.models import Case, CaseType, CaseState
from cases.services.tag_enricher import (
    classify_case_rules,
    validate_tags,
    _match_keywords,
    SECTOR_KEYWORDS,
    CORRUPTION_TYPE_KEYWORDS,
    REGION_KEYWORDS,
    _detect_amount_tier,
)


class TagEnricherRulesTests(TestCase):
    def setUp(self):
        self.case = Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
            title="Bribery in Land Revenue Office Kathmandu",
            key_allegations=[
                "Revenue official accepted bribe of NPR 5 million for land registration",
                "Forged documents used to transfer government land in Baluwatar",
            ],
            court_cases=["special:081-CR-0123"],
            bigo=50_000_000,
        )

    def test_classify_case_rules_returns_list(self):
        tags = classify_case_rules(self.case)
        self.assertIsInstance(tags, list)
        self.assertGreater(len(tags), 0)

    def test_ciaa_always_present(self):
        tags = classify_case_rules(self.case)
        self.assertIn("CIAA", tags)
        self.assertIn("Corruption", tags)

    def test_special_court_tag(self):
        tags = classify_case_rules(self.case)
        self.assertIn("Special Court", tags)

    def test_land_management_detected(self):
        tags = classify_case_rules(self.case)
        self.assertIn("Land Management", tags)

    def test_bribery_detected(self):
        tags = classify_case_rules(self.case)
        self.assertIn("Bribery", tags)

    def test_forged_documents_detected(self):
        tags = classify_case_rules(self.case)
        self.assertIn("Forged Documents", tags)

    def test_kathmandu_detected(self):
        tags = classify_case_rules(self.case)
        self.assertIn("Kathmandu", tags)

    def test_amount_tier_10m_100m(self):
        tags = classify_case_rules(self.case)
        self.assertIn("10M-100M NPR", tags)

    def test_no_false_positive_bus_in_abuse(self):
        tags = classify_case_rules(self.case)
        self.assertNotIn("Transportation", tags)

    def test_no_false_positive_mil_in_million(self):
        tags = classify_case_rules(self.case)
        self.assertNotIn("Bid Rigging", tags)

    def test_validate_tags_filters_invalid(self):
        result = validate_tags(["CIAA", "InvalidTag", "Corruption"])
        self.assertIn("CIAA", result)
        self.assertIn("Corruption", result)
        self.assertNotIn("InvalidTag", result)

    def test_validate_tags_deduplicates(self):
        result = validate_tags(["CIAA", "CIAA", "Corruption"])
        self.assertEqual(result.count("CIAA"), 1)

    def test_amount_tier_under_1m(self):
        self.assertEqual(_detect_amount_tier(500_000), "Under 1M NPR")

    def test_amount_tier_1m_10m(self):
        self.assertEqual(_detect_amount_tier(5_000_000), "1M-10M NPR")

    def test_amount_tier_over_1b(self):
        self.assertEqual(_detect_amount_tier(2_000_000_000), "Over 1B NPR")

    def test_amount_tier_none(self):
        self.assertEqual(_detect_amount_tier(None), "Unknown Amount")

    def test_match_keywords_word_boundary(self):
        text = "abuse of power and bribery in municipality"
        kw_map = {"Test1": ["bus"], "Test2": ["bribe"]}
        matched = _match_keywords(text, kw_map)
        self.assertNotIn("Test1", matched)
        self.assertIn("Test2", matched)

    def test_health_sector_detected(self):
        case = Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
            title="Health Ministry Procurement Fraud",
            key_allegations=["Hospital construction bribes and medical supply fraud"],
            court_cases=["special:081-CR-0045"],
        )
        tags = classify_case_rules(case)
        self.assertIn("Health", tags)

    def test_embezzlement_detected(self):
        case = Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
            title="Cooperative Embezzlement Scandal",
            key_allegations=["Funds embezzled from savings accounts"],
            court_cases=["special:081-CR-0078"],
        )
        tags = classify_case_rules(case)
        self.assertIn("Embezzlement", tags)

    def test_pokhara_gandaki_detected(self):
        case = Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
            title="Construction Scam in Pokhara",
            key_allegations=["Corruption in public works"],
            court_cases=["special:081-CR-0100"],
        )
        tags = classify_case_rules(case)
        self.assertIn("Gandaki", tags)
