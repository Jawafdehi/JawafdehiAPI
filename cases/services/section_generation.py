from __future__ import annotations

import asyncio
import hashlib
import html.parser
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from asgiref.sync import sync_to_async
from django.core.cache import cache

from cases.models import Case, DocumentSource, SourceType

logger = logging.getLogger(__name__)


DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
TAG_RE = re.compile(r"<[^>]+>")
ALLOWED_TAGS = {
    "h2",
    "h3",
    "p",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "strong",
    "em",
}


@dataclass(frozen=True)
class SectionSpec:
    key: str
    title: str
    heading: str | None
    max_tokens: int
    evidence_budget: int
    priority_source_types: tuple[str, ...]
    instructions: str
    few_shot_example: str = ""


@dataclass(frozen=True)
class SectionEvidence:
    source_id: str
    title: str
    source_type: str | None
    text: str


@dataclass(frozen=True)
class SectionGenerationResult:
    key: str
    html: str
    confidence: str
    from_cache: bool = False


class SectionLLMClient(Protocol):
    async def generate(
        self, *, system_prompt: str, user_prompt: str, max_tokens: int
    ) -> str: ...


class HTMLValidationParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.invalid_tags: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in ALLOWED_TAGS:
            self.invalid_tags.append(tag)
        if tag not in {"br", "hr", "img", "meta", "link", "input"}:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            self.invalid_tags.append(tag)
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


SYSTEM_PROMPT = """You are a senior Nepali legal analyst writing case overviews for JAWAFDEHI, a public legal transparency platform serving Nepali citizens.

ROLE: Extract and summarize CIAA case information from evidence documents into structured Nepali-language overview sections.

LANGUAGE RULES:
- Write entirely in Nepali Devanagari script (देवनागरी).
- Use simple, layman-friendly Nepali — your audience is the general public, not lawyers.
- English is allowed ONLY for: proper nouns, legal citation numbers, case numbers, ISO dates, and institution names.
- Never mix English and Nepali within the same sentence.

EVIDENCE RULES:
- Use ONLY evidence provided in the prompt. Never fabricate, infer, or assume facts.
- If evidence is ambiguous or contradictory, state only what is clearly supported.
- When sources conflict, prefer the most recent official document (court orders first, then government documents, then news articles).
- If a section's required information is missing from evidence, output a brief note saying the information was not found in available evidence — do not leave the section empty or make up content.

FORMATTING RULES:
- Output valid HTML using only: <h2>, <h3>, <p>, <ul>, <ol>, <li>, <table>, <thead>, <tbody>, <tr>, <th>, <td>, <strong>, <em>.
- Use <h2> for section headings with Nepali letter numbering (do not put the heading in an <h3>).
- Use <h3> for sub-headings within sections.
- Do not output empty headings, empty tables, empty lists, or placeholder text like "[विवरण उपलब्ध छैन]".
- Use <strong> to highlight names, amounts, and key legal terms.
- Use <table> for structured financial data only; use <ul>/<ol> for lists of entities, arguments, or documents.

QUALITY RULES:
- Every output must be substantive — if you cannot extract meaningful content, lower your confidence to "low" and explain what's missing.
- Double-check that all names, amounts, and dates match the evidence exactly.
- The <h2> heading must be the first element in the html field.
- Do not output any text outside the JSON wrapper.

OUTPUT FORMAT:
Return valid JSON with no markdown fences, no trailing commas, and no text outside the JSON object:
{"html": "<h2>...</h2><h3>...</h3><p>...</p>", "confidence": "high|medium|low"}

CONFIDENCE GUIDELINES:
- "high": Evidence directly and clearly supports all claims in the section.
- "medium": Evidence supports most claims but some details are inferred or thin.
- "low": Sparse evidence; output is based on minimal or indirect support.
"""

