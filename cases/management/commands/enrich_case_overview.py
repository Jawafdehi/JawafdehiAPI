"""
Enrich DRAFT CIAA cases with an LLM-generated case overview from evidence content.

Single management command (v3 plan): gathers evidence, routes to sections,
generates per-section HTML via LLM, concatenates in fixed क→ज order,
generates missing_details, and saves to the Case model.

Usage::

    python manage.py enrich_case_overview --dry-run
    python manage.py enrich_case_overview --limit 10
    python manage.py enrich_case_overview --case-id case-078-CR-0123 --verbose

Environment variables::

    JAWAFDEHI_LLM_API_KEY    — API key for Jawafdehi LLM proxy
    JAWAFDEHI_LLM_PROXY_URL  — base URL for Jawafdehi LLM proxy
"""

from __future__ import annotations

import hashlib
import html.parser
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from cases.models import Case, CaseState, DocumentSource, SourceType
from cases.services.priority_case_loader import filter_by_priority, load_priority_cases

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 16 * 1024
MAX_LLM_RETRIES = 3
DEFAULT_LLM_TIMEOUT = 300

_CLOUD_METADATA_IP = "169.254.169.254"  # NOSONAR — link-local, not configurable

_SSRF_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        _CLOUD_METADATA_IP,
        "metadata",
        "0.0.0.0",
    }
)

# ── Section specs (plan v3 §3) ──────────────────────────────────────────────

ALLOWED_HTML_TAGS = frozenset(
    {"h2", "h3", "p", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "strong", "em"}
)

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

KA_KHA_GA_ORDER: tuple[str, ...] = ("ka", "kha", "ga")
GHA_TO_JA_ORDER: tuple[str, ...] = ("gha", "nga", "cha", "chha", "ja")

