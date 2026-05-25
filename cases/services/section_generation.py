from __future__ import annotations

import asyncio
import hashlib
import html.parser
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from asgiref.sync import sync_to_async

from cases.models import Case, DocumentSource, SourceType


DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
TAG_RE = re.compile(r"<[^>]+>")
ALLOWED_TAGS = {
    "h2", "h3", "p", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "strong", "em",
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
    quality_issues: list[str] = field(default_factory=list)


class SectionLLMClient(Protocol):
    async def generate(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> str: ...


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
- Write ENTIRELY in Nepali (देवनागरी script).
- Use simple, layman-friendly Nepali. Explain legal concepts in plain language.
- English allowed ONLY for: proper nouns, legal citation numbers, case numbers, dates.
- NEVER mix English and Nepali in the same sentence unless a proper noun requires it.

EVIDENCE RULES:
- Use ONLY the evidence provided. Do NOT fabricate or infer facts.
- If evidence is ambiguous, state what IS clear rather than guessing.
- When multiple sources conflict, prefer the most recent court decision.
- SUMMARIZE — do not copy-paste. Synthesis, not transcription.

FORMATTING RULES:
- Output valid HTML: <h2>, <h3>, <p>, <ul>, <ol>, <li>, <table>, <thead>, <tbody>, <tr>, <th>, <td>, <strong>, <em>.
- Use <h2> for section headings with Nepali numbering (क), ख), ग), ...).
- Use <h3> for sub-headings (per accused, per court stage, per evidence type).
- Use <table> for structured numeric data (amounts, dates, penalties).
- Use <ul>/<ol> for lists.
- NEVER output empty headings, tables, or placeholder text.
- If no content for a subsection, omit it entirely.

OUTPUT FORMAT:
Return valid JSON: {"html": "<h2>...</h2>...", "valid": true}
"""

SECTION_SPECS: dict[str, SectionSpec] = {
    "short_description": SectionSpec(
        key="short_description",
        title="Short description",
        heading=None,
        max_tokens=200,
        evidence_budget=5000,
        priority_source_types=(SourceType.OFFICIAL_GOVERNMENT, SourceType.MEDIA_NEWS),
        instructions="""TASK: Write 1-3 Nepali sentences summarizing this CIAA case.

INCLUDE: who is accused (name/position), core allegation, amount involved (if financial), current court stage (if known).
DO NOT INCLUDE: legal citations, detailed evidence, procedural history.

Evidence budget: 5,000 chars. Max output: 200 tokens. Temperature: 0.1.""",
    ),
    "ka": SectionSpec(
        key="ka",
        title="क) अभियोगपत्रको सार",
        heading="क) अभियोगपत्रको सार",
        max_tokens=1500,
        evidence_budget=15000,
        priority_source_types=(SourceType.OFFICIAL_GOVERNMENT,),
        instructions="""TASK: Summarize the CIAA charge sheet allegations in clear Nepali.

<h2>क) अभियोगपत्रको सार</h2>
<h3>मुख्य आरोप</h3><p>[Core allegation 1-2 paragraphs]</p>
<h3>संलग्न व्यक्तिहरू</h3><ul><li><strong>[Name]</strong> — [Position] — [Specific allegation]</li></ul>
<h3>आरोपित रकम र क्षति</h3><p>[Amount, calculation, loss]</p>
<h3>मुद्दा दायर मिति र अवस्था</h3><p>[Filing date, court, status]</p>

Evidence budget: 15,000 chars. Max output: 1,500 tokens. Priority sources: OFFICIAL_GOVERNMENT (chargesheet).""",
    ),
    "kha": SectionSpec(
        key="kha",
        title="ख) आकर्षित कानुनी व्यवस्था",
        heading="ख) आकर्षित कानुनी व्यवस्था",
        max_tokens=1000,
        evidence_budget=10000,
        priority_source_types=(SourceType.LEGAL_PROCEDURAL, SourceType.OFFICIAL_GOVERNMENT),
        instructions="""TASK: List and explain the legal provisions cited in the case.

<h2>ख) आकर्षित कानुनी व्यवस्था</h2>
<h3>[Provision name/number]</h3>
<p>[Simple layman explanation of what this prohibits]</p>
<p><strong>सजाय:</strong> [Penalty range]</p>

Explain each cited provision in simple Nepali. Do NOT list provisions not explicitly cited.
Evidence budget: 10,000 chars. Max output: 1,000 tokens.""",
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
        ),
        instructions="""TASK: Summarize prosecution evidence by type.