SECTION_SPECS: dict[str, SectionSpec] = {
    "short_description": SectionSpec(
        key="short_description",
        title="Short description",
        heading=None,
        max_tokens=300,
        evidence_budget=8000,
        priority_source_types=(SourceType.OFFICIAL_GOVERNMENT, SourceType.MEDIA_NEWS),
        instructions="""TASK: Write a 1-3 sentence summary of this CIAA case in Nepali.

INCLUDE:
- Who is accused: name, position, institution.
- Core allegation: corruption type or misconduct.
- Amount involved, if financial.
- Current court stage, if known.

DO NOT INCLUDE legal citations, detailed evidence discussion, or procedural history.
Return only one <p> block in the html field. Do not wrap in <h2> or any heading.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<p>अख्तियार दुरुपयोग अनुसन्धान आयोगले नेपाल सरकारको मन्त्रालयका तत्कालीन सचिव <strong>रमेश कुमार शर्मा</strong> विरुद्ध घुस रकम रु. ५०,००,०००।— (पचास लाख) लिई भ्रष्टाचार गरेको अभियोगमा विशेष अदालतमा मुद्दा दायर गरेको छ। हाल उक्त मुद्दा विशेष अदालतमा विचाराधीन छ।</p>", "confidence": "high"}""",
    ),
    "ka": SectionSpec(
        key="ka",
        title="क) अभियोगपत्रको सार",
        heading="क) अभियोगपत्रको सार",
        max_tokens=2000,
        evidence_budget=20000,
        priority_source_types=(SourceType.OFFICIAL_GOVERNMENT, SourceType.MEDIA_NEWS),
        instructions="""TASK: Summarize the CIAA charge sheet allegations in clear Nepali.

STRUCTURE:
<h2>क) अभियोगपत्रको सार</h2>
<h3>मुख्य आरोप</h3>
<p>Core allegation in 1-2 paragraphs explaining what happened, when, and how.</p>
<h3>संलग्न व्यक्तिहरू</h3>
<ul><li><strong>Name</strong> — Position — Specific allegation</li></ul>
<h3>आरोपित रकम र क्षति</h3>
<p>Amount involved, how calculated, what was lost. Use a table if multiple amounts.</p>
<h3>मुद्दा दायर मिति र अवस्था</h3>
<p>Filing date, court, current procedural status.</p>

RULES:
- Do not repeat the short_description; write a full structured summary.
- If there are multiple accused, list each with their specific allegation.
- If the charge sheet cites specific legal provisions, mention them briefly.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<h2>क) अभियोगपत्रको सार</h2><h3>मुख्य आरोप</h3><p>अख्तियार दुरुपयोग अनुसन्धान आयोगले <strong>रमेश कुमार शर्मा</strong> (तत्कालीन सचिव, भौतिक पूर्वाधार मन्त्रालय) ले आ.व. २०७९/८० मा सडक निर्माण ठेक्का सम्झौतामा अनियमितता गरी भ्रष्टाचार गरेको आरोप लगाएको छ। अभियोगपत्र अनुसार, शर्माले ठेकेदार कम्पनीसँग मिलेमतो गरी सरकारी मापदण्ड विपरीत ठेक्का स्वीकृत गरेको र बदलामा घुस रकम लिएको दाबी गरिएको छ।</p><h3>संलग्न व्यक्तिहरू</h3><ul><li><strong>रमेश कुमार शर्मा</strong> — तत्कालीन सचिव — मुख्य आरोपित, ठेक्का अनियमितता र घुस लिएको आरोप</li><li><strong>सुरेश बस्नेत</strong> — तत्कालीन शाखा अधिकृत — झुटा कागजात तयार गरी ठेक्का प्रक्रियामा सहयोग गरेको आरोप</li></ul><h3>आरोपित रकम र क्षति</h3><p>अभियोगपत्रमा कुल बिगो रु. <strong>५,००,००,०००।— (पाँच करोड)</strong> दाबी गरिएको छ। जसमध्ये रु. ३,५०,००,०००।— ठेक्का मूल्य वृद्धिबाट र रु. १,५०,००,०००।— गुणस्तरहीन निर्माणबाट भएको क्षति रहेको छ।</p><h3>मुद्दा दायर मिति र अवस्था</h3><p>मुद्दा २०८० माघ १५ गते विशेष अदालत, काठमाडौंमा दायर गरिएको थियो। हाल उक्त मुद्दा विशेष अदालतमा विचाराधीन छ।</p>", "confidence": "high"}""",
    ),
    "kha": SectionSpec(
        key="kha",
        title="ख) आकर्षित कानुनी व्यवस्था",
        heading="ख) आकर्षित कानुनी व्यवस्था",
        max_tokens=1200,
        evidence_budget=12000,
        priority_source_types=(
            SourceType.LEGAL_PROCEDURAL,
            SourceType.OFFICIAL_GOVERNMENT,
        ),
        instructions="""TASK: List and explain the legal provisions cited in the charge sheet.

STRUCTURE:
<h2>ख) आकर्षित कानुनी व्यवस्था</h2>
<h3>Provision name/number</h3>
<p>Simple explanation of what this provision prohibits in layman's Nepali.</p>
<p><strong>सजाय:</strong> Penalty range if specified in the provision.</p>

RULES:
- Explain each cited provision in simple Nepali that a non-lawyer can understand.
- Do not list provisions that are not explicitly cited in evidence.
- If the charge sheet cites sections of the Prevention of Corruption Act, explain what each section covers.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<h2>ख) आकर्षित कानुनी व्यवस्था</h2><h3>भ्रष्टाचार निवारण ऐन, २०५९ — दफा ३</h3><p>यस दफाले सार्वजनिक पद धारण गरेको व्यक्तिले पदीय हैसियतको दुरुपयोग गरी आफू वा अरू कसैलाई लाभ पुर्याउने कार्यलाई भ्रष्टाचारको रूपमा परिभाषित गर्दछ।</p><p><strong>सजाय:</strong> कसुरको मात्रा अनुसार कैद र जरिवाना वा दुवै सजाय हुन सक्छ।</p><h3>भ्रष्टाचार निवारण ऐन, २०५९ — दफा ८</h3><p>यस दफाले घुस लिने वा दिने कार्यलाई अपराधको रूपमा परिभाषित गर्दछ। सार्वजनिक पदाधिकारीले कुनै काम गर्न वा नगर्नको लागि घुस माग्ने, लिने वा लिन सहमत हुने कार्य यस दफा अन्तर्गत दण्डनीय छ।</p><p><strong>सजाय:</strong> बिगो बराबरको जरिवाना र बिगोको मात्रा अनुसार कैद।</p>", "confidence": "high"}""",
    ),
    "ga": SectionSpec(
        key="ga",
        title="ग) प्रमाणको सार संक्षेप",
        heading="ग) प्रमाणको सार संक्षेप (अभियोजन पक्षले दाबी गरेको)",
        max_tokens=2000,
        evidence_budget=20000,
        priority_source_types=(
            SourceType.FINANCIAL_FORENSIC,
            SourceType.OFFICIAL_GOVERNMENT,
            SourceType.INTERNAL_CORPORATE,
            SourceType.MEDIA_NEWS,
        ),
        instructions="""TASK: Summarize the prosecution evidence by type and strength.

STRUCTURE:
<h2>ग) प्रमाणको सार संक्षेप (अभियोजन पक्षले दाबी गरेको)</h2>
<h3>दस्तावेजी प्रमाण</h3>
<ul><li><strong>Document type</strong> — what it shows, why it matters</li></ul>
<h3>आर्थिक प्रमाण</h3>
<p>Financial evidence summary. Use a table when amounts are structured across categories.</p>
<h3>साक्षी/गवाही</h3>
<ul><li><strong>Witness role</strong> — key testimony point</li></ul>
<h3>अन्य प्रमाण</h3>
<p>Other evidence types (electronic records, expert reports, etc.).</p>

RULES:
- Group evidence by type, not by source.
- Omit evidence-type subsections with no evidence.
- Always note whether evidence is direct or circumstantial.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<h2>ग) प्रमाणको सार संक्षेप (अभियोजन पक्षले दाबी गरेको)</h2><h3>दस्तावेजी प्रमाण</h3><ul><li><strong>ठेक्का सम्झौता पत्र</strong> — मिति २०७९ श्रावण १० मा भएको सडक निर्माण ठेक्का सम्झौता जसमा अत्यधिक मूल्य वृद्धि भएको देखिन्छ।</li><li><strong>भुक्तानी भौचर</strong> — ठेकेदारलाई गरिएको भुक्तानीको अभिलेख जसले बढी रकम भुक्तानी भएको पुष्टि गर्दछ।</li><li><strong>गुणस्तर परीक्षण प्रतिवेदन</strong> — निर्माण सम्पन्न पश्चातको गुणस्तर जाँच प्रतिवेदन जसमा तोकिएको मापदण्ड पूरा नभएको उल्लेख छ।</li></ul><h3>आर्थिक प्रमाण</h3><table><thead><tr><th>शीर्षक</th><th>रकम (रु.)</th><th>विवरण</th></tr></thead><tbody><tr><td>ठेक्का मूल्य वृद्धि</td><td>३,५०,००,०००।—</td><td>मूल अनुमानित लागतभन्दा बढी</td></tr><tr><td>गुणस्तरहीन क्षति</td><td>१,५०,००,०००।—</td><td>कमसल सामग्री प्रयोग</td></tr></tbody></table><h3>साक्षी/गवाही</h3><ul><li><strong>लेखा अधिकृत</strong> — भुक्तानी प्रक्रियामा अनियमितता भएको बयान।</li><li><strong>प्राविधिक विज्ञ</strong> — निर्माण गुणस्तर तोकिएको मापदण्डभन्दा कम रहेको प्राविधिक राय।</li></ul>", "confidence": "medium"}""",
    ),
    "gha": SectionSpec(
        key="gha",
        title="घ) अभियुक्तको बयान",
        heading="घ) अभियुक्तको बयान",
        max_tokens=1200,
        evidence_budget=12000,
        priority_source_types=(
            SourceType.LEGAL_PROCEDURAL,
            SourceType.OFFICIAL_GOVERNMENT,
            SourceType.MEDIA_NEWS,
        ),
        instructions="""TASK: Summarize the accused's statement or defense position.

STRUCTURE:
<h2>घ) अभियुक्तको बयान</h2>
<h3>मुख्य बयान</h3>
<p>Core statement or defense position of the accused in 1-2 paragraphs.</p>
<h3>बचाउका आधारहरू</h3>
<ul><li>Each defense argument or denial point</li></ul>

RULES:
- Report only what the evidence says the accused stated. Do not speculate.
- If the accused denies all charges, state that clearly and note any counter-narrative.
- Distinguish between the accused's assertions and what the prosecution evidence shows.
- If the accused refused to give a statement, note that explicitly.
- If multiple accused gave different statements, summarize each separately.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<h2>घ) अभियुक्तको बयान</h2><h3>मुख्य बयान</h3><p>अभियुक्त <strong>रमेश कुमार शर्मा</strong> ले आफू विरुद्धको सबै आरोप अस्वीकार गरेका छन्। उनले बयानमा भनेका छन् कि सबै निर्णय कानूनी प्रक्रिया अनुसार र सामूहिक निर्णय प्रक्रिया मार्फत भएको थियो। ठेक्का सम्झौता तत्कालीन प्रचलित बजार मूल्य अनुसार नै भएको र आफूले कुनै पनि व्यक्तिगत लाभ नलिएको दाबी उनले गरेका छन्।</p><h3>बचाउका आधारहरू</h3><ul><li>सबै निर्णय सामूहिक रूपमा मन्त्रालयको निर्णय प्रक्रिया अनुसार भएको।</li><li>ठेक्का मूल्य प्रचलित सरकारी दररेट अनुसार नै निर्धारण गरिएको।</li><li>गुणस्तरमा कमी भएको भए त्यसको जिम्मेवारी निर्माण कम्पनी र अनुगमन संयन्त्रको हुनुपर्ने।</li><li>आफूले कुनै व्यक्तिगत लाभ वा घुस नलिएको।</li></ul>", "confidence": "high"}""",
    ),
    "nga": SectionSpec(
        key="nga",
        title="ङ) विशेष अदालतको फैसला",
        heading="ङ) विशेष अदालतको फैसला",
        max_tokens=2000,
        evidence_budget=20000,
        priority_source_types=(
            SourceType.LEGAL_PROCEDURAL,
            SourceType.LEGAL_COURT_ORDER,
            SourceType.OFFICIAL_GOVERNMENT,
        ),
        instructions="""TASK: Summarize the Special Court's verdict and reasoning.

STRUCTURE:
<h2>ङ) विशेष अदालतको फैसला</h2>
<h3>फैसलाको सार</h3>
<p>Verdict summary — conviction, acquittal, or partial. State clearly for each charge and each accused.</p>
<h3>अदालतको तर्क</h3>
<p>Court's key reasoning in 1-2 paragraphs.</p>
<h3>सजाय निर्धारण</h3>
<p>Sentencing: imprisonment amount, fine amount, confiscation orders.</p>
<h3>फैसला मिति</h3>
<p>Judgment date and case number.</p>

RULES:
- State clearly whether each accused was convicted or acquitted per charge.
- Note if the Special Court verdict is under appeal.
- If certain charges were dropped or reduced, explain why.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<h2>ङ) विशेष अदालतको फैसला</h2><h3>फैसलाको सार</h3><p>विशेष अदालतले <strong>रमेश कुमार शर्मा</strong> लाई भ्रष्टाचार निवारण ऐन, २०५९ को दफा ३ र ८ अनुसारको कसुरमा <strong>दोषी ठहर</strong> गरेको छ। सह-अभियुक्त <strong>सुरेश बस्नेत</strong> लाई भने प्रमाण अभावमा सफाइ दिइएको छ।</p><h3>अदालतको तर्क</h3><p>अदालतले ठेक्का सम्झौता प्रक्रियामा अनियमितता भएको र अभियुक्त शर्माले पदीय हैसियतको दुरुपयोग गरेको ठहर गरेको छ। भुक्तानी भौचर र गुणस्तर परीक्षण प्रतिवेदनलाई प्रमुख प्रमाणको रूपमा स्वीकार गर्दै अदालतले अभियोजन पक्षले शंका रहित रूपमा आरोप प्रमाणित गर्न सफल भएको उल्लेख गरेको छ।</p><h3>सजाय निर्धारण</h3><p>अदालतले शर्मालाई निम्न सजाय सुनाएको छ:</p><ul><li><strong>कैद:</strong> ३ वर्ष</li><li><strong>जरिवाना:</strong> रु. ५,००,००,०००।— (पाँच करोड)</li><li><strong>जफत:</strong> शर्माको नाममा रहेको काठमाडौंको घरजग्गा जफत</li></ul><h3>फैसला मिति</h3><p>मिति २०८२ बैशाख ५ गते, विशेष अदालत काठमाडौं। मुद्दा नं. ०७९-CR-०४५६।</p>", "confidence": "high"}""",
    ),
    "cha": SectionSpec(
        key="cha",
        title="च) पुनरावेदन",
        heading="च) पुनरावेदन",
        max_tokens=1200,
        evidence_budget=12000,
        priority_source_types=(
            SourceType.LEGAL_PROCEDURAL,
            SourceType.LEGAL_COURT_ORDER,
            SourceType.OFFICIAL_GOVERNMENT,
        ),
        instructions="""TASK: Summarize the appeal proceedings and status.

STRUCTURE:
<h2>च) पुनरावेदन</h2>
<h3>पुनरावेदनको आधार</h3>
<p>Who appealed, on what grounds, and which court it was filed in.</p>
<h3>पुनरावेदन अदालतको निर्णय</h3>
<p>Appeal court decision, if rendered. Include whether the lower court verdict was upheld, modified, or overturned.</p>
<h3>वर्तमान स्थिति</h3>
<p>Current appeal status: pending, decided, further appeal filed.</p>

RULES:
- Note which party filed the appeal (convict or prosecution).
- If multiple appeals exist, list each separately.
- If the appeal is still pending, state that clearly and note when it was filed.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<h2>च) पुनरावेदन</h2><h3>पुनरावेदनको आधार</h3><p>अभियुक्त <strong>रमेश कुमार शर्मा</strong> ले विशेष अदालतको फैसला विरुद्ध <strong>उच्च अदालत पाटन</strong> मा पुनरावेदन दायर गरेका छन्। पुनरावेदनमा मुख्य आधारहरू: (१) विशेष अदालतले प्रमाण मूल्याङ्कनमा त्रुटि गरेको, (२) आफूले पदीय हैसियत दुरुपयोग नगरेको, र (३) सजाय अत्यधिक भएको दाबी गरिएको छ।</p><h3>पुनरावेदन अदालतको निर्णय</h3><p>उच्च अदालत पाटनले २०८२ मंसिर २० गते विशेष अदालतको फैसला <strong>सदर</strong> गरेको छ। अदालतले प्रमाणको पुनरावलोकन गर्दै विशेष अदालतको फैसला कानून सम्मत रहेको ठहर गरेको छ।</p><h3>वर्तमान स्थिति</h3><p>उच्च अदालतको फैसला विरुद्ध शर्माले <strong>सर्वोच्च अदालत</strong> मा पुनरावेदन दायर गरेका छन्। हाल उक्त पुनरावेदन सर्वोच्च अदालतमा <strong>विचाराधीन</strong> छ।</p>", "confidence": "high"}""",
    ),
    "chha": SectionSpec(
        key="chha",
        title="छ) सर्वोच्च अदालत",
        heading="छ) सर्वोच्च अदालत",
        max_tokens=2000,
        evidence_budget=20000,
        priority_source_types=(
            SourceType.LEGAL_COURT_ORDER,
            SourceType.LEGAL_PROCEDURAL,
            SourceType.OFFICIAL_GOVERNMENT,
        ),
        instructions="""TASK: Summarize Supreme Court proceedings and final disposition.

STRUCTURE:
<h2>छ) सर्वोच्च अदालत</h2>
<h3>सर्वोच्चमा पुग्ने आधार</h3>
<p>How and why the case reached Supreme Court. Which party appealed and on what grounds.</p>
<h3>सर्वोच्चको फैसला</h3>
<p>Supreme Court's final decision and reasoning in 1-2 paragraphs.</p>
<h3>अन्तिम स्थिति</h3>
<p>Final case status after Supreme Court ruling.</p>

RULES:
- This is the final appellate court; state whether its ruling is final and binding.
- Reference earlier verdicts (Special Court, High Court) for context.
- Include case citation number and bench composition if available.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<h2>छ) सर्वोच्च अदालत</h2><h3>सर्वोच्चमा पुग्ने आधार</h3><p>उच्च अदालत पाटनले विशेष अदालतको फैसला सदर गरेपछि अभियुक्त <strong>रमेश कुमार शर्मा</strong> ले सर्वोच्च अदालतमा पुनरावेदन दायर गरेका थिए। पुनरावेदनमा प्रमाण मूल्याङ्कनको मापदण्ड र सजाय निर्धारणको विधि सम्बन्धी कानूनी प्रश्न उठाइएको थियो।</p><h3>सर्वोच्चको फैसला</h3><p>सर्वोच्च अदालतका न्यायाधीशद्वय <strong>माननीय न्यायाधीश हरि फुयाँल</strong> र <strong>माननीय न्यायाधीश आनन्दमोहन भट्टराई</strong> को संयुक्त इजलासले २०८३ जेठ १२ गते शर्माको पुनरावेदन <strong>खारेज</strong> गर्दै तल्लो अदालतको फैसला सदर गरेको छ। सर्वोच्चले विशेष अदालत र उच्च अदालतले प्रमाणको समुचित मूल्याङ्कन गरेको र सजाय पनि कानून सम्मत रहेको ठहर गरेको छ।</p><h3>अन्तिम स्थिति</h3><p>सर्वोच्च अदालतको फैसला पश्चात यो मुद्दा <strong>अन्तिम रूपमा टुंगो</strong> लागेको छ। रमेश कुमार शर्मालाई ३ वर्ष कैद र रु. ५,००,००,०००।— जरिवानाको सजाय कायम रहेको छ। मुद्दा नं. ०८३-NT-०१२३। इजलास: माननीय न्यायाधीश हरि फुयाँल र माननीय न्यायाधीश आनन्दमोहन भट्टराई।</p>", "confidence": "high"}""",
    ),
    "ja": SectionSpec(
        key="ja",
        title="ज) अवलोकन",
        heading="ज) अवलोकन",
        max_tokens=800,
        evidence_budget=8000,
        priority_source_types=(
            SourceType.MEDIA_NEWS,
            SourceType.LEGAL_PROCEDURAL,
            SourceType.OFFICIAL_GOVERNMENT,
        ),
        instructions="""TASK: Provide analytical observations about the case.

STRUCTURE:
<h2>ज) अवलोकन</h2>
<h3>मुद्दाको महत्व</h3>
<p>Why this case matters — public interest, legal precedent, systemic issue.</p>
<h3>मुख्य टिप्पणी</h3>
<p>2-3 key takeaways or notable aspects of the case.</p>

RULES:
- Base observations on evidence presented in the case; avoid speculation about broader implications.
- Note any unusual procedural aspects or legal significance.
- Be concise and analytical, not editorial or opinion-based.
- If the case revealed systemic corruption patterns, note them factually.""",
        few_shot_example="""EXAMPLE OUTPUT:
{"html": "<h2>ज) अवलोकन</h2><h3>मुद्दाको महत्व</h3><p>यो मुद्दा उच्च पदस्थ सरकारी अधिकारीले पदीय हैसियत दुरुपयोग गरी सार्वजनिक खरिद प्रक्रियामा अनियमितता गरेको विषयमा केन्द्रित छ। ठूलो रकमको सार्वजनिक सम्पत्ति हानि भएको र यसले सार्वजनिक खरिद प्रणालीमा सुधारको आवश्यकता औंल्याएकोले यो मुद्दा महत्वपूर्ण मानिएको छ।</p><h3>मुख्य टिप्पणी</h3><p>(१) यो पहिलो मुद्दा हो जसमा विशेष अदालतदेखि सर्वोच्च अदालतसम्म तीनै तहबाट अभियुक्त दोषी ठहर भएको छ। (२) अदालतले प्रमाणको श्रृंखला (chain of evidence) को सिद्धान्त प्रयोग गरी निर्णय गरेको यो मुद्दामा उल्लेखनीय छ। (३) मुद्दाले सार्वजनिक खरिदमा हुने मिलेमतो रोक्न प्रणालीगत सुधारको आवश्यकतालाई उजागर गरेको छ।</p>", "confidence": "medium"}""",
    ),
}