CORE_SECTION_KEYS: tuple[str, ...] = ("short_description", "ka", "kha", "ga")
COURT_STAGE_KEYS: tuple[str, ...] = ("gha", "nga", "cha", "chha", "ja")
ALL_SECTION_KEYS: tuple[str, ...] = CORE_SECTION_KEYS + COURT_STAGE_KEYS

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
Return valid JSON: {"html": "<h2>...</h2>...", "confidence": "high|medium|low"}
"""

SECTION_SPECS: dict[str, dict] = {
    "short_description": {
        "key": "short_description",
        "heading": None,
        "max_tokens": 200,
        "evidence_budget": 5000,
        "instructions": (
            "TASK: Write 1-3 Nepali sentences summarizing this CIAA case.\n\n"
            "INCLUDE: who is accused (name/position), core allegation, amount involved (if financial), "
            "current court stage (if known).\n"
            "DO NOT INCLUDE: legal citations, detailed evidence, procedural history.\n"
            "Return only one <p> block in the html field."
        ),
    },
    "ka": {
        "key": "ka",
        "heading": "क) अभियोगपत्रको सार",
        "max_tokens": 1500,
        "evidence_budget": 15000,
        "instructions": (
            "TASK: Summarize the CIAA charge sheet allegations in clear Nepali.\n\n"
            "<h2>क) अभियोगपत्रको सार</h2>\n"
            "<h3>मुख्य आरोप</h3><p>[Core allegation 1-2 paragraphs]</p>\n"
            "<h3>संलग्न व्यक्तिहरू</h3><ul><li><strong>[Name]</strong> — [Position] — [Specific allegation]</li></ul>\n"
            "<h3>आरोपित रकम र क्षति</h3><p>[Amount, calculation, loss]</p>\n"
            "<h3>मुद्दा दायर मिति र अवस्था</h3><p>[Filing date, court, status]</p>"
        ),
    },
    "kha": {
        "key": "kha",
        "heading": "ख) आकर्षित कानुनी व्यवस्था",
        "max_tokens": 1000,
        "evidence_budget": 10000,
        "instructions": (
            "TASK: List and explain the legal provisions cited in the case.\n\n"
            "<h2>ख) आकर्षित कानुनी व्यवस्था</h2>\n"
            "<h3>[Provision name/number]</h3>\n"
            "<p>[Simple layman explanation of what this prohibits]</p>\n"
            "<p><strong>सजाय:</strong> [Penalty range]</p>\n\n"
            "Explain each cited provision in simple Nepali. Do NOT list provisions not explicitly cited."
        ),
    },
    "ga": {
        "key": "ga",
        "heading": "ग) प्रमाणको सार संक्षेप (अभियोजन पक्षले दाबी गरेको)",
        "max_tokens": 1500,
        "evidence_budget": 15000,
        "instructions": (
            "TASK: Summarize prosecution evidence by type.\n\n"
            "<h2>ग) प्रमाणको सार संक्षेप (अभियोजन पक्षले दाबी गरेको)</h2>\n"
            "<h3>दस्तावेजी प्रमाण</h3><ul><li>[Doc type] — [What it shows]</li></ul>\n"
            "<h3>आर्थिक प्रमाण</h3><p>[Use <table> for structured amounts]</p>\n"
            "<h3>साक्षी/गवाही</h3><ul><li>[Witness role] — [Key testimony]</li></ul>\n"
            "<h3>अन्य प्रमाण</h3><p>[Other evidence types]</p>\n\n"
            "Group by TYPE, not source. Omit empty subsections."
        ),
    },
    "gha": {
        "key": "gha",
        "heading": "घ) प्रतिवादीको बयान",
        "max_tokens": 1000,
        "evidence_budget": 10000,
        "instructions": (
            "TASK: Summarize the accused's official statements/defense.\n\n"
            "<h2>घ) प्रतिवादीको बयान</h2>\n"
            "<h3>[Accused Name]</h3>\n"
            "<p>[Defense/stance neutrally presented]</p>\n\n"
            "Present neutrally. Distinguish court-recorded vs CIAA interrogation vs public statements.\n"
            "SKIP entirely if no statement evidence exists. Do not write \"बयान उपलब्ध छैन\"."
        ),
    },
    "nga": {
        "key": "nga",
        "heading": "ङ) विशेष अदालतको फैसलाको सार",
        "max_tokens": 1500,
        "evidence_budget": 15000,
        "instructions": (
            "TASK: Summarize the Special Court verdict.\n\n"
            "<h2>ङ) विशेष अदालतको फैसलाको सार</h2>\n"
            "<h3>फैसला मिति र इजलास</h3><p>[Date, bench]</p>\n"
            "<h3>फैसलाको सार</h3><p>[Guilty/acquitted, which charges]</p>\n"
            "<h3>सजाय र क्षतिपूर्ति</h3>\n"
            "<table><thead><tr><th>प्रतिवादी</th><th>सजाय</th><th>जरिवाना/क्षतिपूर्ति</th></tr></thead>"
            "<tbody>...</tbody></table>\n"
            "<h3>अदालतको तर्क</h3><p>[Key reasoning, simplified]</p>\n\n"
            "SKIP if no Special Court verdict. Use <table> for multiple accused penalties."
        ),
    },
    "cha": {
        "key": "cha",
        "heading": "च) पुनरावेदनको सार",
        "max_tokens": 1000,
        "evidence_budget": 10000,
        "instructions": (
            "TASK: Summarize appeal proceedings if any.\n\n"
            "<h2>च) पुनरावेदनको सार</h2>\n"
            "<h3>पुनरावेदनकर्ता</h3><p>[Who appealed]</p>\n"
            "<h3>पुनरावेदनको आधार</h3><ul><li>[Grounds]</li></ul>\n"
            "<h3>उच्च अदालतको निर्णय</h3><p>[High Court decision]</p>\n\n"
            "SKIP if no appeal evidence."
        ),
    },
    "chha": {
        "key": "chha",
        "heading": "छ) सर्वोच्च अदालतको फैसलाको सार",
        "max_tokens": 1000,
        "evidence_budget": 10000,
        "instructions": (
            "TASK: Summarize Supreme Court decision if the case reached it.\n\n"
            "<h2>छ) सर्वोच्च अदालतको फैसलाको सार</h2>\n"
            "<h3>फैसला मिति</h3><p>[Date]</p>\n"
            "<h3>सर्वोच्चको निर्णय</h3><p>[Upheld/overturned, key reasoning]</p>\n"
            "<h3>अन्तिम परिणाम</h3><p>[Final outcome]</p>\n\n"
            "SKIP if no Supreme Court verdict."
        ),
    },
    "ja": {
        "key": "ja",
        "heading": "ज) नजरको सार",
        "max_tokens": 500,
        "evidence_budget": 5000,
        "instructions": (
            "TASK: Summarize any formal legal observation or precedent.\n\n"
            "<h2>ज) नजरको सार</h2>\n"
            "<p>[Key legal observation/precedent. What principle does this case establish?]</p>\n\n"
            "SKIP if no formal observation section in sources."
        ),
    },
}

# Evidence routing (plan v3 §2)
EVIDENCE_ROUTING: dict[str, list[str]] = {
    SourceType.OFFICIAL_GOVERNMENT: ["ka", "kha", "ga"],
    SourceType.LEGAL_PROCEDURAL: ["kha"],
    SourceType.FINANCIAL_FORENSIC: ["ga"],
    SourceType.INTERNAL_CORPORATE: ["ga"],
    SourceType.LEGAL_COURT_ORDER: ["nga", "cha", "chha", "gha", "ja"],
    SourceType.MEDIA_NEWS: ["ka", "ga"],
    SourceType.INVESTIGATIVE_REPORT: ["ka", "ga"],
    SourceType.PUBLIC_COMPLAINT: ["ka"],
}

EVIDENCE_PRIORITY: dict[str, int] = {
    SourceType.OFFICIAL_GOVERNMENT: 10,
    SourceType.LEGAL_COURT_ORDER: 10,
    SourceType.FINANCIAL_FORENSIC: 9,
    SourceType.LEGAL_PROCEDURAL: 8,
    SourceType.INTERNAL_CORPORATE: 7,
    SourceType.MEDIA_NEWS: 3,
    SourceType.INVESTIGATIVE_REPORT: 3,
    SourceType.PUBLIC_COMPLAINT: 2,
    None: 1,
}

# Court stage detection keywords
COURT_STAGE_KEYWORDS = {
    "gha": ("बयान", "बक्तव्य", "कागज", "statement", "बचाउ", "defense", "प्रतिवादीको बयान", "अभियुक्त"),
    "nga": ("विशेष अदालत", "special court", "विशेष अदालतको फैसला", "special court verdict"),
    "cha": ("पुनरावेदन", "appeal", "उच्च अदालत", "high court", "appellate", "पुनरावेदक"),
    "chha": ("सर्वोच्च अदालत", "supreme court", "सर्वोच्च अदालतको फैसला", "supreme court verdict"),
    "ja": (),
}

COURT_IDENTIFIER_MAP = {
    "special": "nga",
    "supreme": "chha",
}

# ── SSRF / download safety ──────────────────────────────────────────────────

def _validate_host_safety(hostname: str) -> None:
    host = hostname.lower().rstrip(".")
    if host in _SSRF_BLOCKED_HOSTNAMES:
        raise ValueError(
            f"Blocked internal host: {hostname!r}. "
            "Download sources must target public hosts only."
        )
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(
            f"Cannot resolve host: {hostname!r}. "
            "Only resolvable public hosts are allowed for source downloads."
        ) from exc
    for info in addrinfo:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            raise ValueError(
                f"Blocked internal address: {hostname!r} → {addr}. "
                "Download sources must target public IPs only."
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise urllib.error.HTTPError(
                req.full_url, code, f"Unsafe redirect scheme/host to {newurl}", headers, fp,
            )
        _validate_host_safety(parsed.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sanitize_download_filename(filename: str | None, source_id: str) -> str:
    raw = (filename or "").strip()
    if not raw:
        return f"{source_id}.bin"
    decoded = urllib.parse.unquote(raw)
    candidate = Path(decoded).name.strip()
    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"
    candidate = candidate.replace("\x00", "")
    candidate = re.sub(r"[<>:\"/\\|?*]+", "_", candidate).rstrip(" .")
    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"
    max_len = 200
    if len(candidate) <= max_len:
        return candidate
    suffix = "".join(Path(candidate).suffixes)
    stem = candidate[: -len(suffix)] if suffix else candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]
    stem_budget = max_len - len(suffix) - len(digest) - 1
    if stem_budget < 1:
        return f"{source_id}-{digest}{suffix}"[:max_len]
    truncated_stem = stem[:stem_budget].rstrip(" .-_")
    if not truncated_stem:
        truncated_stem = source_id
    return f"{truncated_stem}-{digest}{suffix}"


def _confined_output_path(output_dir: Path, filename: str) -> Path:
    output_dir_resolved = output_dir.resolve()
    out_path = (output_dir / filename).resolve()
    if output_dir_resolved not in out_path.parents:
        raise CommandError(f"Refusing to write outside output directory: '{filename}'")
    return out_path


def _copy_stream_to_path_with_limit(in_file: Any, out_path: Path) -> None:
    total_bytes = 0
    try:
        with out_path.open("wb") as out_file:
            while True:
                chunk = in_file.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_DOWNLOAD_BYTES:
                    raise CommandError(
                        f"Downloaded source exceeds max size of {MAX_DOWNLOAD_BYTES} bytes."
                    )
                out_file.write(chunk)
    except OSError:
        out_path.unlink(missing_ok=True)
        raise
    except CommandError:
        out_path.unlink(missing_ok=True)
        raise


# ── Quality checks (plan v3 §4) ─────────────────────────────────────────────

class HTMLValidator(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.invalid_tags: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in ALLOWED_HTML_TAGS:
            self.invalid_tags.append(tag)
        if tag not in {"br", "hr", "img", "meta", "link", "input"}:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_HTML_TAGS:
            self.invalid_tags.append(tag)
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


def _check_nepali_script(text: str) -> float:
    """Return fraction of non-whitespace characters that are Devanagari."""
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return 0.0
    return sum(1 for ch in chars if DEVANAGARI_RE.match(ch)) / len(chars)


def _validate_section_html(html: str, heading: str | None = None) -> list[str]:
    """Return list of quality warnings. Empty list = no issues."""
    warnings: list[str] = []
    text = re.sub(r"<[^>]+>", "", html)
    if not text.strip():
        warnings.append("empty section output")
        return warnings

    parser = HTMLValidator()
    parser.feed(html)
    if parser.invalid_tags:
        warnings.append(f"disallowed HTML tags: {sorted(set(parser.invalid_tags))}")
    if parser.stack:
        warnings.append(f"unclosed HTML tags: {parser.stack}")

    nepali_ratio = _check_nepali_script(text)
    if nepali_ratio < 0.20:
        warnings.append(f"low Nepali script ratio: {nepali_ratio:.1%}")

    if heading and f"<h2>{heading}</h2>" not in html:
        warnings.append(f"missing expected heading: {heading}")

    return warnings


def _parse_llm_response(raw_text: str) -> tuple[str, str]:
    """Parse JSON from LLM response, returning (html, confidence)."""
    data = json.loads(raw_text)
    html = data.get("html", "")
    confidence = data.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return html, confidence


# ── Evidence routing ────────────────────────────────────────────────────────

def _detect_source_type(source: DocumentSource) -> str | None:
    """Auto-detect source type from filename/title heuristics for null source_type."""
    corpus = " ".join(
        part for part in [source.title or "", source.description or "", source.uploaded_filename or ""]
        if part
    ).lower()

    order_keywords = ["order", "faisala", "आदेश", "फैसला", "निर्णय"]
    charge_keywords = ["charge", "arrest", "investigation", "अभियोग", "पक्राउ", "अनुसन्धान"]
    financial_keywords = ["bank", "audit", "financial", "बैंक", "लेखापरीक्षण", "आर्थिक"]

    if any(kw in corpus for kw in order_keywords):
        return SourceType.LEGAL_COURT_ORDER
    if any(kw in corpus for kw in charge_keywords):
        return SourceType.OFFICIAL_GOVERNMENT
    if any(kw in corpus for kw in financial_keywords):
        return SourceType.FINANCIAL_FORENSIC
    return None


@dataclass
class EvidenceItem:
    source_id: str
    text: str
    source_type: str | None
    title: str
    priority: int = 0


def _gather_evidence(case: Case, source_lookup: dict) -> list[EvidenceItem]:
    """Gather evidence items from a case, routing by source type."""
    items: list[EvidenceItem] = []
    seen_hashes: set[str] = set()

    for entry in (case.evidence or []):
        if not isinstance(entry, dict):
            continue
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            continue
        source = source_lookup.get(source_id)
        if source is None:
            continue

        text_parts = [source.title or ""]
        if source.description and len(source.description) > 200:
            text_parts.append(source.description)
        if (uploaded_filename := getattr(source, "uploaded_filename", None)):
            text_parts.append(uploaded_filename)

        text = "\n".join(p for p in text_parts if p)
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        source_type = source.source_type or _detect_source_type(source)
        priority = EVIDENCE_PRIORITY.get(source_type, 1)

        items.append(
            EvidenceItem(
                source_id=source_id,
                text=text,
                source_type=source_type,
                title=source.title or source_id,
                priority=priority,
            )
        )

    return sorted(items, key=lambda item: item.priority, reverse=True)


def _route_evidence_to_sections(
    evidence_items: list[EvidenceItem],
) -> dict[str, list[EvidenceItem]]:
    """Route evidence items to target sections per routing rules."""
    routed: dict[str, list[EvidenceItem]] = {key: [] for key in ALL_SECTION_KEYS}
    for item in evidence_items:
        target_sections = EVIDENCE_ROUTING.get(item.source_type or "", ["ka", "ga"])
        for section in target_sections:
            if section in routed:
                routed[section].append(item)
    # Always add top-priority evidence to short_description
    if evidence_items:
        routed["short_description"] = evidence_items[:3]
    return routed


def _build_section_prompt(
    case: Case,
    section_key: str,
    evidence: list[EvidenceItem],
) -> str:
    """Build the user prompt for a specific section."""
    spec = SECTION_SPECS[section_key]
    budget = spec["evidence_budget"]

    evidence_chunks: list[str] = []
    remaining = budget
    for item in evidence:
        if remaining <= 0:
            break
        chunk = item.text[:remaining]
        remaining -= len(chunk)
        evidence_chunks.append(
            f"SOURCE {item.source_id}\nTitle: {item.title}\nType: {item.source_type or 'untyped'}\n{chunk}"
        )

    case_context = {
        "case_id": case.case_id,
        "title": case.title,
        "court_cases": case.court_cases,
        "bigo": case.bigo,
    }
    return (
        f"CASE CONTEXT:\n{json.dumps(case_context, ensure_ascii=False, default=str)}\n\n"
        f"SECTION INSTRUCTIONS:\n{spec['instructions']}\n\n"
        f"EVIDENCE:\n{'\\n\\n---\\n\\n'.join(evidence_chunks)}"
    )


# ── Court stage detection ───────────────────────────────────────────────────

def _detect_court_stages(case: Case, all_evidence_text: str) -> dict[str, bool]:
    """Determine which court-stage sections are active for this case."""
    active: dict[str, bool] = {key: False for key in COURT_STAGE_KEYS}

    # Check court_cases field
    court_cases = case.court_cases or []
    if isinstance(court_cases, list):
        for entry in court_cases:
            if isinstance(entry, str) and ":" in entry:
                identifier = entry.split(":", 1)[0].lower()
                mapped = COURT_IDENTIFIER_MAP.get(identifier)
                if mapped:
                    active[mapped] = True
            # If special court detected, charge sheet stage active too
            if active.get("nga"):
                active["gha"] = True

    # Check evidence text for keyword matches
    corpus = all_evidence_text.lower()
    for key, keywords in COURT_STAGE_KEYWORDS.items():
        if active[key]:
            continue
        if any(kw.lower() in corpus for kw in keywords):
            active[key] = True

    # ja is active if 2+ other court stages are active
    active_count = sum(1 for k in COURT_STAGE_KEYS if k != "ja" and active[k])
    active["ja"] = active_count >= 2

    return active


# ── LLM call ─────────────────────────────────────────────────────────────────

def _call_llm(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
) -> str:
    """Call LLM via OpenCode-compatible proxy. Returns raw response text."""

    def _build_body():
        return json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.1,
            }
        )

    def _endpoint():
        prefix = "opencode-go/" if "opencode" in base_url else ""
        return f"{base_url.rstrip('/')}/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "JawafdehiAPI/1.0 enrich_case_overview",
    }

    body = _build_body()
    endpoint = _endpoint()

    last_status = None
    for attempt in range(1, MAX_LLM_RETRIES + 1):
        try:
            req = urllib.request.Request(endpoint, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            payload = json.loads(raw)
            # Extract message content from OpenAI-compatible response
            choices = payload.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""

        except urllib.error.HTTPError as e:
            last_status = e.code
            if attempt < MAX_LLM_RETRIES and last_status in (429, 503):
                wait = 2**attempt
                logger.warning("LLM %s on attempt %d, retrying in %ds...", last_status, attempt, wait)
                time.sleep(wait)
                continue
            try:
                body_snippet = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                body_snippet = "<unreadable>"
            raise CommandError(f"LLM HTTP {last_status}: {body_snippet}") from e

        except OSError as e:
            if attempt < MAX_LLM_RETRIES:
                wait = 2**attempt
                logger.warning("LLM connection error on attempt %d (%s), retrying in %ds...", attempt, e, wait)
                time.sleep(wait)
                continue
            raise CommandError(f"LLM connection failed after {MAX_LLM_RETRIES} attempts: {e}") from e

    raise CommandError(f"LLM call failed after {MAX_LLM_RETRIES} attempts")


# ── Main command ─────────────────────────────────────────────────────────────

@dataclass
class EnrichmentResult:
    section_key: str
    html: str
    confidence: str


class Command(BaseCommand):
    help = "Generate case overview from CIAA evidence content using LLM"

    def __init__(self):
        super().__init__()
        self.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_failed": 0,
            "cases_no_content": 0,
            "sections_generated": 0,
            "sections_skipped": 0,
        }
        self._source_lookup: dict = {}

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview without saving to database")
        parser.add_argument("--limit", type=int, default=None, help="Process only N cases")
        parser.add_argument("--case-id", type=str, default=None, help="Process a specific case by case_id")
        parser.add_argument("--priority", action="store_true", help="Enrich only priority case list cases")
        parser.add_argument("--all", action="store_true", dest="all_cases", help="Enrich all DRAFT CIAA cases")
        parser.add_argument("--verbose", action="store_true", help="Enable detailed debug logging")
        parser.add_argument("--force", action="store_true", help="Re-process cases with existing overview")
        parser.add_argument(
            "--llm-model",
            type=str,
            default=os.environ.get("JAWAFDEHI_OVERVIEW_MODEL", "claude-sonnet-4-6"),
            help="Model id (default: claude-sonnet-4-6)",
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=os.environ.get("JAWAFDEHI_LLM_PROXY_URL"),
            help="Base URL for LLM API",
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            default=os.environ.get("JAWAFDEHI_LLM_API_KEY"),
            help="API key for LLM proxy",
        )
        parser.add_argument(
            "--llm-timeout",
            type=int,
            default=int(os.environ.get("JAWAFDEHI_LLM_TIMEOUT_SECONDS", str(DEFAULT_LLM_TIMEOUT))),
            help=f"LLM request timeout in seconds (default: {DEFAULT_LLM_TIMEOUT})",
        )
        parser.add_argument(
            "--section-delay",
            type=float,
            default=0.5,
            help="Delay in seconds between section LLM calls (default: 0.5)",
        )
        parser.add_argument(
            "--case-delay",
            type=float,
            default=2.0,
            help="Delay in seconds between cases (default: 2.0)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        case_id = options.get("case_id")
        priority = options["priority"]
        verbose = options["verbose"]
        force = options["force"]

        if priority and case_id:
            raise CommandError("--priority and --case-id are mutually exclusive")
        if verbose:
            logger.setLevel(logging.DEBUG)
        if not options.get("llm_base_url"):
            raise CommandError(
                "LLM base URL is required. Set JAWAFDEHI_LLM_PROXY_URL env var or pass --llm-base-url."
            )
        if not options.get("llm_api_key"):
            raise CommandError(
                "LLM API key is required. Set JAWAFDEHI_LLM_API_KEY env var or pass --llm-api-key."
            )

        self.stdout.write(
            self.style.WARNING(f"{'[DRY RUN] ' if dry_run else ''}Starting case overview enrichment...")
        )

        cases = self._get_eligible_cases(limit, force, case_id, priority)
        self._fetch_source_cache(cases)
        self.stdout.write(f"Found {len(cases)} eligible CIAA DRAFT case(s) to process")

        start_time = time.time()

        for idx, case in enumerate(cases, 1):
            try:
                self.stdout.write(f"\n[{idx}/{len(cases)}] {case.case_id} - {case.title[:80]}...")
                self._process_case(case, dry_run, options)
            except Exception as e:
                self.stats["cases_failed"] += 1
                logger.exception(f"Error processing {case.case_id}: {e}")
                self.stdout.write(self.style.ERROR(f"FAILED: {case.case_id} - {e}"))
            if idx < len(cases):
                time.sleep(options["case_delay"])

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)
        elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        self._print_summary(dry_run, elapsed_str)

    # ── Query / source cache ────────────────────────────────────────────────

    def _get_eligible_cases(self, limit, force, case_id, priority=False):
        queryset = Case.objects.filter(state=CaseState.DRAFT)

        if case_id:
            queryset = queryset.filter(case_id=case_id)
        if priority:
            priority_list = load_priority_cases()
            queryset = filter_by_priority(queryset, priority_list)

        eligible = list(queryset)

        # Filter already-enriched (idempotent) unless --force
        if not force:
            eligible = [
                c
                for c in eligible
                if not (c.description and c.description.strip() and c.short_description and c.short_description.strip())
            ]

        if limit is not None:
            if limit < 0:
                raise CommandError(f"--limit must be >= 0, got {limit}")
            eligible = eligible[:limit] if limit > 0 else []

        return eligible

    def _fetch_source_cache(self, cases):
        source_ids = set()
        for case in cases:
            if not case.evidence or not isinstance(case.evidence, (list, tuple)):
                continue
            for entry in case.evidence:
                if not isinstance(entry, dict):
                    continue
                if isinstance((sid := entry.get("source_id")), str) and sid.strip():
                    source_ids.add(sid)

        self._source_lookup = {
            source.source_id: source
            for source in DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            ).prefetch_related("uploaded_files")
        }
        logger.debug(f"Cached {len(self._source_lookup)} DocumentSource records")

    # ── Per-case processing ─────────────────────────────────────────────────

    def _process_case(self, case: Case, dry_run: bool, options: dict) -> None:
        self.stats["cases_processed"] += 1

        # Gather evidence
        evidence_items = _gather_evidence(case, self._source_lookup)
        if not evidence_items:
            self.stats["cases_no_content"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No evidence found"))
            return

        # Convert evidence text via likhit if needed
        evidence_items = self._convert_evidence_text(evidence_items)
        if not evidence_items:
            self.stats["cases_no_content"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No content after conversion"))
            return

        self.stdout.write(f"  Gathered {len(evidence_items)} evidence item(s)")

        # Route evidence to sections
        routed = _route_evidence_to_sections(evidence_items)

        # Detect court stages
        all_text = " ".join(item.text for item in evidence_items)
        court_stages = _detect_court_stages(case, all_text)

        # Determine active sections
        active_sections = list(CORE_SECTION_KEYS)
        for key in COURT_STAGE_KEYS:
            if court_stages.get(key):
                active_sections.append(key)

        skipped_court = [k for k in COURT_STAGE_KEYS if not court_stages.get(k)]
        if skipped_court:
            self.stdout.write(f"  Skipped court-stage sections: {', '.join(skipped_court)}")

        # Generate sections via LLM
        results: dict[str, EnrichmentResult] = {}
        model = options["llm_model"]
        base_url = options["llm_base_url"]
        api_key = options["llm_api_key"]
        timeout = options["llm_timeout"]
        section_delay = options["section_delay"]

        for i, key in enumerate(active_sections):
            if i > 0 and section_delay > 0:
                time.sleep(section_delay)

            spec = SECTION_SPECS[key]
            section_evidence = routed.get(key, evidence_items[:2])

            if not section_evidence:
                self.stats["sections_skipped"] += 1
                continue

            try:
                user_prompt = _build_section_prompt(case, key, section_evidence)
                raw = _call_llm(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    max_tokens=spec["max_tokens"],
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    timeout=timeout,
                )
                html, confidence = _parse_llm_response(raw)
                warnings = _validate_section_html(html, spec.get("heading"))
                if warnings:
                    self.stdout.write(
                        self.style.WARNING(f"  Quality warnings for {key}: {', '.join(warnings)}")
                    )
                results[key] = EnrichmentResult(key, html, confidence)
                self.stats["sections_generated"] += 1
                self.stdout.write(f"  Generated {key} (confidence={confidence}, {len(html)} chars)")
            except Exception as e:
                self.stats["sections_skipped"] += 1
                self.stdout.write(self.style.WARNING(f"  SKIPPED section {key}: {e}"))

        if not results:
            self.stats["cases_skipped"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No sections generated"))
            return

        # ── Phase 4: Assembly & Save ────────────────────────────────────────

        # Concatenate sections in fixed क→ज order
        description_parts: list[str] = []
        for key in KA_KHA_GA_ORDER:
            if key in results:
                description_parts.append(results[key].html)
        for key in GHA_TO_JA_ORDER:
            if key in results:
                description_parts.append(results[key].html)
        full_description = "\n\n".join(description_parts) if description_parts else ""

        # Set short_description
        short_desc = results.get("short_description")
        short_description_html = short_desc.html if short_desc else ""

        # Build missing_details
        missing_details = self._build_missing_details(results, active_sections, court_stages)

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"  [DRY RUN] Would save: short_description={len(short_description_html)} chars, "
                    f"description={len(full_description)} chars ({len(description_parts)} sections), "
                    f"missing_details={len(missing_details or '')} chars"
                )
            )
            self.stats["cases_enriched"] += 1
            return

        # Save to Case model
        case.short_description = short_description_html
        case.description = full_description
        if missing_details:
            case.missing_details = missing_details
        case.save(update_fields=["short_description", "description", "missing_details", "updated_at"])

        self.stats["cases_enriched"] += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"  SAVED: {len(description_parts)} sections, "
                f"{len(missing_details or '')} missing_details chars"
            )
        )

    # ── Evidence text conversion ────────────────────────────────────────────

    def _convert_evidence_text(self, evidence_items: list[EvidenceItem]) -> list[EvidenceItem]:
        """Convert evidence text from document sources via markitdown/likhit where needed."""
        # For sources where description is short, download and convert the file
        converted = []
        for item in evidence_items:
            if len(item.text) >= 200:
                converted.append(item)
                continue
            source = self._source_lookup.get(item.source_id)
            if source is None:
                converted.append(item)
                continue
            try:
                markdown_text = self._convert_source_to_markdown(source)
                if markdown_text and len(markdown_text.strip()) >= 50:
                    converted.append(EvidenceItem(
                        source_id=item.source_id,
                        text=markdown_text,
                        source_type=item.source_type,
                        title=item.title,
                        priority=item.priority,
                    ))
                else:
                    converted.append(item)
            except Exception:
                converted.append(item)
        return converted

    def _convert_source_to_markdown(self, source: DocumentSource) -> str:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise CommandError("markitdown is required for overview enrichment.") from exc

        converter = MarkItDown(enable_plugins=True)
        with tempfile.TemporaryDirectory(prefix="overview-enrichment-") as tmp_dir:
            temp_path = self._download_source_to_path(source, Path(tmp_dir))
            if temp_path:
                result = converter.convert_uri(temp_path.resolve().as_uri())
                if result.text_content and len(result.text_content.strip()) >= 50:
                    return result.text_content

            ranked_urls = self._ranked_source_urls(source)
            for url in ranked_urls:
                try:
                    temp_path = self._download_url_to_path(url, source.source_id, Path(tmp_dir))
                    if not temp_path:
                        continue
                    result = converter.convert_uri(temp_path.resolve().as_uri())
                    if result.text_content and len(result.text_content.strip()) >= 50:
                        return result.text_content
                except (OSError, ValueError):
                    continue

            raise CommandError(f"Unable to convert source {source.source_id}")

    def _download_source_to_path(self, source: DocumentSource, output_dir: Path) -> Path | None:
        if source.uploaded_file:
            filename = _sanitize_download_filename(
                source.uploaded_filename or source.uploaded_file.name, source.source_id,
            )
            out_path = _confined_output_path(output_dir, filename)
            with source.uploaded_file.open("rb") as in_file:
                _copy_stream_to_path_with_limit(in_file, out_path)
            return out_path

        uploaded = source.uploaded_files.first()
        if uploaded and uploaded.file:
            filename = _sanitize_download_filename(uploaded.filename or uploaded.file.name, source.source_id)
            out_path = _confined_output_path(output_dir, filename)
            with uploaded.file.open("rb") as in_file:
                _copy_stream_to_path_with_limit(in_file, out_path)
            return out_path
        return None

    def _ranked_source_urls(self, source: DocumentSource) -> list[str]:
        urls = [url.strip() for url in (source.url or []) if isinstance(url, str) and url.strip()]
        if not urls:
            return []
        direct_urls = [u for u in urls if self._is_direct_document_url(u)]
        non_direct = [u for u in urls if u not in direct_urls]
        direct_urls.sort(key=lambda u: urllib.parse.urlparse(u).path.lower().endswith(".pdf"), reverse=True)
        return direct_urls + non_direct

    def _is_direct_document_url(self, url: str) -> bool:
        return urllib.parse.urlparse(url).path.lower().endswith((".pdf", ".doc", ".docx"))

    def _download_url_to_path(self, url: str, source_id: str, output_dir: Path) -> Path | None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        _validate_host_safety(parsed.hostname)
        out_path = _confined_output_path(output_dir, _sanitize_download_filename(parsed.path, source_id))
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                    )
                },
            )
            opener = urllib.request.build_opener(_SafeRedirectHandler())
            with opener.open(req, timeout=30) as response:
                _copy_stream_to_path_with_limit(response, out_path)
            return out_path
        except OSError:
            out_path.unlink(missing_ok=True)
            return None
        except CommandError:
            out_path.unlink(missing_ok=True)
            raise

    # ── missing_details generation ──────────────────────────────────────────

    def _build_missing_details(
        self,
        results: dict[str, EnrichmentResult],
        active_sections: list[str],
        court_stages: dict[str, bool],
    ) -> str:
        """Generate missing_details for skipped, empty, or low-confidence sections."""
        generated = set(results.keys())
        active_set = set(active_sections)
        skipped = active_set - generated

        lines: list[str] = []

        # Nepali section name mapping
        NEPALI_NAMES = {
            "ka": "क) अभियोगपत्रको सार",
            "kha": "ख) आकर्षित कानुनी व्यवस्था",
            "ga": "ग) प्रमाणको सार संक्षेप",
            "gha": "घ) प्रतिवादीको बयान",
            "nga": "ङ) विशेष अदालतको फैसला",
            "cha": "च) पुनरावेदनको सार",
            "chha": "छ) सर्वोच्च अदालतको फैसला",
            "ja": "ज) नजरको सार",
        }

        for key in sorted(skipped):
            name = NEPALI_NAMES.get(key, key)
            lines.append(f"{key} ({name}): section generation skipped or failed")

        for key, result in results.items():
            if result.confidence == "low":
                name = NEPALI_NAMES.get(key, key)
                lines.append(f"{key} ({name}): low confidence generation")

        # Note inactive court-stage sections
        for key in COURT_STAGE_KEYS:
            if key not in active_set and key not in generated:
                if not court_stages.get(key):
                    name = NEPALI_NAMES.get(key, key)
                    lines.append(f"{key} ({name}): no evidence for this court stage")

        return "\n".join(lines) if lines else ""

    # ── Summary output ──────────────────────────────────────────────────────

    def _print_summary(self, dry_run, elapsed_str):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.WARNING(f"{'[DRY RUN] ' if dry_run else ''}SUMMARY ({elapsed_str})"))
        self.stdout.write("=" * 60)
        self.stdout.write(f"Cases processed:   {self.stats['cases_processed']}")
        self.stdout.write(self.style.SUCCESS(f"Cases enriched:    {self.stats['cases_enriched']}"))
        self.stdout.write(f"Sections generated: {self.stats['sections_generated']}")
        self.stdout.write(f"Sections skipped:   {self.stats['sections_skipped']}")
        self.stdout.write(self.style.WARNING(f"Cases skipped:     {self.stats['cases_skipped']}"))
        self.stdout.write(self.style.WARNING(f"Cases no content:  {self.stats['cases_no_content']}"))
        if self.stats["cases_failed"] > 0:
            self.stdout.write(self.style.ERROR(f"Cases failed:      {self.stats['cases_failed']}"))
        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(self.style.WARNING("\nThis was a dry run. No changes were made to the database."))
            self.stdout.write("Run without --dry-run to apply changes.")
