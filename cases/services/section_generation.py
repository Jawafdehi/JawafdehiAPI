from __future__ import annotations

import asyncio
import hashlib
import html.parser
import json
import logging
import re
from dataclasses import dataclass
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
    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float = 0.1,
        timeout: int = 180,
    ) -> str: ...


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
}


class SectionQualityError(ValueError):
    pass


def evidence_hash(evidence: list[SectionEvidence]) -> str:
    payload = [e.__dict__ for e in sorted(evidence, key=lambda item: item.source_id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prompt_hash(model: str, system_prompt: str, user_prompt: str) -> str:
    return hashlib.sha256(f"{model}\0{system_prompt}\0{user_prompt}".encode()).hexdigest()


class _TagAllowlistValidator(html.parser.HTMLParser):
    """Validates HTML contains only allowed tags with balanced nesting."""

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


def _html5lib_validate(html: str) -> tuple[bool, str]:
    """Validate HTML with html5lib parser. Returns (is_valid, error_message)."""
    try:
        import html5lib
        from html5lib.constants import DataLossWarning
        import warnings
    except ImportError:
        return False, "html5lib is not installed"

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DataLossWarning)
            parsed = html5lib.parseFragment(html, namespaceHTMLElements=False)
        _check_tree_tags(parsed)
        return True, ""
    except _HTML5LibTagError as e:
        return False, str(e)
    except Exception as e:
        return False, f"html5lib parse error: {e}"


class _HTML5LibTagError(Exception):
    pass


def _check_tree_tags(node) -> None:
    """Recursively check all tags in an html5lib tree against ALLOWED_TAGS."""
    if node is None:
        return
    if hasattr(node, "tag") and node.tag is not None:
        tag = node.tag.lower()
        # html5lib uses various tree builders; skip synthetic wrapper nodes
        if tag not in ALLOWED_TAGS and tag not in {
            "document_fragment",
            "#document-fragment",
            "#document",
            "docfragment",
        }:
            raise _HTML5LibTagError(f"disallowed HTML tag via html5lib: {tag}")
    # html5lib 1.1 uses xml.etree.ElementTree (iterable children via list()),
    # older versions use the DOM API (childNodes). Support both.
    children = getattr(node, "childNodes", None)
    if children is not None:
        for child in children:
            _check_tree_tags(child)
    else:
        for child in node:
            _check_tree_tags(child)


def validate_section_html(html: str, *, heading: str | None = None) -> None:
    if not html or not TAG_RE.sub("", html).strip():
        raise SectionQualityError("section output is empty")

    # Tag allowlist check (html.parser)
    parser = _TagAllowlistValidator()
    parser.feed(html)
    if parser.invalid_tags:
        raise SectionQualityError(f"disallowed HTML tags: {sorted(set(parser.invalid_tags))}")
    if parser.stack:
        raise SectionQualityError(f"unclosed HTML tags: {parser.stack}")

    # html5lib structural validation
    is_valid, err = _html5lib_validate(html)
    if not is_valid:
        raise SectionQualityError(err)

    # Nepali script detection: >=80% Devanagari characters (per plan v3)
    text = TAG_RE.sub("", html)
    chars = [ch for ch in text if not ch.isspace()]
    if chars:
        nepali_ratio = sum(1 for ch in chars if DEVANAGARI_RE.match(ch)) / len(chars)
        if nepali_ratio < 0.80:
            raise SectionQualityError(
                f"section output has insufficient Nepali text ({nepali_ratio:.1%} Devanagari, need ≥80%)"
            )

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
        if raw is not None:
            html, confidence = parse_llm_response(raw)
            validate_section_html(html, heading=spec.heading)
            await self._store_section_cache(case, spec.key, eh, html, confidence)
            return SectionGenerationResult(spec.key, html, confidence, False)

        raw = await self._call_llm_with_retry(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt, spec=spec)
        cache.set(l4_key, raw, timeout=24 * 60 * 60)

        html, confidence = parse_llm_response(raw)
        validate_section_html(html, heading=spec.heading)
        await self._store_section_cache(case, spec.key, eh, html, confidence)
        return SectionGenerationResult(spec.key, html, confidence, False)

    async def _call_llm_with_retry(
        self, *, system_prompt: str, user_prompt: str, spec: SectionSpec
    ) -> str:
        """Call LLM, retrying once on invalid JSON or quality failure."""
        last_raw = None
        last_error = None
        for attempt in range(2):
            try:
                last_raw = await self.llm_client.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=spec.max_tokens,
                    temperature=0.1,
                    timeout=180,
                )
                # Validate JSON is parseable
                json.loads(last_raw)
                # Validate HTML quality pre-cache-write
                html, confidence = parse_llm_response(last_raw)
                validate_section_html(html, heading=spec.heading)
                return last_raw
            except (json.JSONDecodeError, SectionQualityError) as e:
                last_error = e
                logger.warning(
                    "LLM section %s attempt %d failed (%s), %s",
                    spec.key,
                    attempt + 1,
                    type(e).__name__,
                    "retrying" if attempt == 0 else "giving up",
                )
        raise SectionQualityError(
            f"LLM generation failed for {spec.key} after 2 attempts: {last_error}"
        ) from last_error

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
    source_ids = [item.get("source_id") for item in case.evidence or [] if item.get("source_id")]
    sources = DocumentSource.objects.filter(
        source_id__in=source_ids, is_deleted=False
    ).prefetch_related("uploaded_files")
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
