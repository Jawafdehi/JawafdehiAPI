from __future__ import annotations

import asyncio
import hashlib
import html.parser
import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
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
    async def generate(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        ...


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


SYSTEM_PROMPT = """You are a senior Nepali legal analyst writing case overviews for JAWAFDEHI, a public legal transparency platform.

ROLE: Extract and summarize case information from CIAA evidence documents.

LANGUAGE RULES:
- Write entirely in Nepali Devanagari script.
- Use simple, layman-friendly Nepali.
- English is allowed only for proper nouns, legal citation numbers, case numbers, and ISO dates.

EVIDENCE RULES:
- Use only evidence provided in the prompt.
- Do not fabricate or infer facts.
- If evidence is ambiguous, state only what is clear.
- Prefer the most recent court decision when sources conflict.

FORMATTING RULES:
- Output valid HTML using only: <h2>, <h3>, <p>, <ul>, <ol>, <li>, <table>, <thead>, <tbody>, <tr>, <th>, <td>, <strong>, <em>.
- Use <h2> for section headings with Nepali letter numbering.
- Use <h3> for sub-headings.
- Do not output empty headings, empty tables, or placeholder text.

OUTPUT FORMAT:
Return valid JSON: {"html": "<h2>...</h2>", "confidence": "high|medium|low"}
"""

SECTION_SPECS: dict[str, SectionSpec] = {
    "short_description": SectionSpec(
        key="short_description",
        title="Short description",
        heading=None,
        max_tokens=200,
        evidence_budget=5000,
        priority_source_types=(SourceType.OFFICIAL_GOVERNMENT, SourceType.MEDIA_NEWS),
        instructions="""TASK: Write a 1-3 sentence summary of this CIAA case in Nepali.

INCLUDE:
- Who is accused: name, position, institution.
- Core allegation: corruption type or misconduct.
- Amount involved, if financial.
- Current court stage, if known.

DO NOT INCLUDE legal citations, detailed evidence discussion, or procedural history.
Return only one <p> block in the html field.""",
    ),
    "ka": SectionSpec(
        key="ka",
        title="क) अभियोगपत्रको सार",
        heading="क) अभियोगपत्रको सार",
        max_tokens=1500,
        evidence_budget=15000,
        priority_source_types=(SourceType.OFFICIAL_GOVERNMENT, SourceType.MEDIA_NEWS),
        instructions="""TASK: Summarize the CIAA charge sheet allegations in clear Nepali.

STRUCTURE:
<h2>क) अभियोगपत्रको सार</h2>
<h3>मुख्य आरोप</h3>
<p>Core allegation in 1-2 paragraphs.</p>
<h3>संलग्न व्यक्तिहरू</h3>
<ul><li><strong>Name</strong> — Position — Specific allegation</li></ul>
<h3>आरोपित रकम र क्षति</h3>
<p>Amount involved, how calculated, what was lost.</p>
<h3>मुद्दा दायर मिति र अवस्था</h3>
<p>Filing date, court, current status.</p>""",
    ),
    "kha": SectionSpec(
        key="kha",
        title="ख) आकर्षित कानुनी व्यवस्था",
        heading="ख) आकर्षित कानुनी व्यवस्था",
        max_tokens=1000,
        evidence_budget=10000,
        priority_source_types=(SourceType.LEGAL_PROCEDURAL, SourceType.OFFICIAL_GOVERNMENT),
        instructions="""TASK: List and explain the legal provisions cited in the case.

STRUCTURE:
<h2>ख) आकर्षित कानुनी व्यवस्था</h2>
<h3>Provision name/number</h3>
<p>Simple explanation of what this provision prohibits.</p>
<p><strong>सजाय:</strong> Penalty range if specified.</p>

RULES:
- Explain each cited provision in simple Nepali.
- Do not list provisions that are not explicitly cited in evidence.""",
    ),
    "ga": SectionSpec(
        key="ga",
        title="ग) प्रमाणको सार संक्षेप",
        heading="ग) प्रमाणको सार संक्षेप (अभियोजन पक्षले दाबी गरेको)",
        max_tokens=1500,
        evidence_budget=15000,
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
<ul><li>Document type — what it shows</li></ul>
<h3>आर्थिक प्रमाण</h3>
<p>Financial evidence summary. Use a table when amounts are structured.</p>
<h3>साक्षी/गवाही</h3>
<ul><li>Witness role — key testimony point</li></ul>
<h3>अन्य प्रमाण</h3>
<p>Other evidence types.</p>

RULES:
- Group evidence by type, not by source.
- Omit evidence-type subsections with no evidence.""",
    ),
    "gha": SectionSpec(
        key="gha",
        title="घ) अभियुक्तको बयान",
        heading="घ) अभियुक्तको बयान",
        max_tokens=1000,
        evidence_budget=10000,
        priority_source_types=(SourceType.LEGAL_PROCEDURAL, SourceType.OFFICIAL_GOVERNMENT, SourceType.MEDIA_NEWS),
        instructions="""TASK: Summarize the accused's statement or defense position.

STRUCTURE:
<h2>घ) अभियुक्तको बयान</h2>
<h3>मुख्य बयान</h3>
<p>Core statement or defense position of the accused.</p>
<h3>बचाउका आधारहरू</h3>
<ul><li>Defense argument or denial point</li></ul>

RULES:
- Only state facts from evidence; do not editorialize.
- If the accused denies all charges, state that clearly.
- Distinguish between what the accused asserts and what evidence shows.""",
    ),
    "nga": SectionSpec(
        key="nga",
        title="ङ) विशेष अदालतको फैसला",
        heading="ङ) विशेष अदालतको फैसला",
        max_tokens=1500,
        evidence_budget=15000,
        priority_source_types=(SourceType.LEGAL_PROCEDURAL, SourceType.LEGAL_COURT_ORDER, SourceType.OFFICIAL_GOVERNMENT),
        instructions="""TASK: Summarize the Special Court's verdict and reasoning.

STRUCTURE:
<h2>ङ) विशेष अदालतको फैसला</h2>
<h3>फैसलाको सार</h3>
<p>Verdict summary — conviction, acquittal, or partial.</p>
<h3>अदालतको तर्क</h3>
<p>Court's key reasoning in 1-2 paragraphs.</p>
<h3>सजाय निर्धारण</h3>
<p>Sentencing: imprisonment amount, fine amount, confiscation orders.</p>
<h3>फैसला मिति</h3>
<p>Judgment date and case number.</p>

RULES:
- State whether the accused was convicted or acquitted per charge.
- Note if the Special Court verdict is under appeal.""",
    ),
    "cha": SectionSpec(
        key="cha",
        title="च) पुनरावेदन",
        heading="च) पुनरावेदन",
        max_tokens=1200,
        evidence_budget=12000,
        priority_source_types=(SourceType.LEGAL_PROCEDURAL, SourceType.LEGAL_COURT_ORDER, SourceType.OFFICIAL_GOVERNMENT),
        instructions="""TASK: Summarize the appeal proceedings and status.

STRUCTURE:
<h2>च) पुनरावेदन</h2>
<h3>पुनरावेदनको आधार</h3>
<p>Who appealed, on what grounds.</p>
<h3>पुनरावेदन अदालतको निर्णय</h3>
<p>Appeal court decision, if rendered.</p>
<h3>वर्तमान स्थिति</h3>
<p>Current appeal status: pending, decided, further appeal filed.</p>

