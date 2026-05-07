import re
from dataclasses import dataclass, field

KNOWLEDGE_RAG_KEYWORDS = [
    "annual report",
    "annual-report",
    "statistical report",
    "yearly report",
    "documentation",
    "document",
    "archive",
    "judicial data",
    "ngm",
    "evidence document",
    "source document",
    "process",
    "verify",
    "verification",
    "methodology",
    "faq",
    "policy",
    "report",
    "बार्षिक प्रतिवेदन",
    "वार्षिक प्रतिवेदन",
    "कागजात",
    "दस्तावेज",
    "प्रतिवेदन",
    "प्रक्रिया",
    "प्रमाण",
]

CASE_LIST_KEYWORDS = [
    "current published cases",
    "currently published cases",
    "published jawafdehi cases",
    "published cases",
    "list cases",
    "list published",
    "show cases",
    "show published",
    "recent cases",
    "latest cases",
    "all cases",
    "all published",
]

BS_YEAR_PATTERN = re.compile(r"\b(20[0-9]{2})(?:[/\-.।](?:[0-9]{2,4}))?\b")
CASE_URL_PATTERN = re.compile(
    r"/case/(?P<identifier>[A-Za-z0-9][A-Za-z0-9_-]{0,160})\b",
    flags=re.IGNORECASE,
)
CASE_SLUG_LABEL_PATTERN = re.compile(
    r"\bcase\s+slug\s*[:#]\s*(?P<identifier>[A-Za-z0-9][A-Za-z0-9_-]{0,160})\b",
    flags=re.IGNORECASE,
)
CASE_ID_PATTERN = re.compile(
    (
        r"\bcase(?:\s+id)?\b\s*[:#]?\s*"
        r"(?P<identifier>\d+|case[-_][A-Za-z0-9][A-Za-z0-9_-]{0,80})\b"
    ),
    flags=re.IGNORECASE,
)
CASE_LOOKUP_PREFIX_PATTERN = re.compile(
    r"^\s*case(?:\s+(?:id|slug))?(?:\s+|[:#]\s*)(?P<identifier>.+?)\s*$",
    flags=re.IGNORECASE,
)

COUNT_KEYWORDS = [
    "how many",
    "count",
    "total",
    "number of",
    "कति",
    "जम्मा",
    "संख्या",
]

ENTITY_KEYWORDS = [
    "entity",
    "person",
    "organization",
    "office",
    "ministry",
    "व्यक्ति",
    "संस्था",
    "कार्यालय",
]

STOP_PHRASES = [
    "how many",
    "number of",
    "count",
    "total",
    "cases",
    "case",
    "are published",
    "is published",
    "published",
    "current",
    "currently",
    "recent",
    "latest",
    "are there",
    "are ther",
    "ther",
    "were there",
    "registered",
    "show me",
    "what are",
    "what is",
    "in jawafdehi",
    "jawafdehi.org",
    "jawafdehi",
    "?",
]

PUBLIC_CHAT_MCP_TOOLS = frozenset(
    {
        "public_count_published_cases",
        "public_search_published_cases",
        "public_get_published_case",
        "public_search_jawaf_entities",
    }
)


@dataclass(frozen=True)
class QueryPlan:
    route: str
    retrieval_query: str
    reason: str
    tool_name: str | None = None
    case_identifier: str = ""
    filters: dict[str, str] = field(default_factory=dict)
    requires_document_citation: bool = False
    rag_skill_name: str = ""
    classifier_source: str = "deterministic"
    confidence: float | None = None
    classifier_error: str | None = None

    @property
    def search(self) -> str:
        """Compatibility alias while callers migrate to retrieval_query."""
        if self.route == "case_get":
            return self.case_identifier or self.retrieval_query
        return self.retrieval_query


RouteDecision = QueryPlan


def normalize_search(question: str) -> str:
    normalized = question.strip()
    lowered = normalized.lower()
    for phrase in STOP_PHRASES:
        lowered = lowered.replace(phrase, " ")
    return " ".join(lowered.split())


def extract_bs_year(question: str) -> str | None:
    match = BS_YEAR_PATTERN.search(question)
    return match.group(1) if match else None


def extract_case_identifier(question: str) -> str | None:
    for pattern in (CASE_URL_PATTERN, CASE_SLUG_LABEL_PATTERN, CASE_ID_PATTERN):
        match = pattern.search(question)
        if match:
            return match.group("identifier")
    return None


def normalize_case_lookup_identifier(value: str) -> str:
    """Strip conversational case prefixes without damaging slug-like ids."""
    normalized = value.strip()
    match = CASE_LOOKUP_PREFIX_PATTERN.match(normalized)
    if not match:
        return normalized

    identifier = match.group("identifier").strip()
    return identifier.removeprefix("#").strip() or normalized


def is_explicit_case_list_question(question: str) -> bool:
    lowered = question.lower()
    if any(keyword in lowered for keyword in COUNT_KEYWORDS):
        return False
    if any(keyword in lowered for keyword in ENTITY_KEYWORDS):
        return False
    if any(keyword in lowered for keyword in KNOWLEDGE_RAG_KEYWORDS):
        return False
    return any(keyword in lowered for keyword in CASE_LIST_KEYWORDS)


def route_question(question: str, *, default_to_case_search: bool = False) -> QueryPlan:
    lowered = question.lower()
    year = extract_bs_year(question)
    case_identifier = extract_case_identifier(question)
    normalized_search = normalize_search(question)

    if case_identifier and any(
        keyword in lowered
        for keyword in ["get", "show", "detail", "details", "case", "fetch"]
    ):
        return QueryPlan(
            "case_get",
            "",
            "case",
            "public_get_published_case",
            case_identifier=case_identifier,
            classifier_source="deterministic",
            confidence=0.9,
        )

    if any(keyword in lowered for keyword in KNOWLEDGE_RAG_KEYWORDS) or (
        year
        and "registered" in lowered
        and any(word in lowered for word in ["type", "kind"])
    ):
        return QueryPlan(
            "document_rag",
            normalized_search,
            "knowledge",
            requires_document_citation=True,
            classifier_source="deterministic",
            confidence=0.9,
        )

    if any(keyword in lowered for keyword in COUNT_KEYWORDS):
        return QueryPlan(
            "case_count",
            normalized_search,
            "count",
            "public_count_published_cases",
            classifier_source="deterministic",
            confidence=0.85,
        )

    if any(keyword in lowered for keyword in ENTITY_KEYWORDS):
        return QueryPlan(
            "entity_search",
            normalized_search,
            "entity",
            "public_search_jawaf_entities",
            classifier_source="deterministic",
            confidence=0.85,
        )

    if is_explicit_case_list_question(question):
        return QueryPlan(
            "case_list",
            "",
            "published_case_list",
            "public_search_published_cases",
            classifier_source="deterministic",
            confidence=0.9,
        )

    if not default_to_case_search:
        return QueryPlan(
            "clarify",
            normalized_search,
            "uncertain",
            classifier_source="deterministic",
            confidence=0.0,
        )

    return QueryPlan(
        "case_search",
        normalized_search,
        "case",
        "public_search_published_cases",
        classifier_source="deterministic",
        confidence=0.5,
    )
