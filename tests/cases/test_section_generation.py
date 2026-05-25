import json

import pytest
from django.core.cache import cache

from cases.models import Case, CaseType, SourceType
from cases.services.section_generation import (
    ALL_SECTION_KEYS,
    COURT_STAGE_KEYS,
    CORE_SECTION_KEYS,
    SectionEvidence,
    SectionGenerationService,
    SectionQualityError,
    SectionReadinessCheck,
    build_readiness_check,
    validate_section_html,
)


class FakeLLMClient:
    def __init__(self):
        self.calls = []

    async def generate(
        self, *, system_prompt: str, user_prompt: str, max_tokens: int
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
            }
        )
        if "अभियोगपत्रको सार" in user_prompt:
            html = "<h2>क) अभियोगपत्रको सार</h2><p>अख्तियारले भ्रष्टाचारको आरोप दाबी गरेको छ।</p>"
        elif "कानुनी व्यवस्था" in user_prompt:
            html = "<h2>ख) आकर्षित कानुनी व्यवस्था</h2><p>दफा अनुसार कारबाही माग गरिएको छ।</p>"
        elif "प्रमाणको सार" in user_prompt:
            html = "<h2>ग) प्रमाणको सार संक्षेप (अभियोजन पक्षले दाबी गरेको)</h2><p>दस्तावेजी प्रमाण पेश गरिएको छ।</p>"
        elif "अभियुक्तको बयान" in user_prompt:
            html = (
                "<h2>घ) अभियुक्तको बयान</h2><p>प्रतिवादीले आरोप अस्वीकार गरेका छन्।</p>"
            )
        elif "विशेष अदालतको फैसला" in user_prompt:
            html = (
                "<h2>ङ) विशेष अदालतको फैसला</h2><p>विशेष अदालतले दोषी ठहर गरेको छ।</p>"
            )
        elif "पुनरावेदन" in user_prompt:
            html = "<h2>च) पुनरावेदन</h2><p>उच्च अदालतमा पुनरावेदन विचाराधीन छ।</p>"
        elif "सर्वोच्च अदालत" in user_prompt:
            html = "<h2>छ) सर्वोच्च अदालत</h2><p>सर्वोच्चले फैसला सदर गरेको छ।</p>"
        elif "अवलोकन" in user_prompt:
            html = "<h2>ज) अवलोकन</h2><p>मुद्दा महत्वपूर्ण छ।</p>"
        else:
            html = "<p>अख्तियारले सार्वजनिक पदाधिकारीविरुद्ध भ्रष्टाचार मुद्दा दायर गरेको छ।</p>"
        return json.dumps({"html": html, "confidence": "high"})


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_generate_core_sections_fans_out_and_stores_db_cache():
    cache.clear()
    case = await Case.objects.acreate(
        case_type=CaseType.CORRUPTION,
        title="CIAA case",
        description="",
        timeline=[],
        evidence=[],
    )
    evidence = [
        SectionEvidence(
            source_id="source:1",
            title="अभियोग पत्र",
            source_type=SourceType.OFFICIAL_GOVERNMENT,
            text="प्रतिवादीले भ्रष्टाचार गरेको आरोप छ।",
        )
    ]
    llm = FakeLLMClient()
    service = SectionGenerationService(llm)

    results = await service.generate_core_sections(case, evidence)

    assert set(results) == {"short_description", "ka", "kha", "ga"}
    assert len(llm.calls) == 4
    await case.arefresh_from_db()
    section_cache = case.versionInfo["section_generation_cache"]
    assert set(section_cache) == {"short_description", "ka", "kha", "ga"}
    assert section_cache["ka"]["confidence"] == "high"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_generate_section_reuses_db_cache():
    cache.clear()
    case = await Case.objects.acreate(
        case_type=CaseType.CORRUPTION,
        title="CIAA case",
        description="",
        timeline=[],
        evidence=[],
    )
    evidence = [
        SectionEvidence(
            source_id="source:1",
            title="अभियोग पत्र",
            source_type=SourceType.OFFICIAL_GOVERNMENT,
            text="प्रतिवादीले भ्रष्टाचार गरेको आरोप छ।",
        )
    ]
    llm = FakeLLMClient()
    service = SectionGenerationService(llm)

    await service.generate_core_sections(case, evidence, section_keys=("ka",))
    first_call_count = len(llm.calls)
    await case.arefresh_from_db()
    results = await service.generate_core_sections(case, evidence, section_keys=("ka",))

    assert len(llm.calls) == first_call_count
    assert results["ka"].from_cache is True