RULES:
- Note which party filed the appeal (convict or prosecution).
- If multiple appeals exist, list each separately.
- If the appeal is still pending, state that clearly.""",
    ),
    "chha": SectionSpec(
        key="chha",
        title="छ) सर्वोच्च अदालत",
        heading="छ) सर्वोच्च अदालत",
        max_tokens=1500,
        evidence_budget=15000,
        priority_source_types=(SourceType.LEGAL_COURT_ORDER, SourceType.LEGAL_PROCEDURAL, SourceType.OFFICIAL_GOVERNMENT),
        instructions="""TASK: Summarize Supreme Court proceedings and final disposition.

STRUCTURE:
<h2>छ) सर्वोच्च अदालत</h2>
<h3>सर्वोच्चमा पुग्ने आधार</h3>
<p>How and why the case reached Supreme Court.</p>
<h3>सर्वोच्चको फैसला</h3>
<p>Supreme Court's final decision and reasoning.</p>
<h3>अन्तिम स्थिति</h3>
<p>Final case status after Supreme Court ruling.</p>

RULES:
- This is the final appellate court; note if its ruling is final.
- Reference the Special Court verdict when comparing outcomes.
- Include case citation number if available.""",
    ),
    "ja": SectionSpec(
        key="ja",
        title="ज) अवलोकन",
        heading="ज) अवलोकन",
        max_tokens=800,
        evidence_budget=8000,
        priority_source_types=(SourceType.MEDIA_NEWS, SourceType.LEGAL_PROCEDURAL, SourceType.OFFICIAL_GOVERNMENT),
        instructions="""TASK: Provide analytical observations about the case.