COURT_STAGE_KEYS: tuple[str, ...] = ("gha", "nga", "cha", "chha", "ja")

CORE_SECTION_KEYS: tuple[str, ...] = ("short_description", "ka", "kha", "ga")

ALL_SECTION_KEYS: tuple[str, ...] = CORE_SECTION_KEYS + COURT_STAGE_KEYS


class CourtStage(str, Enum):
    CHARGE_SHEET = "charge_sheet"
    SPECIAL_COURT = "special_court"
    APPEAL = "appeal"
    SUPREME_COURT = "supreme_court"


COURT_IDENTIFIER_STAGE: dict[str, CourtStage] = {
    "special": CourtStage.SPECIAL_COURT,
    "supreme": CourtStage.SUPREME_COURT,
}

EVIDENCE_STAGE_KEYWORDS: dict[CourtStage, tuple[str, ...]] = {
    CourtStage.CHARGE_SHEET: (
        "बयान",
        "बक्तव्य",
        "कागज",
        "बयान गर",
        "statement",
        "बचाउ",
        "accused statement",
        "defense",
        "प्रतिवादीको बयान",
        "अभियुक्त",
    ),
    CourtStage.SPECIAL_COURT: (
        "विशेष अदालत",
        "special court",
        "विशेष अदालतको फैसला",
        "special court verdict",
        "special court judgment",
    ),
    CourtStage.APPEAL: (
        "पुनरावेदन",
        "appeal",
        "उच्च अदालत",
        "high court",
        "appellate",
        "पुनरावेदक",
    ),
    CourtStage.SUPREME_COURT: (
        "सर्वोच्च अदालत",
        "supreme court",
        "सर्वोच्च अदालतको फैसला",
        "supreme court verdict",
        "supreme court judgment",
    ),
}