def test_validate_section_html_rejects_disallowed_tags():
    with pytest.raises(SectionQualityError, match="disallowed HTML tags"):
        validate_section_html("<script>alert(1)</script><p>नेपाली पाठ</p>")


def test_validate_section_html_rejects_non_nepali_output():
    with pytest.raises(SectionQualityError, match="Nepali"):
        validate_section_html("<p>This is only English text.</p>")


# --- Phase 3: Court-Stage Conditional Section Tests ---


class TestSectionReadinessCheck:
    def test_all_core_sections_always_active(self):
        check = SectionReadinessCheck(court_cases=[], evidence_text="")
        for key in CORE_SECTION_KEYS:
            result = check.check_section(key)
            assert result.active, f"core section {key} should be active"
            assert "core section" in result.reason

    def test_no_court_cases_no_evidence_skips_all_court_stage(self):
        check = SectionReadinessCheck(court_cases=[], evidence_text="")
        active = check.active_court_stage_keys()
        assert active == []

    def test_special_court_case_activates_gha_and_nga(self):
        check = SectionReadinessCheck(
            court_cases=["special:076-CR-0456"], evidence_text=""
        )
        assert check.check_section("gha").active
        assert check.check_section("nga").active
        assert not check.check_section("cha").active
        assert not check.check_section("chha").active

    def test_special_and_supreme_activates_all_except_cha(self):
        check = SectionReadinessCheck(
            court_cases=["special:076-CR-0456", "supreme:078-WC-0123"],
            evidence_text="",
        )
        assert check.check_section("gha").active
        assert check.check_section("nga").active
        assert not check.check_section("cha").active
        assert check.check_section("chha").active

    def test_evidence_keywords_activate_sections(self):
        check = SectionReadinessCheck(
            court_cases=[],
            evidence_text="विशेष अदालतले फैसला सुनाएको छ। अभियुक्तले बयान दिए।",
        )
        assert check.check_section("gha").active
        assert check.check_section("nga").active
        assert not check.check_section("cha").active

    def test_appeal_evidence_activates_cha(self):
        check = SectionReadinessCheck(
            court_cases=[],
            evidence_text="पुनरावेदन अदालतमा मुद्दा विचाराधीन छ।",
        )
        assert check.check_section("cha").active

    def test_supreme_evidence_activates_chha(self):
        check = SectionReadinessCheck(
            court_cases=[],
            evidence_text="सर्वोच्च अदालतले अन्तिम फैसला सुनाएको छ।",
        )
        assert check.check_section("chha").active

    def test_ja_requires_two_stages(self):
        check = SectionReadinessCheck(
            court_cases=["special:076-CR-0456"], evidence_text=""
        )
        assert not check.check_section("ja").active
        check2 = SectionReadinessCheck(
            court_cases=["special:076-CR-0456", "supreme:078-WC-0123"],
            evidence_text="",
        )
        assert check2.check_section("ja").active

    def test_active_court_stage_keys_returns_correct_subset(self):
        check = SectionReadinessCheck(
            court_cases=["special:076-CR-0456"],
            evidence_text="पुनरावेदन दर्ता भएको छ।",
        )
        active = check.active_court_stage_keys()
        assert "gha" in active
        assert "nga" in active
        assert "cha" in active
        assert "chha" not in active
        assert "ja" in active  # >= 2 stages

    def test_build_readiness_check_from_case(self):
        evidence = [
            SectionEvidence(
                source_id="s:1",
                title="फैसला",
                source_type=SourceType.LEGAL_COURT_ORDER,
                text="विशेष अदालतले दोषी ठहर गरेको फैसला।",
            )
        ]
        check = build_readiness_check(
            Case(
                case_type=CaseType.CORRUPTION,
                court_cases=["special:076-CR-0456"],
            ),
            evidence,
        )
        assert check.check_section("nga").active
        assert "court_cases field" in check.check_section("nga").reason

    def test_invalid_court_entries_are_ignored(self):
        check = SectionReadinessCheck(
            court_cases=["baglunghc:123", "not-a-court", "", None],
            evidence_text="",
        )
        active = check.active_court_stage_keys()
        assert active == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_generate_all_sections_includes_only_active_court_stages():
    cache.clear()
    case = await Case.objects.acreate(
        case_type=CaseType.CORRUPTION,
        title="CIAA case with special court",
        description="",
        timeline=[],
        evidence=[],
        court_cases=["special:076-CR-0456"],
    )
    evidence = [
        SectionEvidence(
            source_id="source:1",
            title="अभियोग पत्र",
            source_type=SourceType.OFFICIAL_GOVERNMENT,
            text="प्रतिवादीले भ्रष्टाचार गरेको आरोप छ। विशेष अदालतमा मुद्दा दायर।",
        )
    ]
    llm = FakeLLMClient()
    service = SectionGenerationService(llm)

    results = await service.generate_all_sections(case, evidence)

    assert "short_description" in results
    assert "ka" in results
    assert "kha" in results
    assert "ga" in results
    assert "gha" in results
    assert "nga" in results
    assert "cha" not in results
    assert "chha" not in results
    assert "ja" in results  # special court => CHARGE_SHEET + SPECIAL_COURT = 2 stages


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_generate_all_sections_without_conditional_is_core_only():
    cache.clear()
    case = await Case.objects.acreate(
        case_type=CaseType.CORRUPTION,
        title="CIAA case",
        description="",
        timeline=[],
        evidence=[],
        court_cases=["special:076-CR-0456"],
    )
    evidence = [
        SectionEvidence(
            source_id="source:1",
            title="अभियोग पत्र",
            source_type=SourceType.OFFICIAL_GOVERNMENT,
            text="प्रतिवादीले भ्रष्टाचार गरेको आरोप छ।",
        )
    ]
    llm = FakeLLMClient()
    service = SectionGenerationService(llm)

    results = await service.generate_all_sections(
        case, evidence, include_conditional=False
    )

    assert set(results) == set(CORE_SECTION_KEYS)