STRUCTURE:
<h2>ज) अवलोकन</h2>
<h3>मुद्दाको महत्व</h3>
<p>Why this case matters — public interest, precedent, systemic issue.</p>
<h3>मुख्य टिप्पणी</h3>
<p>2-3 key takeaways or notable aspects of the case.</p>

RULES:
- Keep observations grounded in evidence; avoid speculation.
- Note any unusual procedural aspects or legal significance.
- Be concise and analytical, not editorial.""",
    ),
}


COURT_STAGE_KEYS: tuple[str, ...] = ("gha", "nga", "cha", "chha", "ja")

CORE_SECTION_KEYS: tuple[str, ...] = ("short_description", "ka", "kha", "ga")

ALL_SECTION_KEYS: tuple[str, ...] = CORE_SECTION_KEYS + COURT_STAGE_KEYS


class CourtStage(StrEnum):
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
        "बयान", "बक्तव्य", "कागज", "बयान गर", "statement", "बचाउ",
        "accused statement", "defense", "प्रतिवादीको बयान", "अभियुक्त",
    ),
    CourtStage.SPECIAL_COURT: (
        "विशेष अदालत", "special court", "विशेष अदालतको फैसला",
        "special court verdict", "special court judgment",
    ),
    CourtStage.APPEAL: (
        "पुनरावेदन", "appeal", "उच्च अदालत",
        "high court", "appellate", "पुनरावेदक",
    ),
    CourtStage.SUPREME_COURT: (
        "सर्वोच्च अदालत", "supreme court",
        "सर्वोच्च अदालतको फैसला", "supreme court verdict",
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


def build_readiness_check(case: Case, evidence: list[SectionEvidence]) -> SectionReadinessCheck:
    court_cases = case.court_cases if isinstance(case.court_cases, list) else []
    evidence_text = " ".join(e.text for e in evidence)
    return SectionReadinessCheck(court_cases=court_cases, evidence_text=evidence_text)


class SectionQualityError(ValueError):
    pass


def evidence_hash(evidence: list[SectionEvidence]) -> str:
    payload = [e.__dict__ for e in sorted(evidence, key=lambda item: item.source_id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prompt_hash(model: str, system_prompt: str, user_prompt: str) -> str:
    return hashlib.sha256(f"{model}\0{system_prompt}\0{user_prompt}".encode()).hexdigest()


def validate_section_html(html: str, *, heading: str | None = None) -> None:
    if not html or not TAG_RE.sub("", html).strip():
        raise SectionQualityError("section output is empty")

    parser = HTMLValidationParser()
    parser.feed(html)
    if parser.invalid_tags:
        raise SectionQualityError(f"disallowed HTML tags: {sorted(set(parser.invalid_tags))}")
    if parser.stack:
        raise SectionQualityError(f"unclosed HTML tags: {parser.stack}")

    text = TAG_RE.sub("", html)
    chars = [ch for ch in text if not ch.isspace()]
    if chars:
        nepali_ratio = sum(1 for ch in chars if DEVANAGARI_RE.match(ch)) / len(chars)
        if nepali_ratio < 0.20:
            raise SectionQualityError("section output does not contain enough Nepali text")

    if heading and f"<h2>{heading}</h2>" not in html:
        raise SectionQualityError(f"section heading missing: {heading}")


def parse_llm_response(raw: str) -> tuple[str, str]:
    data = json.loads(raw)
    html = data["html"]
    confidence = data.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return html, confidence


def build_section_prompt(case: Case, spec: SectionSpec, evidence: list[SectionEvidence]) -> str:
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
    return f"""CASE CONTEXT:
{json.dumps(case_context, ensure_ascii=False, default=str)}

