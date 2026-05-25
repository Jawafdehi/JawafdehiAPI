from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{3,}\b")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
NEPALI_DIGIT_RE = re.compile(r"[०-९]")
ASCII_NUMBERING_RE = re.compile(r"(?:^|\n)\s*(?:\d+\.|\d+\)|[A-Za-z]\.)\s+")
AMOUNT_RE = re.compile(r"(?:रु\.?|NPR|Rs\.?)\s*([०-९0-9,]+)|([०-९0-9,]+)\s*(?:रुपैयाँ|करोड|लाख)")
DATE_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|[०-९0-9]{4}[-/][०-९0-9]{1,2}[-/][०-९0-9]{1,2})\b")

EXPECTED_SECTION_ORDER = [
    "Case Metadata",
    "Entities",
    "Description",
    "Key Allegations",
    "Timeline",
    "Evidence / Sources",
    "Tags",
    "Missing Details",
    "Internal Notes",
    "Images",
]
CRITICAL_SECTIONS = [
    "Case Metadata",
    "Entities",
    "Description",
    "Key Allegations",
    "Timeline",
    "Evidence / Sources",
]
ALLOWED_ENGLISH = {
    "case",
    "metadata",
    "entities",
    "description",
    "key",
    "allegations",
    "timeline",
    "evidence",
    "sources",
    "tags",
    "missing",
    "details",
    "internal",
    "notes",
    "images",
    "type",
    "state",
    "title",
    "date",
    "bigo",
    "amount",
    "draft",
    "corruption",
    "review",
    "published",
    "ciaa",
    "special",
    "court",
    "nes",
    "id",
    "url",
    "html",
    "npr",
    "ad",
    "bs",
    "high",
    "compute",
    "infrastructure",
}


@dataclass(frozen=True)
class QualityFinding:
    check: str
    severity: str
    message: str
    section: str | None = None