def test_all_section_keys_includes_all_8():
    assert len(ALL_SECTION_KEYS) == 8
    assert set(ALL_SECTION_KEYS) == set(CORE_SECTION_KEYS) | set(COURT_STAGE_KEYS)
    assert "short_description" in ALL_SECTION_KEYS
    assert "gha" in ALL_SECTION_KEYS
    assert "ja" in ALL_SECTION_KEYS


def test_court_stage_section_specs_have_required_fields():
    for key in COURT_STAGE_KEYS:
        from cases.services.section_generation import SECTION_SPECS

        spec = SECTION_SPECS[key]
        assert spec.key == key
        assert spec.heading is not None, f"{key} missing heading"
        assert spec.max_tokens > 0, f"{key} missing max_tokens"
        assert spec.evidence_budget > 0, f"{key} missing evidence_budget"
        assert spec.instructions, f"{key} missing instructions"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_generate_core_sections_fans_out_and_stores_db_cache():
    cache.clear()
    case = await Case.objects.acreate(
        case_type=CaseType.CORRUPTION,
        title="CIAA case",
        description="",
        timeline=[],
        evidence=[],
    )
    evidence = [
        SectionEvidence(
            source_id="source:1",
            title="अभियोग पत्र",
            source_type=SourceType.OFFICIAL_GOVERNMENT,
            text="प्रतिवादीले भ्रष्टाचार गरेको आरोप छ।",
        )
    ]
    llm = FakeLLMClient()
    service = SectionGenerationService(llm)

    results = await service.generate_core_sections(case, evidence)

    assert set(results) == {"short_description", "ka", "kha", "ga"}
    assert len(llm.calls) == 4
    await case.arefresh_from_db()
    section_cache = case.versionInfo["section_generation_cache"]
    assert set(section_cache) == {"short_description", "ka", "kha", "ga"}
    assert section_cache["ka"]["confidence"] == "high"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_generate_section_reuses_db_cache():
    cache.clear()
    case = await Case.objects.acreate(
        case_type=CaseType.CORRUPTION,
        title="CIAA case",
        description="",
        timeline=[],
        evidence=[],
    )
    evidence = [
        SectionEvidence(
            source_id="source:1",
            title="अभियोग पत्र",
            source_type=SourceType.OFFICIAL_GOVERNMENT,
            text="प्रतिवादीले भ्रष्टाचार गरेको आरोप छ।",
        )
    ]
    llm = FakeLLMClient()
    service = SectionGenerationService(llm)

    await service.generate_core_sections(case, evidence, section_keys=("ka",))
    first_call_count = len(llm.calls)
    await case.arefresh_from_db()
    results = await service.generate_core_sections(case, evidence, section_keys=("ka",))

    assert len(llm.calls) == first_call_count
    assert results["ka"].from_cache is True


def test_validate_section_html_rejects_disallowed_tags():
    with pytest.raises(SectionQualityError, match="disallowed HTML tags"):
        validate_section_html("<script>alert(1)</script><p>नेपाली पाठ</p>")


def test_validate_section_html_rejects_non_nepali_output():
    with pytest.raises(SectionQualityError, match="Nepali"):
        validate_section_html("<p>This is only English text.</p>")
