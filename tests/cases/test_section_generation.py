import json

import pytest
from django.core.cache import cache

from cases.models import Case, CaseType, SourceType
from cases.services.section_generation import (
    SectionEvidence,
    SectionGenerationService,
    SectionQualityError,
    validate_section_html,
)


class FakeLLMClient:
    def __init__(self):
        self.calls = []

    async def generate(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
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