SECTION INSTRUCTIONS:
{spec.instructions}

EVIDENCE:
{'\n\n---\n\n'.join(evidence_chunks)}
"""


def prioritize_evidence(evidence: list[SectionEvidence], spec: SectionSpec) -> list[SectionEvidence]:
    priority = {source_type: i for i, source_type in enumerate(spec.priority_source_types)}
    return sorted(evidence, key=lambda item: priority.get(item.source_type or "", 999))


class SectionGenerationService:
    def __init__(self, llm_client: SectionLLMClient, *, model: str = "claude-opus-4-7") -> None:
        self.llm_client = llm_client
        self.model = model

    async def generate_core_sections(
        self,
        case: Case,
        evidence: list[SectionEvidence],
        *,
        section_keys: tuple[str, ...] = ("short_description", "ka", "kha", "ga"),
    ) -> dict[str, SectionGenerationResult]:
        tasks = [self.generate_section(case, SECTION_SPECS[key], evidence) for key in section_keys]
        results = await asyncio.gather(*tasks)
        return {result.key: result for result in results}

    async def generate_all_sections(
        self,
        case: Case,
        evidence: list[SectionEvidence],
        *,
        include_conditional: bool = True,
        section_delay: float = 0.5,
    ) -> dict[str, SectionGenerationResult]:
        readiness = build_readiness_check(case, evidence)
        keys = readiness.all_active_keys() if include_conditional else list(CORE_SECTION_KEYS)
        if include_conditional:
            skipped = [k for k in COURT_STAGE_KEYS if k not in keys]
            if skipped:
                logger.info("Skipping inactive sections: %s", skipped)
        results: dict[str, SectionGenerationResult] = {}
        for i, key in enumerate(keys):
            if i > 0 and section_delay > 0:
                await asyncio.sleep(section_delay)
            result = await self.generate_section(case, SECTION_SPECS[key], evidence)
            results[result.key] = result
        return results

    async def generate_section(
        self, case: Case, spec: SectionSpec, evidence: list[SectionEvidence]
    ) -> SectionGenerationResult:
        eh = evidence_hash(evidence)
        db_cache = (case.versionInfo or {}).get("section_generation_cache", {})
        cached = db_cache.get(spec.key)
        if cached and cached.get("evidence_hash") == eh and cached.get("model") == self.model:
            validate_section_html(cached["html"], heading=spec.heading)
            return SectionGenerationResult(spec.key, cached["html"], cached["confidence"], True)

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
        self, case: Case, key: str, eh: str, html: str, confidence: str) -> None:
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


def extract_case_evidence(case: Case) -> list[SectionEvidence]:
    source_ids = [item.get("source_id") for item in case.evidence or [] if item.get("source_id")]
    sources = DocumentSource.objects.filter(source_id__in=source_ids, is_deleted=False).prefetch_related(
        "uploaded_files"
    )
    evidence = []
    for source in sources:
        text_parts = [source.title, source.description]
        for upload in source.uploaded_files.all():
            if upload.filename:
                text_parts.append(upload.filename)
        evidence.append(
            SectionEvidence(
                source_id=source.source_id,
                title=source.title,
                source_type=source.source_type,
                text="\n".join(part for part in text_parts if part),
            )
        )
    return evidence