@dataclass(frozen=True)
class SectionReadinessResult:
    key: str
    active: bool
    reason: str
    court_stage: CourtStage | None = None


@dataclass
class SectionReadinessCheck:
    court_cases: list[str] = field(default_factory=list)
    evidence_text: str = ""

    def check_section(self, key: str) -> SectionReadinessResult:
        spec = SECTION_SPECS.get(key)
        if spec is None:
            return SectionReadinessResult(key, False, f"unknown section key: {key}")
        if key in CORE_SECTION_KEYS:
            return SectionReadinessResult(key, True, "core section — always active")
        return self._check_court_stage_section(key)

    def _check_court_stage_section(self, key: str) -> SectionReadinessResult:
        court_stages = self._detect_court_stages()
        evidence = self._detect_stages_from_evidence()

        stage_map: dict[str, CourtStage] = {
            "gha": CourtStage.CHARGE_SHEET,
            "nga": CourtStage.SPECIAL_COURT,
            "cha": CourtStage.APPEAL,
            "chha": CourtStage.SUPREME_COURT,
        }

        if key == "ja":
            can_generate = len(court_stages | evidence) >= 2
            reason = (
                f"observation {'active' if can_generate else 'inactive'} — "
                f"{len(court_stages | evidence)} stages detected"
            )
            return SectionReadinessResult(key, can_generate, reason)

        required_stage = stage_map.get(key)
        if required_stage is None:
            return SectionReadinessResult(key, False, f"no stage mapping for {key}")

        stage_active = required_stage in (court_stages | evidence)
        reason_parts = []
        if required_stage in court_stages:
            reason_parts.append("court_cases field")
        if required_stage in evidence:
            reason_parts.append("evidence keyword match")
        reason = (
            f"{'active' if stage_active else 'inactive'} — {' + '.join(reason_parts)}"
            if reason_parts
            else f"no court_cases entry or evidence keyword match for {required_stage.value}"
        )
        return SectionReadinessResult(key, stage_active, reason, required_stage)

    def _detect_court_stages(self) -> set[CourtStage]:
        if not self.court_cases:
            return set()
        stages: set[CourtStage] = set()
        for entry in self.court_cases:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            identifier = entry.split(":", 1)[0].lower()
            stage = COURT_IDENTIFIER_STAGE.get(identifier)
            if stage is not None:
                stages.add(stage)
        if CourtStage.SPECIAL_COURT in stages:
            stages.add(CourtStage.CHARGE_SHEET)
        return stages

    def _detect_stages_from_evidence(self) -> set[CourtStage]:
        if not self.evidence_text:
            return set()
        corpus = self.evidence_text.lower()
        stages: set[CourtStage] = set()
        for stage, keywords in EVIDENCE_STAGE_KEYWORDS.items():
            if any(kw.lower() in corpus for kw in keywords):
                stages.add(stage)
        return stages

    def active_court_stage_keys(self) -> list[str]:
        return [k for k in COURT_STAGE_KEYS if self.check_section(k).active]

    def all_active_keys(self) -> list[str]:
        core = list(CORE_SECTION_KEYS)
        court = self.active_court_stage_keys()
        return core + court


