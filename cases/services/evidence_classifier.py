from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from cases.services.likhit_util import evidence_content_hash, idempotency_key


class EvidenceSection(StrEnum):
    OVERVIEW = "overview"
    KEY_ALLEGATIONS = "key_allegations"
    TIMELINE = "timeline"
    BIGO = "bigo"
    COURT_PROCEEDINGS = "court_proceedings"
    ENTITIES = "entities"
    SOURCE_DOCUMENTS = "source_documents"


@dataclass(frozen=True)
class EvidenceClassification:
    case_id: str
    content_hash: str
    idempotency_key: str
    sections: tuple[EvidenceSection, ...]
    source_type: str | None = None
    confidence: float = 0.0
    reasons: tuple[str, ...] = field(default_factory=tuple)


class EvidenceClassifier:
    SECTION_RULES = {
        EvidenceSection.BIGO: (
            "बिगो",
            "मागदाबी",
            "हानि",
            "नोक्सानी",
            "loss amount",
            "damage claim",
            "claim amount",
        ),
        EvidenceSection.KEY_ALLEGATIONS: (
            "आरोप",
            "कसुर",
            "भ्रष्टाचार",
            "अनियमितता",
            "allegation",
            "misconduct",
            "corruption",
            "irregularity",
        ),
        EvidenceSection.COURT_PROCEEDINGS: (
            "विशेष अदालत",
            "सर्वोच्च अदालत",
            "मुद्दा दायर",
            "फैसला",
            "court",
            "order",
            "judgment",
            "charge sheet",
        ),
        EvidenceSection.TIMELINE: (
            "मिति",
            "आ.व.",
            "आर्थिक वर्ष",
            "fiscal year",
            "dated",
            "between",
            "from",
            "to",
        ),
        EvidenceSection.ENTITIES: (
            "प्रतिवादी",
            "आरोपी",
            "अधिकृत",
            "employee",
            "officer",
            "defendant",
            "accused",
            "company",
            "firm",
        ),
    }

    TYPE_ROUTING = {
        "legal_procedural": (EvidenceSection.COURT_PROCEEDINGS,),
        "legal_court_order": (EvidenceSection.COURT_PROCEEDINGS,),
        "official_government": (EvidenceSection.KEY_ALLEGATIONS, EvidenceSection.BIGO),
        "news_article": (EvidenceSection.OVERVIEW, EvidenceSection.TIMELINE),
    }

    UNTYPED_HEURISTICS = (
        (re.compile(r"\bpress[-_ ]?release\b|\bciaa\b|विज्ञप्ति|अख्तियार", re.I), "press_release"),
        (re.compile(r"\bcourt\b|\border\b|\bjudgment\b|फैसला|अदालत", re.I), "court_order"),
        (re.compile(r"\bnews\b|\barticle\b|समाचार", re.I), "news_article"),
    )

    HEURISTIC_ROUTING = {
        "press_release": (
            EvidenceSection.KEY_ALLEGATIONS,
            EvidenceSection.BIGO,
            EvidenceSection.ENTITIES,
        ),
        "court_order": (EvidenceSection.COURT_PROCEEDINGS, EvidenceSection.TIMELINE),
        "news_article": (EvidenceSection.OVERVIEW, EvidenceSection.TIMELINE),
    }

    def classify(
        self,
        *,
        case_id: str,
        evidence_text: str,
        source_type: str | None = None,
        title: str = "",
        filename: str = "",
    ) -> EvidenceClassification:
        content_hash = evidence_content_hash(evidence_text)
        normalized_type = self._normalize_source_type(source_type)
        corpus = f"{title}\n{filename}\n{evidence_text}".lower()
        sections: list[EvidenceSection] = []
        reasons: list[str] = []

        for section in self.TYPE_ROUTING.get(normalized_type, ()):
            self._append(sections, section)
            reasons.append(f"source_type:{normalized_type}->{section.value}")

        inferred = None
        if not normalized_type:
            inferred = self._infer_untyped(corpus)
            for section in self.HEURISTIC_ROUTING.get(inferred, ()):
                self._append(sections, section)
                reasons.append(f"heuristic:{inferred}->{section.value}")

        for section, keywords in self.SECTION_RULES.items():
            if any(self._keyword_match(kw, corpus) for kw in keywords):
                self._append(sections, section)
                reasons.append(f"keyword:{section.value}")

        if not sections:
            sections.append(EvidenceSection.SOURCE_DOCUMENTS)
            reasons.append("default:source_documents")

        has_type_signal = bool(inferred or normalized_type in self.TYPE_ROUTING)
        confidence = self._confidence(sections, has_type_signal, reasons)
        return EvidenceClassification(
            case_id=case_id,
            content_hash=content_hash,
            idempotency_key=idempotency_key(case_id, content_hash),
            sections=tuple(sections),
            source_type=normalized_type or inferred,
            confidence=confidence,
            reasons=tuple(reasons),
        )

    def _normalize_source_type(self, source_type: str | None) -> str | None:
        if not source_type:
            return None
        return source_type.strip().lower().replace(" ", "_").replace("-", "_")

    def _infer_untyped(self, corpus: str) -> str | None:
        for pattern, inferred_type in self.UNTYPED_HEURISTICS:
            if pattern.search(corpus):
                return inferred_type
        return None

    def _append(self, sections: list[EvidenceSection], section: EvidenceSection) -> None:
        if section not in sections:
            sections.append(section)

    @staticmethod
    def _keyword_match(keyword: str, corpus: str) -> bool:
        kw_lower = keyword.lower()
        if kw_lower.isascii():
            return bool(re.search(r"\b" + re.escape(kw_lower) + r"\b", corpus))
        return kw_lower in corpus

    def _confidence(
        self, sections: list[EvidenceSection], has_type_signal: bool, reasons: list[str]
    ) -> float:
        score = 0.35
        if has_type_signal:
            score += 0.2
        score += min(0.35, 0.1 * len(reasons))
        if sections != [EvidenceSection.SOURCE_DOCUMENTS]:
            score += 0.1
        return min(score, 0.95)