<h2>ग) प्रमाणको सार संक्षेप (अभियोजन पक्षले दाबी गरेको)</h2>
<h3>दस्तावेजी प्रमाण</h3><ul><li>[Doc type] — [What it shows]</li></ul>
<h3>आर्थिक प्रमाण</h3><p>[Use <table> for structured amounts]</p>
<h3>साक्षी/गवाही</h3><ul><li>[Witness role] — [Key testimony]</li></ul>
<h3>अन्य प्रमाण</h3><p>[Other evidence types]</p>

Group by TYPE, not source. Omit empty subsections.
Evidence budget: 15,000 chars. Max output: 1,500 tokens.""",
    ),
}


class SectionQualityError(ValueError):
    pass


def evidence_hash(evidence: list[SectionEvidence]) -> str:
    payload = [e.__dict__ for e in sorted(evidence, key=lambda item: item.source_id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def validate_section_html(html: str, *, heading: str | None = None) -> list[str]:
    """Validate section HTML. Returns list of quality issue descriptions."""
    issues: list[str] = []

    if not html or not TAG_RE.sub("", html).strip():
        issues.append("section output is empty")
        return issues

    parser = HTMLValidationParser()
    parser.feed(html)
    if parser.invalid_tags:
        issues.append(f"disallowed HTML tags: {sorted(set(parser.invalid_tags))}")
    if parser.stack:
        issues.append(f"unclosed HTML tags: {parser.stack}")

    text = TAG_RE.sub("", html)
    chars = [ch for ch in text if not ch.isspace()]
    if chars:
        nepali_ratio = sum(1 for ch in chars if DEVANAGARI_RE.match(ch)) / len(chars)
        if nepali_ratio < 0.80:
            issues.append(
                f"Nepali script ratio {nepali_ratio:.1%} below 80% threshold"
            )
    else:
        issues.append("no non-whitespace characters in output")

    if heading and f"<h2>{heading}</h2>" not in html:
        issues.append(f"section heading missing: {heading}")

    return issues


def parse_llm_response(raw: str) -> tuple[str, str]:
    data = json.loads(raw)
    html = data["html"]
    confidence = data.get("valid", False)
    if isinstance(confidence, bool):
        confidence = "high" if confidence else "low"
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
            f"SOURCE {item.source_id}\n"
            f"Title: {item.title}\n"
            f"Type: {item.source_type or 'untyped'}\n"
            f"{text}"
        )

    case_context = {
        "case_id": case.case_id,
        "title": case.title,
        "bigo": case.bigo,
        "court_cases": case.court_cases,
    }
    return (
        f"CASE CONTEXT:\n"
        f"{json.dumps(case_context, ensure_ascii=False, default=str)}\n\n"
        f"SECTION INSTRUCTIONS:\n"
        f"{spec.instructions}\n\n"
        f"EVIDENCE:\n"
        f"{chr(10).join('---' + chr(10) + chunk for chunk in evidence_chunks)}"
    )


def prioritize_evidence(
    evidence: list[SectionEvidence], spec: SectionSpec
) -> list[SectionEvidence]:
    priority = {
        source_type: i for i, source_type in enumerate(spec.priority_source_types)
    }
    return sorted(
        evidence, key=lambda item: priority.get(item.source_type or "", 999)
    )


class SectionGenerationService:
    def __init__(
        self, llm_client: SectionLLMClient, *, model: str = "claude-sonnet-4-6"
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
            issues = validate_section_html(cached["html"], heading=spec.heading)
            return SectionGenerationResult(
                spec.key, cached["html"], cached.get("confidence", "low"),
                from_cache=True, quality_issues=issues,
            )

        user_prompt = build_section_prompt(case, spec, evidence)
        raw = await self.llm_client.generate(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=spec.max_tokens,
        )

        html, confidence = parse_llm_response(raw)
        quality_issues = validate_section_html(html, heading=spec.heading)
        await self._store_section_cache(case, spec.key, eh, html, confidence)
        return SectionGenerationResult(
            spec.key, html, confidence, from_cache=False,
            quality_issues=quality_issues,
        )

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


def extract_case_evidence(case: Case) -> list[SectionEvidence]:
    source_ids = [
        item.get("source_id") for item in (case.evidence or []) if item.get("source_id")
    ]
    sources = DocumentSource.objects.filter(
        source_id__in=source_ids, is_deleted=False
    ).prefetch_related("uploaded_files")
    evidence: list[SectionEvidence] = []
    for source in sources:
        text_parts: list[str] = []
        if source.title:
            text_parts.append(source.title)
        if source.description:
            text_parts.append(source.description)
        for upload in source.uploaded_files.all():
            if upload.filename:
                text_parts.append(upload.filename)
        evidence.append(
            SectionEvidence(
                source_id=source.source_id,
                title=source.title or "",
                source_type=source.source_type,
                text="\n".join(text_parts),
            )
        )
    return evidence