def build_readiness_check(
    case: Case, evidence: list[SectionEvidence]
) -> SectionReadinessCheck:
    court_cases = case.court_cases if isinstance(case.court_cases, list) else []
    evidence_text = " ".join(e.text for e in evidence)
    return SectionReadinessCheck(court_cases=court_cases, evidence_text=evidence_text)


class SectionQualityError(ValueError):
    pass


def evidence_hash(evidence: list[SectionEvidence]) -> str:
    payload = [e.__dict__ for e in sorted(evidence, key=lambda item: item.source_id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prompt_hash(model: str, system_prompt: str, user_prompt: str) -> str:
    return hashlib.sha256(
        f"{model}\0{system_prompt}\0{user_prompt}".encode()
    ).hexdigest()


def validate_section_html(html: str, *, heading: str | None = None) -> None:
    if not html or not html.strip():
        raise SectionQualityError("section output is empty")

    # Check for markdown fence leakage (common LLM mistake)
    if html.strip().startswith("```") or html.strip().endswith("```"):
        raise SectionQualityError(
            "section output contains markdown code fences — LLM output formatting error"
        )

    parser = HTMLValidationParser()
    parser.feed(html)
    if parser.invalid_tags:
        raise SectionQualityError(
            f"disallowed HTML tags: {sorted(set(parser.invalid_tags))}"
        )
    if parser.stack:
        raise SectionQualityError(f"unclosed HTML tags: {parser.stack}")

    text = TAG_RE.sub("", html).strip()
    if not text:
        raise SectionQualityError(
            "section output contains no text after stripping HTML tags"
        )

    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        raise SectionQualityError("section output contains only whitespace")

    if len(chars) < 50:
        raise SectionQualityError(
            f"section output too short ({len(chars)} chars) — minimum 50 non-whitespace chars required"
        )

    nepali_ratio = sum(1 for ch in chars if DEVANAGARI_RE.match(ch)) / len(chars)
    if nepali_ratio < 0.15:
        raise SectionQualityError(
            f"section output does not contain enough Nepali text "
            f"(ratio: {nepali_ratio:.2f}, need ≥ 0.15)"
        )

    if heading and f"<h2>{heading}</h2>" not in html:
        raise SectionQualityError(f"section heading missing: {heading}")

    # Detect empty structural elements
    for tag in ("<ul></ul>", "<ol></ol>", "<table></table>"):
        if tag in html:
            raise SectionQualityError(
                f"empty {tag[1:tag.index('>')]} element in output"
            )

    # Detect placeholder patterns
    placeholder_patterns = [
        r"\[.*?(?:विवरण|जानकारी|थप|यहाँ).*?उपलब्ध.*?\]",
        r"\[.*?(?:details?|info|TBD|TODO|placeholder).*?\]",
    ]
    for pattern in placeholder_patterns:
        if re.search(pattern, html, re.IGNORECASE):
            raise SectionQualityError(
                "placeholder text detected — LLM failed to generate real content"
            )


def parse_llm_response(raw: str) -> tuple[str, str]:
    text = raw.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: extract JSON between first { and last }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start : end + 1])
        else:
            raise
    html = data["html"]
    confidence = data.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return html, confidence