class _FragmentParser(HTMLParser):
    VOID_TAGS = {"br", "hr", "img", "input", "meta", "link"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unmatched closing tag </{tag}>")
            return
        self.stack.pop()

    def close(self) -> None:
        super().close()
        if self.stack:
            self.errors.append("unclosed tag(s): " + ", ".join(reversed(self.stack)))


def evaluate_draft(draft_text: str, source_texts: dict[str, str] | None = None) -> dict[str, Any]:
    draft_text = unicodedata.normalize("NFC", draft_text or "")
    source_texts = source_texts or {}
    sections = _extract_sections(draft_text)
    findings: list[QualityFinding] = []

    findings.extend(_check_nepali_script(sections))
    findings.extend(_check_english_leakage(sections))
    findings.extend(_check_html_validity(sections))
    findings.extend(_check_no_empty_sections(sections))
    findings.extend(_check_cross_section_consistency(sections))
    findings.extend(_check_hallucination(draft_text, source_texts))
    findings.extend(_check_section_ordering(sections))
    findings.extend(_check_nepali_numbering(sections))
    findings.extend(_check_content_overlap(sections))
    findings.extend(_check_missing_critical_sections(sections))

    confidence = _section_confidence(sections, findings)
    route = _route(findings, confidence)

    return {
        "route": route,
        "overall_confidence": _overall_confidence(confidence),
        "section_confidence": confidence,
        "findings": [finding.__dict__ for finding in findings],
        "checks": {
            "nepali_script_presence": not any(f.check == "nepali_script_presence" for f in findings),
            "english_leakage": not any(f.check == "english_leakage" for f in findings),
            "html_validity": not any(f.check == "html_validity" for f in findings),
            "no_empty_sections": not any(f.check == "no_empty_sections" for f in findings),
            "cross_section_consistency": not any(f.check == "cross_section_consistency" for f in findings),
            "hallucination_detection": not any(f.check == "hallucination_detection" for f in findings),
            "section_ordering": not any(f.check == "section_ordering" for f in findings),
            "nepali_numbering": not any(f.check == "nepali_numbering" for f in findings),
            "content_overlap_detection": not any(f.check == "content_overlap_detection" for f in findings),
            "missing_critical_sections": not any(f.check == "missing_critical_sections" for f in findings),
        },
    }


def evaluate_case_dir(case_dir: Path) -> dict[str, Any]:
    draft_path = case_dir / "draft.md"
    source_dir = case_dir / "sources" / "markdown"
    source_texts = {
        path.name: path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(source_dir.glob("*.md"))
    } if source_dir.is_dir() else {}
    return evaluate_draft(draft_path.read_text(encoding="utf-8"), source_texts)


def write_quality_gate_report(case_dir: str) -> str:
    result = evaluate_case_dir(Path(case_dir))
    report_path = Path(case_dir) / "quality-gate.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"quality gate route={result['route']} confidence={result['overall_confidence']} report={report_path}"


def _extract_sections(text: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[title] = text[start:end].strip()
    return sections


def _check_nepali_script(sections: dict[str, str]) -> list[QualityFinding]:
    findings = []
    for section in ["Description", "Key Allegations", "Timeline", "Evidence / Sources"]:
        text = sections.get(section, "")
        chars = len(re.sub(r"\s+", "", text))
        nepali = len(DEVANAGARI_RE.findall(text))
        if chars >= 30 and nepali / max(chars, 1) < 0.25:
            findings.append(QualityFinding("nepali_script_presence", "hard_block", "Nepali content density is too low", section))
    return findings


def _check_english_leakage(sections: dict[str, str]) -> list[QualityFinding]:
    findings = []
    for section, text in sections.items():
        if section in {"Tags", "Internal Notes"}:
            continue
        words = [w.lower() for w in LATIN_WORD_RE.findall(text)]
        leaked = [w for w in words if w not in ALLOWED_ENGLISH]
        if len(leaked) >= 8:
            findings.append(QualityFinding("english_leakage", "soft_flag", f"High English leakage: {', '.join(sorted(set(leaked))[:8])}", section))
    return findings


def _check_html_validity(sections: dict[str, str]) -> list[QualityFinding]:
    description = sections.get("Description", "")
    parser = _FragmentParser()
    parser.feed(description)
    parser.close()
    return [QualityFinding("html_validity", "hard_block", "; ".join(parser.errors), "Description")] if parser.errors else []


def _check_no_empty_sections(sections: dict[str, str]) -> list[QualityFinding]:
    findings = []
    for section in CRITICAL_SECTIONS:
        body = sections.get(section, "")
        meaningful = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip(" -|\n\t")
        if not meaningful:
            findings.append(QualityFinding("no_empty_sections", "hard_block", "Critical section is empty", section))
    return findings


def _check_cross_section_consistency(sections: dict[str, str]) -> list[QualityFinding]:
    findings = []
    metadata = sections.get("Case Metadata", "")
    timeline = sections.get("Timeline", "")
    amounts = set(_normalise_num(m.group(1) or m.group(2)) for m in AMOUNT_RE.finditer(metadata))
    body_amounts = set(_normalise_num(m.group(1) or m.group(2)) for m in AMOUNT_RE.finditer(sections.get("Description", "") + sections.get("Key Allegations", "")))
    if amounts and body_amounts and amounts.isdisjoint(body_amounts):
        findings.append(QualityFinding("cross_section_consistency", "hard_block", "Bigo/amount values conflict across metadata and narrative"))
    metadata_dates = set(DATE_RE.findall(metadata))
    timeline_dates = set(DATE_RE.findall(timeline))
    if metadata_dates and timeline_dates and metadata_dates.isdisjoint(timeline_dates):
        findings.append(QualityFinding("cross_section_consistency", "soft_flag", "Metadata dates do not appear in timeline"))
    return findings


def _check_hallucination(draft_text: str, source_texts: dict[str, str]) -> list[QualityFinding]:
    if not source_texts:
        return [QualityFinding("hallucination_detection", "soft_flag", "No source markdown available for support check")]
    source_blob = "\n".join(source_texts.values())
    unsupported = []
    for value in sorted(set(DATE_RE.findall(draft_text))):
        if value not in source_blob:
            unsupported.append(value)
    for value in sorted(set(_normalise_num(m.group(1) or m.group(2)) for m in AMOUNT_RE.finditer(draft_text))):
        if value and value not in _normalise_num(source_blob):
            unsupported.append(value)
    if unsupported:
        return [QualityFinding("hallucination_detection", "hard_block", "Unsourced dates/amounts: " + ", ".join(unsupported[:10]))]
    return []


def _check_section_ordering(sections: dict[str, str]) -> list[QualityFinding]:
    present = [section for section in sections if section in EXPECTED_SECTION_ORDER]
    expected = [section for section in EXPECTED_SECTION_ORDER if section in sections]
    if present != expected:
        return [QualityFinding("section_ordering", "soft_flag", "Sections are out of template order")]
    return []


def _check_nepali_numbering(sections: dict[str, str]) -> list[QualityFinding]:
    findings = []
    for section in ["Description", "Key Allegations", "Timeline", "Evidence / Sources"]:
        text = sections.get(section, "")
        if ASCII_NUMBERING_RE.search(text) and not NEPALI_DIGIT_RE.search(text):
            findings.append(QualityFinding("nepali_numbering", "soft_flag", "ASCII numbering found without Nepali digits", section))
    return findings


def _check_content_overlap(sections: dict[str, str]) -> list[QualityFinding]:
    findings = []
    names = list(sections)
    for left_index, left in enumerate(names):
        if left in {"Case Metadata", "Tags", "Internal Notes", "Images"}:
            continue
        for right in names[left_index + 1:]:
            if right in {"Case Metadata", "Tags", "Internal Notes", "Images"}:
                continue
            ratio = SequenceMatcher(None, _compact(sections[left]), _compact(sections[right])).ratio()
            if ratio > 0.72 and min(len(sections[left]), len(sections[right])) > 120:
                findings.append(QualityFinding("content_overlap_detection", "soft_flag", f"High overlap with {right}", left))
    return findings


def _check_missing_critical_sections(sections: dict[str, str]) -> list[QualityFinding]:
    return [
        QualityFinding("missing_critical_sections", "hard_block", "Critical section missing", section)
        for section in CRITICAL_SECTIONS
        if section not in sections
    ]


def _section_confidence(sections: dict[str, str], findings: list[QualityFinding]) -> dict[str, str]:
    result = {}
    for section in sections:
        section_findings = [f for f in findings if f.section == section]
        if any(f.severity == "hard_block" for f in section_findings):
            result[section] = "low"
        elif section_findings:
            result[section] = "medium"
        else:
            body = re.sub(r"<!--.*?-->", "", sections[section], flags=re.DOTALL).strip()
            result[section] = "high" if len(body) >= 80 or section not in CRITICAL_SECTIONS else "medium"
    return result


def _overall_confidence(confidence: dict[str, str]) -> str:
    if any(value == "low" for value in confidence.values()):
        return "low"
    if any(value == "medium" for value in confidence.values()):
        return "medium"
    return "high"


def _route(findings: list[QualityFinding], confidence: dict[str, str]) -> str:
    if any(f.severity == "hard_block" for f in findings) or any(v == "low" for v in confidence.values()):
        return "hard_block"
    if findings or any(v == "medium" for v in confidence.values()):
        return "soft_flag"
    return "auto_publish"


def _normalise_num(value: str) -> str:
    table = str.maketrans("०१२३४५६७८९,", "0123456789 ")
    return "".join(value.translate(table).split())


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()