def build_section_prompt(
    case: Case, spec: SectionSpec, evidence: list[SectionEvidence]
) -> str:
    evidence_chunks: list[str] = []
    remaining = spec.evidence_budget
    for item in prioritize_evidence(evidence, spec):
        if remaining <= 0:
            break
        text = item.text[:remaining]
        remaining -= len(text)
        evidence_chunks.append(
            f"SOURCE {item.source_id}\nTitle: {item.title}\nType: {item.source_type or 'untyped'}\n{text}"
        )

    case_context = {
        "case_id": case.case_id,
        "title": case.title,
        "short_description": case.short_description,
        "court_cases": case.court_cases,
        "bigo": case.bigo,
    }
    parts = [
        f"CASE CONTEXT:\n{json.dumps(case_context, ensure_ascii=False, default=str)}",
        f"SECTION INSTRUCTIONS:\n{spec.instructions}",
    ]
    if spec.few_shot_example:
        parts.append(f"FEW-SHOT EXAMPLE:\n{spec.few_shot_example}")
    parts.append(f"EVIDENCE:\n{'\n\n---\n\n'.join(evidence_chunks)}")
    return "\n\n".join(parts)


def prioritize_evidence(
    evidence: list[SectionEvidence], spec: SectionSpec
) -> list[SectionEvidence]:
    priority = {
        source_type: i for i, source_type in enumerate(spec.priority_source_types)
    }
    return sorted(evidence, key=lambda item: priority.get(item.source_type or "", 999))


class SectionGenerationService:
    def __init__(
        self, llm_client: SectionLLMClient, *, model: str = "claude-opus-4-7"
    ) -> None:
        self.llm_client = llm_client
        self.model = model

    async def generate_core_sections(
        self,
        case: Case,
        evidence: list[SectionEvidence],
        *,
        section_keys: tuple[str, ...] = ("short_description", "ka", "kha", "ga"),
    ) -> dict[str, SectionGenerationResult]:
        tasks = [
            self.generate_section(case, SECTION_SPECS[key], evidence)
            for key in section_keys
        ]
        results = await asyncio.gather(*tasks)
        return {result.key: result for result in results}

    async def generate_all_sections(
        self,
        case: Case,
        evidence: list[SectionEvidence],
        *,
        include_conditional: bool = True,
    ) -> dict[str, SectionGenerationResult]:
        readiness = build_readiness_check(case, evidence)
        keys = (
            readiness.all_active_keys()
            if include_conditional
            else list(CORE_SECTION_KEYS)
        )
        if include_conditional:
            skipped = [k for k in COURT_STAGE_KEYS if k not in keys]
            if skipped:
                logger.info("Skipping inactive sections: %s", skipped)
        tasks = [
            self.generate_section(case, SECTION_SPECS[key], evidence) for key in keys
        ]
        results = await asyncio.gather(*tasks)
        return {result.key: result for result in results}

    async def generate_section(
        self, case: Case, spec: SectionSpec, evidence: list[SectionEvidence]
    ) -> SectionGenerationResult:
        eh = evidence_hash(evidence)
        db_cache = (case.versionInfo or {}).get("section_generation_cache", {})
        cached = db_cache.get(spec.key)
        if (
            cached
            and cached.get("evidence_hash") == eh
            and cached.get("model") == self.model
        ):
            try:
                validate_section_html(cached["html"], heading=spec.heading)
            except SectionQualityError:
                logger.warning(
                    "Cached section %s failed validation, regenerating",
                    spec.key,
                )
            else:
                return SectionGenerationResult(
                    spec.key, cached["html"], cached["confidence"], True
                )

        user_prompt = build_section_prompt(case, spec, evidence)
        l4_key = f"llm:{prompt_hash(self.model, SYSTEM_PROMPT, user_prompt)}"
        raw = cache.get(l4_key)
        if raw is None:
            raw = await self.llm_client.generate(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=spec.max_tokens,
            )
            cache.set(l4_key, raw, timeout=24 * 60 * 60)

        html, confidence = parse_llm_response(raw)
        validate_section_html(html, heading=spec.heading)
        await self._store_section_cache(case, spec.key, eh, html, confidence)
        return SectionGenerationResult(spec.key, html, confidence, False)

    async def _store_section_cache(
        self, case: Case, key: str, eh: str, html: str, confidence: str
    ) -> None:
        version_info = dict(case.versionInfo or {})
        cache_data = dict(version_info.get("section_generation_cache", {}))
        cache_data[key] = {
            "model": self.model,
            "evidence_hash": eh,
            "html": html,
            "confidence": confidence,
        }
        version_info["section_generation_cache"] = cache_data
        case.versionInfo = version_info
        await sync_to_async(case.save)(update_fields=["versionInfo", "updated_at"])


def extract_case_evidence(
    case: Case, *, convert_documents: bool = False
) -> list[SectionEvidence]:
    source_ids = [
        item.get("source_id")
        for item in case.evidence or []
        if isinstance(item, dict) and item.get("source_id")
    ]
    sources = DocumentSource.objects.filter(
        source_id__in=source_ids, is_deleted=False
    ).prefetch_related("uploaded_files")
    evidence = []
    for source in sources:
        text_parts = [source.title, source.description or ""]
        for upload in source.uploaded_files.all():
            if upload.filename:
                text_parts.append(upload.filename)
            if upload.converted_text:
                text_parts.append(upload.converted_text)
            elif convert_documents and upload.file:
                try:
                    from cases.services.likhit_util import convert_bytes_to_markdown

                    content = upload.file.read()
                    result = convert_bytes_to_markdown(
                        content, filename=upload.filename or "source.bin"
                    )
                    text_parts.append(result.markdown)
                except Exception:
                    logger.warning(
                        "Failed to convert %s for case %s",
                        upload.filename,
                        case.case_id,
                        exc_info=True,
                    )
        evidence.append(
            SectionEvidence(
                source_id=source.source_id,
                title=source.title or "(untitled)",
                source_type=source.source_type,
                text="\n".join(part for part in text_parts if part),
            )
        )
    return evidence
