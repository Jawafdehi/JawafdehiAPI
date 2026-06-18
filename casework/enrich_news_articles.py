#!/usr/bin/env python
"""Enrich CIAA Special Court cases with related news articles (DB-free script using the llm package).

Standalone script that searches the web for news articles about a CIAA corruption
case, LLM-verifies each candidate is about the same case, and stores accepted
articles as NEWS DocumentSources linked into the case's evidence — fully over the
Jawafdehi HTTP API. Never touches the database.

Phase 2d of the CIAA Case Enrichment pipeline. For each case it covers the case
lifecycle (investigation/filing/hearing/verdict/appeal), accepting at most one
article per event type up to --max-articles.

Usage:
    python casework/enrich_news_articles.py --dry-run
    python casework/enrich_news_articles.py --slug case-0123 --max-articles 3
    python casework/enrich_news_articles.py --court-case 081-CR-0121 --verbose
    python casework/enrich_news_articles.py --priority --limit 5 --provider bedrock
"""

import argparse
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

# Ensure the api dir is in sys.path so imports work when run as a file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from casework.common import (
    CaseworkApi,
    add_common_args,
    bootstrap,
    is_ciaa_special_court_case,
    matches_fiscal_year,
    print_summary,
    setup_logging,
    source_content,
)

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

_NEWS_SOURCE_TYPE = "NEWS"
_MAX_HTML_REGEX_LENGTH = 500_000

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JawafdehiAPI/1.0)",
}
_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JawafdehiAPI/1.0)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en,ne;q=0.9",
}

_ALLOWED_HOSTS = frozenset({"ciaa.gov.np", "ngm-store.jawafdehi.org"})

_OFFICIAL_PRESS_RELEASE_PATTERNS = (
    re.compile(
        r"^https?://(?:www\.)?ciaa\.gov\.np/(?:index\.php/)?pressrelease/",
        re.IGNORECASE,
    ),
)
_URL_BLOCKLIST_PATTERNS = (
    re.compile(r"/tag[/?]|/category[/?]|/author[/?]|/page/\d+", re.IGNORECASE),
)
_NON_NEWS_DOMAIN_PATTERNS = (
    re.compile(r"^https?://(?:[a-z-]+\.)?wikipedia\.org/", re.IGNORECASE),
    re.compile(r"^https?://(?:[a-z-]+\.)?facebook\.com/", re.IGNORECASE),
)

# Event types in a CIAA corruption case lifecycle
_EVENT_INVESTIGATION = "investigation"
_EVENT_FILING = "filing"
_EVENT_HEARING = "hearing"
_EVENT_VERDICT = "verdict"
_EVENT_APPEAL = "appeal"
_EVENT_OTHER = "other"

_ALL_EVENT_TYPES = (
    _EVENT_INVESTIGATION,
    _EVENT_FILING,
    _EVENT_HEARING,
    _EVENT_VERDICT,
    _EVENT_APPEAL,
)
_LIFECYCLE_EVENT_TYPES = frozenset(_ALL_EVENT_TYPES)
_EVENT_LIFECYCLE_ORDER = {
    _EVENT_INVESTIGATION: 1,
    _EVENT_FILING: 2,
    _EVENT_HEARING: 3,
    _EVENT_VERDICT: 4,
    _EVENT_APPEAL: 5,
    _EVENT_OTHER: 6,
}

_MAX_ARTICLES_PER_EVENT_TYPE = 1
_QUERY_LIMIT = 12
_QUERY_RESERVED_ENGLISH_SLOTS = 4
_QUERY_RESERVED_EVENT_SLOTS = 4

_EVENT_QUERY_TEMPLATES: dict[str, list[str]] = {
    _EVENT_INVESTIGATION: [
        "{name} CIAA investigation",
    ],
    _EVENT_FILING: [
        "{name} अख्तियार मुद्दा दायर",
        "{name} CIAA charge sheet special court",
    ],
    _EVENT_HEARING: [
        "{name} सुनुवाइ विशेष अदालत",
        "{name} hearing special court corruption",
    ],
    _EVENT_VERDICT: [
        "{name} फैसला विशेष अदालत",
        "{name} verdict special court corruption",
    ],
    _EVENT_APPEAL: [
        "{name} पुनरावेदन सर्वोच्च",
        "{name} supreme court appeal corruption",
        "{name} सर्वोच्च अदालत फैसला",
    ],
}

# Cheap-tier first-pass gate: a recall-biased filter that drops obviously
# off-topic candidates before the (pricier) premium verifier sees them. It only
# emits relevance — no event_type/summary/reason — to keep the cheap call small.
_GATE_SYSTEM_PROMPT = """\
You are a triage assistant for a Nepal corruption accountability platform. You are
given ONE CIAA Special Court corruption case and a NUMBERED LIST of candidate news
articles. For EACH candidate, decide whether it COULD plausibly be about the SAME
corruption case (same defendants/institution + corruption allegations).

This is a fast recall-oriented gate, not the final decision: when genuinely unsure,
return relevant=true so a stronger model can re-check. Only return relevant=false
when the candidate is clearly unrelated (different topic, navigation/boilerplate,
or a different case).

Respond with ONLY a JSON object with one result per candidate index:
{"results": [{"index": 0, "relevant": true}, {"index": 1, "relevant": false}]}
"""

# Premium batched verifier: the authoritative relevance decision. Folds the Nepali
# summary into the same call so accepted articles need no separate summary request.
_VERIFY_SYSTEM_PROMPT = """\
You are a fact-checking assistant for a Nepal corruption accountability platform. You
are given ONE CIAA Special Court corruption case and a NUMBERED LIST of candidate news
articles. For EACH candidate, decide whether it is genuinely about the SAME case.

Respond with ONLY a JSON object containing exactly one result per candidate index:
{"results": [
  {"index": 0, "relevant": true, "confidence": "high|medium|low", "reason": "<English>", "event_type": "investigation|filing|hearing|verdict|appeal|other", "summary": "<1-2 sentence Nepali (Devanagari) summary>"},
  {"index": 1, "relevant": false, "reason": "<English>"}
]}

Event types for the "event_type" field:
- "investigation" — CIAA is investigating or completed investigation phase
- "filing" — CIAA filed the charge sheet at Special Court
- "hearing" — court hearing, proceedings, or bench session coverage
- "verdict" — Special Court verdict, decision, or conviction
- "appeal" — Supreme Court appeal or review
- "other" — relevant article that does not clearly fit the above categories

Rules:
- The article must reference the same corruption case, not just mention the same person in an unrelated context.
- Matching on case number alone is strong evidence of relevance.
- Matching on defendant name + corruption allegations is medium evidence.
- If the article is about a different corruption case involving the same person, it is NOT relevant.
- If the article is about the same person but not about corruption allegations, it is NOT relevant.
- If a candidate excerpt is mostly navigation menus, category listings, or site boilerplate with only a headline and one sentence of real content, return relevant=false with reason "insufficient article content — likely paywalled or thin page".
- When relevant=true, BOTH "event_type" and "summary" are REQUIRED. event_type must never be an empty string (use "other" if unsure); summary must be in Nepali (Devanagari) and describe what the article reports.
- When relevant=false, omit event_type and summary.
"""

_ENGLISH_QUERY_SYSTEM_PROMPT = (
    "You are a Nepal-focused news search assistant. Output only clean search queries."
)


# ── HTML parsing / text extraction ───────────────────────────────────────────


def _truncate_for_regex(html: str) -> str:
    if len(html) > _MAX_HTML_REGEX_LENGTH:
        return html[:_MAX_HTML_REGEX_LENGTH]
    return html


class _TextExtractor(HTMLParser):
    """Extract visible text from HTML, skipping script/style tags."""

    def __init__(self):
        super().__init__()
        self.text_parts = []
        # Depth counter (not a bool) so nested skip tags — e.g. a <script>
        # inside a <nav> — don't re-enable extraction when the inner tag closes.
        self._skip_depth = 0
        self._skip_tags = {"script", "style", "noscript", "nav", "footer"}

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower in self._skip_tags:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag_lower in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.text_parts.append(text)


def _fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was decoded as Latin-1 then re-encoded.

    The pattern `à¤...` is the hallmark of Devanagari script that got
    Latin-1-mangled somewhere in the HTTP → HTML → Python pipeline.
    """
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def _extract_text_from_html(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        logger.exception("HTML parse error in _extract_text_from_html")
    text = " ".join(parser.text_parts)
    text = re.sub(r"\s+", " ", text).strip()
    return _fix_mojibake(text)


def _extract_title_from_html(html: str) -> str:
    safe_html = _truncate_for_regex(html)
    match = re.search(r"<title[^>]*>([^<]*)</title>", safe_html, re.IGNORECASE)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        return _fix_mojibake(title)
    return ""


def _parse_date_string(date_str: str) -> Optional[date]:
    date_str = date_str.strip()[:19]  # drops any trailing 'Z' / timezone
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def _extract_publication_date(html: str) -> Optional[date]:
    safe_html = _truncate_for_regex(html)
    patterns = [
        r'<meta[^>]+?property="article:published_time"[^>]+?content="([^"]+)"',
        r'<meta[^>]+?name="[^"]*date[^"]*"[^>]+?content="([^"]+)"',
        r'<meta[^>]+?itemprop="datePublished"[^>]+?content="([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, safe_html, re.IGNORECASE)
        if match:
            result = _parse_date_string(match.group(1))
            if result is not None:
                return result

    nepali_date_pattern = re.compile(
        r"(?:प्रकाशित|मिति)[:\s]*(\d{4})[-/](\d{1,2})[-/](\d{1,2})"
    )
    match = nepali_date_pattern.search(safe_html)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return None


# ── search ────────────────────────────────────────────────────────────────────


def _extract_ddg_redirect(url: str) -> str:
    """Extract the real URL from a DuckDuckGo redirect URL."""
    if "uddg=" in url:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        uddg = params.get("uddg", [""])[0]
        if uddg:
            return unquote(uddg)
    return url


def _search_duckduckgo(query: str, timeout: int = 15) -> list[dict]:
    """Search DuckDuckGo HTML; return list of {title, url, snippet} dicts.

    Retries with exponential backoff on 403/429 responses.
    """
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, headers=_HTTP_HEADERS, timeout=timeout)
            if resp.status_code in (403, 429):
                if attempt < max_attempts:
                    delay = 5 * 3 ** (attempt - 1)
                    logger.warning(
                        "DDG search attempt %d/%d: HTTP %d for '%s' — retrying in %ds",
                        attempt,
                        max_attempts,
                        resp.status_code,
                        query[:60],
                        delay,
                    )
                    time.sleep(delay)
                    continue
                logger.warning(
                    "DDG search failed after %d attempts for '%s'",
                    max_attempts,
                    query[:60],
                )
                return []
            resp.raise_for_status()
        except requests.RequestException as exc:
            # Transient network errors (timeouts, resets) should retry with the
            # same backoff as 403/429 — not abort the whole query on attempt 1.
            if attempt < max_attempts:
                delay = 5 * 3 ** (attempt - 1)
                logger.warning(
                    "DDG search attempt %d/%d failed: %s — retrying in %ds",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.warning(
                "DuckDuckGo search failed after %d attempts for '%s': %s",
                max_attempts,
                query[:60],
                exc,
            )
            return []
        break  # success → exit loop

    html = _truncate_for_regex(resp.text)
    results = []

    link_pattern = re.compile(
        r'<a[^>]{0,200}class="result__a"[^>]{0,100}href="([^"]{1,500})"[^>]{0,50}>'
        r"([^<]{1,500})</a>",
        re.IGNORECASE,
    )
    snippet_pattern = re.compile(
        r'<a[^>]{0,200}class="result__snippet"[^>]{0,100}>([^<]{1,1000})</a>',
        re.IGNORECASE,
    )
    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (href, title_html) in enumerate(links):
        result_url = _extract_ddg_redirect(href)
        title_text = re.sub(r"<[^>]+>", "", title_html).strip()
        snippet_text = ""
        if i < len(snippets):
            snippet_text = re.sub(r"<[^>]+>", "", snippets[i]).strip()
        if result_url and title_text:
            results.append(
                {"title": title_text, "url": result_url, "snippet": snippet_text}
            )
    return results[:8]


def _fetch_article_content(url: str, timeout: int = 20) -> Optional[str]:
    """Fetch article HTML content from a URL. Returns raw HTML or None on failure."""
    try:
        resp = requests.get(
            url, headers=_FETCH_HEADERS, timeout=timeout, allow_redirects=True
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").lower()
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return None
        return resp.text
    except requests.RequestException as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
        return None


def _is_official_press_release(url: str) -> bool:
    return any(p.search(url) for p in _OFFICIAL_PRESS_RELEASE_PATTERNS)


def _is_url_blocklisted(url: str) -> Optional[str]:
    for pattern in _URL_BLOCKLIST_PATTERNS:
        if pattern.search(url):
            return "tag/category/author page"
    for pattern in _NON_NEWS_DOMAIN_PATTERNS:
        if pattern.search(url):
            return "non-news domain (wikipedia/facebook)"
    return None


def _guess_outlet(url: str) -> str:
    try:
        hostname = urlparse(url).hostname or ""
        hostname = re.sub(r"^www\d*\.", "", hostname)
        parts = hostname.split(".")
        if len(parts) >= 2:
            return parts[-2].title()
        return hostname
    except Exception:  # noqa: BLE001
        return "Unknown"


# ── query generation ──────────────────────────────────────────────────────────

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

_ROMANIZATION_REPLACEMENTS = (
    ("क्ष", "ksh"),
    ("त्र", "tr"),
    ("ज्ञ", "gy"),
    ("श्र", "shr"),
    ("अ", "a"),
    ("आ", "aa"),
    ("इ", "i"),
    ("ई", "ee"),
    ("उ", "u"),
    ("ऊ", "oo"),
    ("ए", "e"),
    ("ऐ", "ai"),
    ("ओ", "o"),
    ("औ", "au"),
    ("क", "k"),
    ("ख", "kh"),
    ("ग", "g"),
    ("घ", "gh"),
    ("ङ", "n"),
    ("च", "ch"),
    ("छ", "chh"),
    ("ज", "j"),
    ("झ", "jh"),
    ("ञ", "n"),
    ("ट", "t"),
    ("ठ", "th"),
    ("ड", "d"),
    ("ढ", "dh"),
    ("ण", "n"),
    ("त", "t"),
    ("थ", "th"),
    ("द", "d"),
    ("ध", "dh"),
    ("न", "n"),
    ("प", "p"),
    ("फ", "ph"),
    ("ब", "b"),
    ("भ", "bh"),
    ("म", "m"),
    ("य", "y"),
    ("र", "r"),
    ("ल", "l"),
    ("व", "w"),
    ("श", "sh"),
    ("ष", "sh"),
    ("स", "s"),
    ("ह", "h"),
    ("ा", "a"),
    ("ि", "i"),
    ("ी", "i"),
    ("ु", "u"),
    ("ू", "u"),
    ("े", "e"),
    ("ै", "ai"),
    ("ो", "o"),
    ("ौ", "au"),
    ("ं", "n"),
    ("ँ", "n"),
    ("ः", ""),
    ("्", ""),
)


def _romanize_devanagari(text: str) -> str:
    romanized = text
    for devanagari, roman in _ROMANIZATION_REPLACEMENTS:
        romanized = romanized.replace(devanagari, roman)
    romanized = re.sub(r"[^A-Za-z0-9\s\"-]", " ", romanized)
    return re.sub(r"\s+", " ", romanized).strip()


def _is_english_query(query: str) -> bool:
    return not _DEVANAGARI_RE.search(query) and bool(re.search(r"[A-Za-z]", query))


def _query_has_nepal_keyword(query: str) -> bool:
    return bool(re.search(r"(?:\bNepal\b|नेपाल)", query, flags=re.IGNORECASE))


def _with_nepal_keyword(query: str) -> str:
    if _query_has_nepal_keyword(query):
        return query
    return f"{query} {'नेपाल' if _DEVANAGARI_RE.search(query) else 'Nepal'}"


def _normalize_search_queries(queries: list[str]) -> list[str]:
    normalized = [_with_nepal_keyword(query) for query in queries]
    english = [query for query in normalized if _is_english_query(query)]
    devanagari = [query for query in normalized if query not in english]
    return english[:4] + devanagari + english[4:]


def _deduplicate_queries(queries: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped


def _resolve_case_number(case: dict) -> Optional[str]:
    """Extract the first court case number from case['court_cases']."""
    for cc in case.get("court_cases") or []:
        if isinstance(cc, str) and ":" in cc:
            return cc.split(":", 1)[1]
    return None


def _get_accused_names(case: dict) -> list[str]:
    """Accused entity display names from the serialized case, with a title fallback.

    Reads case['entities'] (each {display_name, nes_id, type, ...}) for type=='accused';
    falls back to parsing the title when no accused relationships are present.
    """
    names = []
    for entity in case.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("type") != "accused":
            continue
        name_clean = (entity.get("display_name") or entity.get("nes_id") or "").strip()
        if name_clean:
            names.append(name_clean)
    if names:
        return names[:5]

    title = case.get("title") or ""
    if title:
        match = re.search(
            r"(?:विरुद्ध|vs\.?|versus)\s+(.{1,200})(?:\s+मुद्दा|\s+मा\.?\s|$)",
            title,
        )
        if match:
            rest = match.group(1).strip()
            if " र " in rest:
                names.extend(n.strip() for n in rest.split(" र "))
            elif "," in rest:
                names.extend(n.strip() for n in rest.split(","))
            else:
                names.append(rest)
    if not names and title:
        names.append(title[:80])
    return names[:5]


def _extract_org_name_from_title(title: str) -> str:
    if not title:
        return ""
    suffixes = (
        "सहकारी",
        "संस्था",
        "कम्पनी",
        "स्कुल",
        "कलेज",
        "अस्पताल",
        "बैंक",
        "विकास बैंक",
        "फाइनान्स",
        "जलस्रोत",
        "खानेपानी",
        "उपभोक्ता समिति",
        "विद्युत",
        "सिंचाइ",
        "निर्माण सेवा",
    )
    for suffix in sorted(suffixes, key=len, reverse=True):
        m = re.search(rf"(\S{{2,60}}\s*{re.escape(suffix)})", title)
        if m:
            return m.group(1).strip()
    return ""


def _extract_location_from_title(title: str) -> str:
    # In Nepali the place name PRECEDES the administrative division
    # (e.g. "काठमाडौं महानगरपालिका", "ललितपुर जिल्ला"), so try that order first.
    before = re.search(
        r"(\S{1,50})\s*(?:महानगरपालिका|उपमहानगरपालिका|नगरपालिका|गाउँपालिका|जिल्ला)",
        title,
    )
    if before:
        return before.group(1)
    # An office ("कार्यालय") often has the place AFTER it (e.g. "नापी कार्यालय चन्द्रगढी").
    after = re.search(r"कार्यालय\s+(\S{1,50})", title)
    if after:
        return after.group(1)
    loc_match2 = re.search(r"(\S{1,50})(?:को|का|मा)\s+(?:नापी|मालपोत|स्वास्थ्य)", title)
    if loc_match2:
        return loc_match2.group(1)
    return ""


def _extract_title_keywords(title: str) -> str:
    if not title:
        return ""
    parts = re.split(r"[,।\n]", title)
    if len(parts) > 1 and len(parts[0].strip()) > 10:
        return parts[0].strip()[:80]
    cleaned = re.sub(r"\b(?:मुद्दा|विरुद्ध|सम्बन्धी|सम्बन्धमा|मा\.?)\b", "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:100]


def _extract_corruption_keywords(key_allegations: list[str]) -> list[str]:
    corruption_terms = [
        "घुस",
        "रिश्वत",
        "भ्रष्टाचार",
        "अवैध सम्पत्ति",
        "हिनामिना",
        "पद दुरुपयोग",
        "किर्ते",
        "नक्कली",
        "बिगो",
        "अख्तियार",
        "विशेष अदालत",
        "bribery",
        "corruption",
        "illegal property",
        "embezzlement",
        "forgery",
        "abuse of authority",
    ]
    found = []
    text = " ".join(key_allegations).lower()
    for term in corruption_terms:
        if term.lower() in text:
            found.append(term)
    return found[:3]


def _append_english_name_queries(queries: list[str], accused_names: list[str]) -> None:
    name = accused_names[0] if accused_names else ""
    roman_name = _romanize_devanagari(name)
    if roman_name and len(roman_name) >= 3:
        queries.append(f"{roman_name} CIAA Nepal corruption")
        queries.append(f"{roman_name} Nepal special court case")


def _append_title_keyword_query(queries: list[str], title: str) -> None:
    title_keywords = _extract_title_keywords(title)
    if title_keywords:
        queries.append(f"{title_keywords} भ्रष्टाचार")


def _append_accused_corruption_queries(queries: list[str], case: dict) -> None:
    corruption_keywords = _extract_corruption_keywords(
        case.get("key_allegations") or []
    )
    for name in _get_accused_names(case)[:2]:
        name_clean = re.sub(r"\s+", " ", name).strip()
        if not name_clean or len(name_clean) < 3:
            continue
        for kw in corruption_keywords[:2]:
            queries.append(f"{name_clean} {kw} Nepal")


def _append_location_queries(queries: list[str], case: dict, title: str) -> None:
    accused_names = _get_accused_names(case)
    if not accused_names:
        return
    name_clean = re.sub(r"\s+", " ", accused_names[0]).strip()
    if not name_clean or len(name_clean) <= 3:
        return
    location = _extract_location_from_title(title)
    if location:
        queries.append(f"{name_clean} {location} भ्रष्टाचार")
        queries.append(f"{name_clean} {location} अख्तियार")


def _detect_case_events(case: dict) -> list[str]:
    """Return all five lifecycle event types for broad coverage.

    The LLM filters out irrelevant results, so casting wider is safe.
    """
    return [
        _EVENT_FILING,
        _EVENT_INVESTIGATION,
        _EVENT_HEARING,
        _EVENT_VERDICT,
        _EVENT_APPEAL,
    ]


def _append_event_targeted_queries(queries: list[str], case: dict) -> None:
    accused_names = _get_accused_names(case)
    if not accused_names:
        return
    name_clean = re.sub(r"\s+", " ", accused_names[0]).strip()
    if not name_clean or len(name_clean) < 3:
        return
    for event_type in _detect_case_events(case):
        for template in _EVENT_QUERY_TEMPLATES.get(event_type, []):
            queries.append(template.format(name=name_clean))


def _build_name_based_queries(case: dict) -> list[str]:
    queries = []
    accused_names = _get_accused_names(case)
    title = case.get("title") or ""
    location = _extract_location_from_title(title)
    org_name = _extract_org_name_from_title(title)

    for idx, name in enumerate(accused_names[:3]):
        name_clean = re.sub(r"\s+", " ", name).strip()
        if not name_clean or len(name_clean) < 3:
            continue
        if idx == 0:
            if location:
                queries.append(f'"{name_clean}" {location} भ्रष्टाचार')
                queries.append(f"{name_clean} {location} अख्तियार")
        else:
            queries.append(f"{name_clean} CIAA corruption Nepal")

    if org_name:
        queries.append(f"{org_name} भ्रष्टाचार")
        queries.append(f"{org_name} अख्तियार")
    return queries


def _generate_query_variations(
    case: dict, llm_english_queries: Optional[list[str]] = None
) -> list[str]:
    """Generate search query variations for a CIAA case.

    Prioritizes accused name + location + corruption keywords over case numbers,
    which mostly surface court/admin pages rather than newsrooms.
    """
    title = case.get("title") or ""
    accused_names = _get_accused_names(case)

    event_queries: list[str] = []
    _append_event_targeted_queries(event_queries, case)

    english_queries: list[str] = []
    if llm_english_queries:
        english_queries.extend(llm_english_queries)
    else:
        _append_english_name_queries(english_queries, accused_names)

    general_queries = _build_name_based_queries(case)
    _append_title_keyword_query(general_queries, title)
    _append_accused_corruption_queries(general_queries, case)
    _append_location_queries(general_queries, case, title)

    deduped_english = _deduplicate_queries(english_queries)
    deduped_events = _deduplicate_queries(event_queries)
    deduped_general = _deduplicate_queries(general_queries)

    reserved_english_slots = min(_QUERY_RESERVED_ENGLISH_SLOTS, len(deduped_english))
    reserved_event_slots = min(_QUERY_RESERVED_EVENT_SLOTS, len(deduped_events))
    general_slots = _QUERY_LIMIT - reserved_english_slots - reserved_event_slots

    combined = (
        deduped_english[:reserved_english_slots]
        + deduped_events[:reserved_event_slots]
        + deduped_general[:general_slots]
        + deduped_events[reserved_event_slots:]
        + deduped_english[reserved_english_slots:]
    )
    return _normalize_search_queries(_deduplicate_queries(combined))[:_QUERY_LIMIT]


# ── event-type inference ───────────────────────────────────────────────────────


def _infer_event_type_from_reason(
    reason: str, article_title: str, fallback_event_type: str
) -> str:
    """Extract a lifecycle event type from the LLM's reason text or article title."""
    combined = f"{reason} {article_title}".lower()
    event = fallback_event_type if fallback_event_type in _LIFECYCLE_EVENT_TYPES else ""
    if not event:
        rules = (
            (_EVENT_INVESTIGATION, r"investigat(?:ion|e|ing)", r"अनुसन्धान"),
            (_EVENT_FILING, r"charge.?sheet", r"filed|filing", r"मुद्दा दायर"),
            (_EVENT_HEARING, r"hearing|proceeding|bench", r"सुनुवाइ"),
            (
                _EVENT_VERDICT,
                r"verdict|convict|acquit|sentence|(?:found\s+)?guilty",
                r"फैसला",
                r"ठहर",
            ),
            (
                _EVENT_APPEAL,
                r"appeal|appellate|supreme court",
                r"पुनरावेदन",
                r"सर्वोच्च",
            ),
        )
        for event_type, *patterns in rules:
            for pat in patterns:
                if re.search(pat, combined):
                    event = event_type
                    break
            if event:
                break
    return event


# ── LLM helpers (via the llm package) ──────────────────────────────────────────


def _trim_excerpt(text: str, max_chars: int = 1500, devanagari_max: int = 1000) -> str:
    """Trim article excerpt with a shorter limit for Devanagari-dominant content."""
    if _DEVANAGARI_RE.search(text):
        return text[:devanagari_max]
    return text[:max_chars]


# ── existing-evidence bookkeeping (from the serialized case) ───────────────────


def _news_evidence_entries(case: dict) -> list[dict]:
    """Evidence entries whose linked source is a NEWS source."""
    entries = []
    for entry in case.get("evidence") or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if isinstance(source, dict) and source.get("source_type") == _NEWS_SOURCE_TYPE:
            entries.append(entry)
    return entries


def _evidence_source_urls(entry: dict) -> list[str]:
    """The link strings from an evidence entry's nested source.urls."""
    source = entry.get("source") or {}
    return [
        u["link"]
        for u in source.get("urls") or []
        if isinstance(u, dict) and u.get("link")
    ]


def _case_linked_news_urls(case: dict) -> set[str]:
    urls: set[str] = set()
    for entry in _news_evidence_entries(case):
        urls.update(_evidence_source_urls(entry))
    return urls


def _count_news_evidence(case: dict) -> int:
    return len(_news_evidence_entries(case))


def _existing_event_type_counts(case: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in _news_evidence_entries(case):
        event_type = entry.get("event_type")
        if event_type:
            counts[event_type] = counts.get(event_type, 0) + 1
    return counts


# ── enricher ────────────────────────────────────────────────────────────────


class NewsEnricher:
    """Search for, verify, and store news articles for one CIAA case at a time."""

    _CANDIDATE_BATCH_SIZE = 12
    _RETRY_MAX = 3

    def __init__(
        self,
        api: CaseworkApi,
        invoke_json,
        usage,
        max_articles_per_case: int = 5,
        search_delay: float = 1.5,
        fetch_delay: float = 0.5,
        verbose: bool = False,
        transcribe: bool = True,
        overwrite_markdown: bool = False,
    ):
        self.api = api
        self.invoke_json = invoke_json
        self.usage = usage
        self.max_articles_per_case = max_articles_per_case
        self.search_delay = search_delay
        self.fetch_delay = fetch_delay
        self.verbose = verbose
        self.transcribe = transcribe
        self.overwrite_markdown = overwrite_markdown

    # — LLM calls —

    def _llm_json(
        self, system: str, content: str, max_tokens: int, tier: str
    ) -> Optional[dict]:
        try:
            result = self.invoke_json(
                system=system,
                content=content,
                max_tokens=max_tokens,
                tier=tier,
                usage=self.usage,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLM call failed: %s", exc, exc_info=self.verbose)
            return None
        return result if isinstance(result, dict) else None

    def _generate_english_queries(
        self, case: dict, press_release_text: Optional[str]
    ) -> list[str]:
        """Use the LLM to generate correctly-romanized English search queries."""
        accused_names = _get_accused_names(case)
        name_list = ", ".join(accused_names[:3]) if accused_names else "Unknown"
        case_context = (
            f"Case Title: {case.get('title') or 'Unknown'}\nAccused: {name_list}"
        )
        user_prompt = (
            "Generate 5 English search queries to find Nepali news articles "
            "about this CIAA corruption case. One query per event type: "
            "investigation, chargesheet filing, court hearing, verdict, appeal. "
            "Use correct English romanization of Nepali names (e.g. Bahadur not wahadur). "
            "Include the word Nepal in each query. "
            'Respond with ONLY: {"queries": ["q1", "q2", "q3", "q4", "q5"]}\n\n'
            f"{case_context}"
        )
        result = self._llm_json(
            _ENGLISH_QUERY_SYSTEM_PROMPT, user_prompt, max_tokens=300, tier="cheap"
        )
        if not result:
            return []
        queries = result.get("queries")
        if not isinstance(queries, list):
            return []
        english = [
            q
            for q in queries
            if isinstance(q, str) and _is_english_query(q) and len(q) > 10
        ][:5]
        if english:
            logger.info(
                "  LLM generated %d English queries (e.g. %s)",
                len(english),
                english[0][:80],
            )
        return english

    @staticmethod
    def _build_case_context(case: dict, press_release_text: Optional[str]) -> str:
        """The shared case-context block prepended to every verify/gate prompt."""
        case_number = _resolve_case_number(case)
        key_allegations = case.get("key_allegations") or []
        ctx = (
            f"Case Title: {case.get('title') or 'Unknown'}\n"
            f"Court Case Number: {case_number or 'Unknown'}\n"
            f"Short Description: {case.get('short_description') or 'Not provided'}\n"
            f"Key Allegations: {', '.join(key_allegations[:5]) if key_allegations else 'None'}"
        )
        if press_release_text:
            ctx += (
                "\n\nPress Release Text (official CIAA document):\n"
                f"{press_release_text[:1200]}"
            )
        else:
            ctx += "\n\nNo official press release text available."
        return ctx

    def _verify_batch_call(
        self, system: str, case_context: str, items: list, tier: str
    ) -> dict:
        """Run ONE batched gate/verify call; return {candidate_index: verdict}.

        Each item is a fetch_result dict; its list position is the candidate index
        the model must echo back in each result object.
        """
        lines = []
        for i, fr in enumerate(items):
            excerpt = _trim_excerpt(
                (fr.get("article_text") or "").strip(),
                max_chars=900,
                devanagari_max=700,
            )
            lines.append(
                f"Candidate {i}:\n"
                f"Title: {fr.get('article_title') or ''}\n"
                f"URL: {fr['candidate']['url']}\n"
                f"Excerpt: {excerpt}"
            )
        user_prompt = (
            f"CASE CONTEXT:\n{case_context}\n\n"
            "CANDIDATES (return exactly one result per index):\n\n" + "\n\n".join(lines)
        )
        max_tokens = min(4000, 200 + 200 * len(items))
        result = self._llm_json(system, user_prompt, max_tokens, tier)
        verdicts: dict[int, dict] = {}
        if result and isinstance(result.get("results"), list):
            for r in result["results"]:
                if not isinstance(r, dict):
                    continue
                try:
                    idx = int(r.get("index"))
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(items):
                    verdicts[idx] = r
        return verdicts

    def _verify_batch(
        self, fetched: list, case: dict, press_release_text: Optional[str], stats: dict
    ) -> list[dict]:
        """Two-tier batched verification of one fetched batch.

        Cheap-tier gate drops clearly-irrelevant candidates in a single call; the
        premium tier then re-checks the survivors in a single call and returns the
        authoritative verdict plus the Nepali summary. Returns premium-confirmed
        article dicts; event-coverage selection happens in the caller.
        """
        items = [fr for fr in fetched if self._prefilter(fr, stats)]
        if not items:
            return []

        case_context = self._build_case_context(case, press_release_text)

        gate = self._verify_batch_call(
            _GATE_SYSTEM_PROMPT, case_context, items, "cheap"
        )
        if gate:
            survivor_pos = [
                i for i in range(len(items)) if gate.get(i, {}).get("relevant")
            ]
            stats["rejected"] += len(items) - len(survivor_pos)
        else:
            # Gate call/parse failed — escalate the whole batch to premium rather
            # than silently dropping everything on a transient cheap-model error.
            survivor_pos = list(range(len(items)))
        if not survivor_pos:
            return []

        survivors = [items[i] for i in survivor_pos]
        verdicts = self._verify_batch_call(
            _VERIFY_SYSTEM_PROMPT, case_context, survivors, "premium"
        )

        accepted = []
        for sub_idx, fr in enumerate(survivors):
            verdict = verdicts.get(sub_idx)
            url = fr["candidate"]["url"]
            if not verdict or not verdict.get("relevant"):
                stats["rejected"] += 1
                if self.verbose and verdict:
                    logger.info(
                        "  rejected: %s — %s", url[:80], verdict.get("reason", "")
                    )
                continue
            accepted.append(self._build_verified(fr, verdict))
        return accepted

    @staticmethod
    def _build_verified(fr: dict, verdict: dict) -> dict:
        """Assemble an accepted-article dict from a fetch result + premium verdict."""
        article_title = (fr.get("article_title") or "").strip()
        # Canonicalize the model's event_type: lowercase/trim and accept only
        # known lifecycle values, so non-canonical labels (casing, whitespace,
        # variants) can't bypass per-event caps/ordering or leak into the PATCH.
        raw_event_type = str(verdict.get("event_type") or "").strip().lower()
        event_type = raw_event_type if raw_event_type in _EVENT_LIFECYCLE_ORDER else ""
        if not event_type:
            inferred = _infer_event_type_from_reason(
                verdict.get("reason", ""), article_title, ""
            )
            event_type = inferred or _EVENT_OTHER
        return {
            "title": article_title,
            "url": fr["candidate"]["url"],
            "publication_date": fr.get("article_date"),
            "confidence": verdict.get("confidence", "medium"),
            "reason": verdict.get("reason", ""),
            "summary": (verdict.get("summary") or "").strip(),
            "event_type": event_type,
            "_article_text": (fr.get("article_text") or "").strip(),
        }

    # — fetch + verify —

    def _fetch_one(self, candidate: dict, stats: dict) -> Optional[dict]:
        url = candidate["url"]
        if _is_official_press_release(url):
            logger.debug("  skipped: official CIAA press release — %s", url[:80])
            return None
        blocklist_reason = _is_url_blocklisted(url)
        if blocklist_reason:
            logger.debug("  skipped: %s — %s", blocklist_reason, url[:80])
            return None
        try:
            html = _fetch_article_content(url)
            if not html:
                logger.debug("  Failed to fetch content: %s", url)
                return None
            article_text = _extract_text_from_html(html)
            if len(article_text) < 100:
                logger.debug(
                    "  Insufficient content from %s (%d chars)", url, len(article_text)
                )
                return None
            article_title = _extract_title_from_html(html) or candidate.get("title", "")
            article_date = _extract_publication_date(html)
            stats["fetched"] += 1
            return {
                "candidate": candidate,
                "article_text": article_text,
                "article_title": article_title,
                "article_date": article_date,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "  fetch error: %s — %s: %s", type(exc).__name__, url[:80], exc
            )
            stats["errors"] += 1
            return None
        finally:
            # Always pace requests — even on early return / error — to stay polite.
            if self.fetch_delay > 0:
                time.sleep(self.fetch_delay)

    _PAYWALL_KEYWORDS = frozenset(
        {"ciaa", "corruption", "akhtiyar", "अख्तियार", "भ्रष्टाचार"}
    )
    _NOT_FOUND_SIGNALS = (
        "does not exist",
        "page not found",
        "article not found",
        "content not found",
        "no longer available",
        "nothing was found",
        "could not be found",
    )

    def _is_thin_or_missing(
        self, article_body: str, article_title: str, url: str, stats: dict
    ) -> bool:
        """Reject paywall/redirect/404 pages before spending an LLM call."""
        if len(article_body) < 500 and not _DEVANAGARI_RE.search(article_body):
            body_lower = article_body.lower()
            if not any(kw in body_lower for kw in self._PAYWALL_KEYWORDS):
                stats["rejected"] += 1
                logger.debug(
                    "  rejected: %s — insufficient content (short English)", url[:80]
                )
                return True

        # Skip this check when the title is English but the body is Devanagari:
        # the English title words will never appear in a Nepali body, which would
        # falsely reject valid Nepali articles (common on mixed-language sites).
        english_title_nepali_body = not _DEVANAGARI_RE.search(
            article_title
        ) and _DEVANAGARI_RE.search(article_body)
        if article_title and len(article_title) > 10 and not english_title_nepali_body:
            title_words = [
                w.lower() for w in re.split(r"[\s\-–—|]+", article_title) if len(w) >= 4
            ][:5]
            body_beyond_nav = article_body[200:].lower()
            if title_words and not any(w in body_beyond_nav for w in title_words):
                stats["rejected"] += 1
                logger.debug(
                    "  rejected: %s — body lacks title keywords (likely paywall/redirect)",
                    url[:80],
                )
                return True

        body_lower_nf = article_body.lower()
        if any(sig in body_lower_nf for sig in self._NOT_FOUND_SIGNALS):
            stats["rejected"] += 1
            logger.debug("  rejected: %s — page signals article not found", url[:80])
            return True
        return False

    def _prefilter(self, fetch_result: dict, stats: dict) -> bool:
        """Cheap local checks before any LLM call. True = send to verification.

        Rejects empty / paywalled / 404 pages (counting them as rejected) so the
        LLM batches only contain candidates with real article bodies.
        """
        url = fetch_result["candidate"]["url"]
        article_body = (fetch_result.get("article_text") or "").strip()
        article_title = (fetch_result.get("article_title") or "").strip()
        if not article_body or len(article_body) < 100:
            stats["rejected"] += 1
            logger.debug("  rejected: %s — could not fetch article content", url[:80])
            return False
        if self._is_thin_or_missing(article_body, article_title, url, stats):
            return False
        return True

    def _collect_articles(
        self,
        candidates: list[dict],
        case: dict,
        stats: dict,
        press_release_text: Optional[str],
        max_to_accept: int,
    ) -> list[dict]:
        """Fetch + verify candidates sequentially, accepting up to one per event type."""
        accepted: list[dict] = []
        accepted_event_types: set[str] = set()
        event_type_counts = _existing_event_type_counts(case)
        deferred: list[dict] = []

        def would_exceed(event: str) -> bool:
            return (
                bool(event)
                and event_type_counts.get(event, 0) >= _MAX_ARTICLES_PER_EVENT_TYPE
            )

        def uncovered_events() -> list[str]:
            return [
                et
                for et in _ALL_EVENT_TYPES
                if et not in accepted_event_types and not would_exceed(et)
            ]

        for start in range(0, len(candidates), self._CANDIDATE_BATCH_SIZE):
            if len(accepted) >= max_to_accept or not uncovered_events():
                break
            batch = candidates[start : start + self._CANDIDATE_BATCH_SIZE]
            fetched = [fr for fr in (self._fetch_one(c, stats) for c in batch) if fr]
            if not fetched:
                continue
            for verified in self._verify_batch(
                fetched, case, press_release_text, stats
            ):
                event = verified.get("event_type", "")
                if (
                    event
                    and event not in accepted_event_types
                    and not would_exceed(event)
                ):
                    accepted.append(verified)
                    accepted_event_types.add(event)
                    event_type_counts[event] = event_type_counts.get(event, 0) + 1
                    stats["accepted"] += 1
                    logger.info(
                        "  Accepted: %s (event: %s, confidence: %s)",
                        verified["url"][:80],
                        event,
                        verified["confidence"],
                    )
                    if len(accepted) >= max_to_accept:
                        break
                else:
                    deferred.append(verified)

        self._fill_deferred_slots(
            deferred,
            max_to_accept,
            accepted,
            accepted_event_types,
            event_type_counts,
            stats,
            would_exceed,
        )
        return accepted

    def _fill_deferred_slots(
        self,
        deferred,
        limit,
        accepted,
        accepted_event_types,
        event_type_counts,
        stats,
        would_exceed,
    ) -> None:
        """Fill remaining accepted slots from deferred candidates (longest body first)."""
        sorted_deferred = sorted(
            deferred, key=lambda v: len(v.get("_article_text", "")), reverse=True
        )
        used = set()
        # First pass: cover still-uncovered event types.
        for idx, verified in enumerate(sorted_deferred):
            if len(accepted) >= limit:
                return
            event = verified.get("event_type", "")
            if event and event not in accepted_event_types and not would_exceed(event):
                accepted.append(verified)
                accepted_event_types.add(event)
                event_type_counts[event] = event_type_counts.get(event, 0) + 1
                stats["accepted"] += 1
                used.add(idx)
                logger.info(
                    "  Accepted (deferred): %s (event: %s)", verified["url"][:80], event
                )
        # Second pass: fill any remaining slots regardless of event coverage.
        for idx, verified in enumerate(sorted_deferred):
            if len(accepted) >= limit:
                return
            if idx in used:
                continue
            event = verified.get("event_type", "")
            if event and would_exceed(event):
                continue
            accepted.append(verified)
            event_type_counts[event] = event_type_counts.get(event, 0) + 1
            stats["accepted"] += 1
            logger.info(
                "  Accepted (same event): %s (event: %s)", verified["url"][:80], event
            )

    # — search —

    def _search_candidates(self, queries: list[str], stats: dict) -> list[dict]:
        all_candidates = []
        seen_urls = set()
        for index, query in enumerate(queries):
            stats["searched"] += 1
            try:
                results = _search_duckduckgo(query)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  Search error for '%s': %s", query[:60], exc)
                stats["errors"] += 1
                results = []
            new_count = 0
            for r in results:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    all_candidates.append(r)
                    new_count += 1
            logger.info(
                "  query '%s' → %d results (%d new)",
                query[:70],
                len(results),
                new_count,
            )
            if index < len(queries) - 1 and self.search_delay > 0:
                time.sleep(self.search_delay)
        logger.info(
            "  Found %d candidate URLs from %d queries",
            len(all_candidates),
            len(queries),
        )
        return all_candidates

    def _filter_new_candidates(
        self, candidates: list[dict], case_linked_urls: set[str], force: bool
    ) -> tuple[list[dict], int]:
        already_linked = sum(1 for c in candidates if c["url"] in case_linked_urls)
        if force:
            return list(candidates), already_linked
        new = [c for c in candidates if c["url"] not in case_linked_urls]
        return new, already_linked

    def _fallback_queries(self, case: dict, attempt: int) -> list[str]:
        accused_names = _get_accused_names(case)
        title = case.get("title") or ""
        if attempt == 0:
            queries = []
            for name in accused_names[:2]:
                name_clean = re.sub(r"\s+", " ", name).strip()
                if name_clean and len(name_clean) >= 3:
                    queries.append(f'"{name_clean}" Nepal')
            if title and len(title) > 10:
                queries.append(title[:100])
            return queries[:5]
        if attempt == 1:
            queries = []
            for name in accused_names[:2]:
                name_clean = re.sub(r"\s+", " ", name).strip()
                if name_clean and len(name_clean) >= 3:
                    queries.append(f"{name_clean} corruption Nepal")
                    queries.append(f"{name_clean} भ्रष्टाचार")
            return queries[:5]
        title_keywords = _extract_title_keywords(title)
        if title_keywords:
            return [f"{title_keywords} Nepal"]
        if accused_names:
            name_clean = re.sub(r"\s+", " ", accused_names[0]).strip()
            if name_clean and len(name_clean) >= 3:
                return [f"{name_clean} Nepal"]
        return ["CIAA Nepal corruption"]

    def _retry_with_fallbacks(
        self, case, stats, case_linked_urls, remaining_slots, press_release_text, force
    ) -> list[dict]:
        for attempt in range(self._RETRY_MAX):
            queries = self._fallback_queries(case, attempt)
            logger.info(
                "  Retry %d/%d: fallback search with %d queries",
                attempt + 1,
                self._RETRY_MAX,
                len(queries),
            )
            retry_candidates = self._search_candidates(queries, stats)
            new_candidates, retry_linked = self._filter_new_candidates(
                retry_candidates, case_linked_urls, force
            )
            if retry_linked > 0:
                stats["already_linked"] += retry_linked
            if not new_candidates:
                continue
            accepted = self._collect_articles(
                new_candidates, case, stats, press_release_text, remaining_slots
            )
            if accepted:
                logger.info(
                    "  Retry %d/%d: found %d accepted article(s)",
                    attempt + 1,
                    self._RETRY_MAX,
                    len(accepted),
                )
                return accepted
        logger.info("  All %d retries exhausted — no articles", self._RETRY_MAX)
        return []

    # — orchestration —

    def enrich_case(
        self, case: dict, dry_run: bool, force: bool, case_num: int, total: int
    ) -> dict:
        stats = _make_stats()
        case_id = case.get("case_id", "?")
        case_number = _resolve_case_number(case)
        cn_str = f" (#{case_number})" if case_number else ""
        logger.info(
            "[%d/%d] Processing %s%s — %s",
            case_num,
            total,
            case_id,
            cn_str,
            (case.get("title") or "")[:70],
        )

        current_count = _count_news_evidence(case)
        if current_count >= self.max_articles_per_case and not force:
            logger.info(
                "  Already has %d NEWS evidence entries (max=%d) — skipping",
                current_count,
                self.max_articles_per_case,
            )
            return _make_stats("skipped", "already_saturated")

        press_release_text, char_count = source_content(
            case, source_types={"CIAA_PRESS_RELEASE"}
        )
        if char_count:
            logger.info("  Press release context: %d chars", char_count)
        else:
            press_release_text = None
            logger.warning(
                "  no press release text — LLM verification lacks official context"
            )

        english_queries = self._generate_english_queries(case, press_release_text)
        queries = _generate_query_variations(case, llm_english_queries=english_queries)
        if not queries:
            return _make_stats("skipped", "no_queries")

        case_linked_urls = _case_linked_news_urls(case)
        remaining_slots = self.max_articles_per_case - current_count
        if remaining_slots <= 0:
            if not force:
                return _make_stats("skipped", "max_articles_reached")
            # --force on a saturated case: grant a fresh budget so it can still
            # accept articles (otherwise _collect_articles would exit immediately).
            remaining_slots = self.max_articles_per_case

        candidates = self._search_candidates(queries, stats)
        new_candidates, already_linked = self._filter_new_candidates(
            candidates, case_linked_urls, force
        )
        if already_linked:
            stats["already_linked"] = already_linked
            logger.info(
                "  %d candidate URLs already linked to this case", already_linked
            )

        new_candidates.sort(key=lambda c: len(c.get("snippet", "")), reverse=True)

        accepted = self._collect_articles(
            new_candidates, case, stats, press_release_text, remaining_slots
        )
        if not accepted and stats["already_linked"] == 0:
            accepted = self._retry_with_fallbacks(
                case,
                stats,
                case_linked_urls,
                remaining_slots,
                press_release_text,
                force,
            )

        if not accepted:
            stats["status"] = (
                "no_articles" if stats["already_linked"] == 0 else "skipped"
            )
            return stats

        stats["new_sources"] = self._save_articles(case, accepted, dry_run, stats)
        return stats

    def _save_articles(
        self, case: dict, accepted: list[dict], dry_run: bool, stats: dict
    ) -> int:
        """Create NEWS sources, append evidence, and transcribe — all over HTTP."""
        ordered = sorted(
            accepted,
            key=lambda a: _EVENT_LIFECYCLE_ORDER.get(
                a.get("event_type"), _EVENT_LIFECYCLE_ORDER[_EVENT_OTHER]
            ),
        )
        if dry_run:
            logger.info("  [DRY RUN] Would save %d article(s):", len(ordered))
            for a in ordered:
                logger.info("    - [%s] %s", a.get("event_type") or "?", a["url"])
            return len(ordered)

        slug = case.get("slug") or case.get("case_id")
        created = []  # source dicts for the transcription pass
        for article in ordered:
            # Step 1: create the source.
            try:
                title = _fix_mojibake(article.get("title") or "Untitled News Article")
                source = self.api.create_source(
                    title=title,
                    description=_build_source_description(article),
                    source_type=_NEWS_SOURCE_TYPE,
                    url=[{"link": article["url"], "role": "RAW"}],
                    publication_date=_publication_date(article, case),
                )
                source_id = source["source_id"]
            except requests.HTTPError as exc:
                body = getattr(exc.response, "text", "")[:200]
                logger.warning(
                    "  Failed to create source for %s: %s %s",
                    article["url"][:80],
                    exc,
                    body,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "  Failed to create source for %s: %s", article["url"][:80], exc
                )
                continue

            # Step 2: link it as evidence. Source creation + evidence link are not
            # atomic (no combined endpoint), so a failure here leaves an orphan
            # NEWS source. Surface its id loudly so it's recoverable, and skip the
            # transcription pass for it (an unlinked source must not be enriched).
            try:
                self.api.add_evidence(
                    slug=slug,
                    source_id=source_id,
                    description=_build_evidence_description(article),
                    event_type=article.get("event_type") or None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "  ORPHAN source %s created but NOT linked to case %s "
                    "(evidence PATCH failed: %s) — needs manual cleanup",
                    source_id,
                    slug,
                    exc,
                )
                continue

            created.append(
                {"source_id": source_id, "title": title, "url": article["url"]}
            )
            logger.info("  [SAVED] %s → %s", source_id, article["url"][:80])

        if self.transcribe and created:
            self._transcribe_sources(created, stats)
        return len(created)

    def _transcribe_sources(self, created: list[dict], stats: dict) -> None:
        """Convert each new NEWS article to markdown and attach it upstream.

        Reuses the same path as the ``reprocess_source_markdown`` command:
        ``sourcing.converter.convert_case_to_attach_candidates`` (likhit /
        trafilatura main-content conversion + frontmatter, skipping sources that
        already carry a MARKDOWN link) and the ``…/sources/<id>/markdown/``
        attach endpoint. We feed it a synthetic case holding only the sources we
        just created, so only those are converted.
        """
        from sourcing import converter

        synthetic_case = {
            "evidence": [
                {
                    "source_id": c["source_id"],
                    "description": c["title"],
                    "source": {
                        "title": c["title"],
                        "source_type": _NEWS_SOURCE_TYPE,
                        "urls": [{"link": c["url"], "role": "RAW"}],
                    },
                }
                for c in created
            ]
        }
        try:
            _, candidates = converter.convert_case_to_attach_candidates(
                synthetic_case, overwrite=self.overwrite_markdown
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("  transcription: conversion failed: %s", exc)
            return

        markdown_by_id = {c["source_id"]: c["markdown"] for c in candidates}
        for c in created:
            sid = c["source_id"]
            markdown = markdown_by_id.get(sid)
            if not markdown:
                logger.info("  transcription: no markdown produced for %s", sid)
                continue
            try:
                result = self.api.attach_markdown(
                    sid, markdown, overwrite=self.overwrite_markdown
                )
                if result.get("created"):
                    stats["transcribed"] += 1
                    logger.info("  [TRANSCRIBED] %s", sid)
                else:
                    logger.info("  transcription: markdown already present for %s", sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  transcription attach failed for %s: %s", sid, exc)


# ── stats + descriptions ───────────────────────────────────────────────────────


def _make_stats(status: str = "processed", reason: str = "") -> dict:
    stats = {
        "status": status,
        "searched": 0,
        "fetched": 0,
        "accepted": 0,
        "rejected": 0,
        "errors": 0,
        "already_linked": 0,
        "new_sources": 0,
        "transcribed": 0,
    }
    if reason:
        stats["reason"] = reason
    return stats


def _publication_date(article: dict, case: dict) -> str:
    """YYYY-MM-DD for the source: article date → case_start_date → today."""
    pub = article.get("publication_date")
    if isinstance(pub, date):
        return pub.isoformat()
    case_start = case.get("case_start_date")
    if isinstance(case_start, str) and case_start:
        return case_start[:10]
    return date.today().isoformat()


def _build_source_description(article: dict) -> str:
    summary = article.get("summary")
    if summary:
        return _fix_mojibake(summary)
    outlet = _guess_outlet(article.get("url", ""))
    pub_date = article.get("publication_date")
    if isinstance(pub_date, date):
        return f"{outlet}ले यस मुद्दासम्बन्धी समाचार प्रकाशित गरेको ({pub_date.isoformat()})।"
    return f"{outlet}ले यस मुद्दासम्बन्धी समाचार प्रकाशित गरेको।"


def _build_evidence_description(article: dict) -> str:
    outlet = _guess_outlet(article.get("url", ""))
    pub_date = article.get("publication_date")
    date_str = f" ({pub_date.isoformat()})" if isinstance(pub_date, date) else ""
    return _fix_mojibake(
        f"{outlet}{date_str} ले यस मुद्दासम्बन्धी समाचार प्रकाशित गरेको।"
    )


# ── target case selection ──────────────────────────────────────────────────────

_NEWS_STATES = frozenset({"DRAFT", "IN_REVIEW"})


def _news_target_cases(api: CaseworkApi, args):
    """Yield case detail dicts to enrich, honoring the standard selectors.

    --slug / --court-case fetch a case in ANY state; the batch path scans
    CORRUPTION cases in DRAFT/IN_REVIEW that are CIAA special-court.
    """
    count = 0
    limit = getattr(args, "limit", None)

    def _emit(case):
        nonlocal count
        count += 1
        return case

    # 1) Explicit slugs.
    slugs = getattr(args, "slug", None) or []
    for slug in slugs:
        try:
            case = api.get_case(slug)
        except requests.HTTPError as exc:
            logger.warning("fetch %s failed: %s", slug, exc)
            continue
        if is_ciaa_special_court_case(case):
            yield _emit(case)
            if limit and count >= limit:
                return
    if slugs:
        return

    # 2) Explicit court case numbers.
    court_cases = getattr(args, "court_case", None)
    if court_cases:
        wanted = {_court_number(c) for c in court_cases if c}
        seen = set()
        for summary in api.iter_cases(params={"case_type": "CORRUPTION"}):
            nums = {_court_number(ref) for ref in summary.get("court_cases") or []}
            slug = summary.get("slug")
            if not slug or slug in seen or not (wanted & nums):
                continue
            seen.add(slug)
            try:
                case = api.get_case(slug)
            except requests.HTTPError as exc:
                logger.warning("fetch %s failed: %s", slug, exc)
                continue
            if is_ciaa_special_court_case(case):
                yield _emit(case)
                if limit and count >= limit:
                    return
        return

    # 3) Batch scan.
    fiscal_year = getattr(args, "fiscal_year", None)
    priority_nums = None
    if getattr(args, "priority", False):
        from cases.services.priority_case_loader import load_priority_cases

        priority_nums = {_court_number(n) for n in load_priority_cases()}
        logger.info(
            "Priority mode: %d priority case number(s) loaded", len(priority_nums)
        )

    scanned = 0
    logger.info("Scanning corruption cases (filtering client-side)...")
    for summary in api.iter_cases(params={"case_type": "CORRUPTION"}):
        scanned += 1
        if scanned % 500 == 0:
            logger.info("  scanned %d cases (matched %d so far)...", scanned, count)
        if summary.get("state") not in _NEWS_STATES:
            continue
        if not is_ciaa_special_court_case(summary):
            continue
        if fiscal_year and not matches_fiscal_year(summary, fiscal_year):
            continue
        if priority_nums is not None:
            nums = {_court_number(ref) for ref in summary.get("court_cases") or []}
            if not (priority_nums & nums):
                continue
        slug = summary.get("slug")
        if not slug:
            continue
        try:
            case = api.get_case(slug)  # detail carries evidence + entities
        except requests.HTTPError as exc:
            logger.warning("fetch %s failed: %s", slug, exc)
            continue
        yield _emit(case)
        if limit and count >= limit:
            return


def _court_number(ref) -> str:
    if not isinstance(ref, str):
        return ""
    return ref.split(":")[-1].strip().upper()


# ── main ────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(
        description="Enrich CIAA cases with related news articles via search + LLM verification (DB-free).",
        epilog="Reads cases and writes sources/evidence entirely over HTTP via JAWAFDEHI_API_TOKEN.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--max-articles", type=int, default=5, help="Max articles per case (default: 5)"
    )
    ap.add_argument(
        "--search-delay",
        type=float,
        default=1.5,
        help="Seconds between search queries (default: 1.5)",
    )
    ap.add_argument(
        "--fetch-delay",
        type=float,
        default=0.5,
        help="Seconds between article fetches (default: 0.5)",
    )
    ap.add_argument(
        "--transcribe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Convert saved news articles to markdown and attach upstream (default: on)",
    )
    ap.add_argument(
        "--overwrite-markdown",
        action="store_true",
        help="Re-convert and replace markdown even if a source already has one",
    )
    args = ap.parse_args()

    if args.max_articles < 0:
        print("--max-articles must be non-negative", file=sys.stderr)
        sys.exit(1)
    if args.search_delay < 0 or args.fetch_delay < 0:
        print("--search-delay and --fetch-delay must be non-negative", file=sys.stderr)
        sys.exit(1)

    setup_logging(args.verbose)

    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:  # noqa: BLE001
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_json
    from llm.usage import UsageAccumulator, render_usage_table

    try:
        api = CaseworkApi(base_url=args.api_base_url, token=args.api_token)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    usage = UsageAccumulator()

    cases = list(_news_target_cases(api, args))
    total = len(cases)
    if total == 0:
        print("No CIAA cases to process.", file=sys.stderr)
        sys.exit(0)

    print(
        f"Found {total} CIAA case(s) to process (max {args.max_articles} articles each)."
    )
    if args.force:
        print("  --force: re-enriching even saturated cases")
    if args.dry_run:
        print("  [DRY RUN] No changes will be saved.")

    enricher = NewsEnricher(
        api=api,
        invoke_json=invoke_json,
        usage=usage,
        max_articles_per_case=args.max_articles,
        search_delay=args.search_delay,
        fetch_delay=args.fetch_delay,
        verbose=args.verbose,
        transcribe=args.transcribe,
        overwrite_markdown=args.overwrite_markdown,
    )

    totals = {
        "cases_processed": 0,
        "cases_skipped": 0,
        "cases_with_articles": 0,
        "cases_no_articles": 0,
        "searched": 0,
        "fetched": 0,
        "accepted": 0,
        "rejected": 0,
        "already_linked": 0,
        "new_sources": 0,
        "transcribed": 0,
        "errors": 0,
    }

    for idx, case in enumerate(cases, 1):
        try:
            result = enricher.enrich_case(case, args.dry_run, args.force, idx, total)
        except Exception as exc:  # noqa: BLE001
            totals["errors"] += 1
            logger.exception("Failed to process %s: %s", case.get("case_id"), exc)
            continue

        if result["status"] in ("skipped", "no_articles"):
            totals["cases_skipped"] += 1
        else:
            totals["cases_processed"] += 1
        if result.get("accepted", 0) > 0 or result.get("already_linked", 0) > 0:
            totals["cases_with_articles"] += 1
        elif result["status"] == "no_articles":
            totals["cases_no_articles"] += 1
        for key in (
            "searched",
            "fetched",
            "accepted",
            "rejected",
            "already_linked",
            "new_sources",
            "transcribed",
            "errors",
        ):
            totals[key] += result.get(key, 0)

    print_summary(totals, args.dry_run, "News article enrichment")

    if usage.calls > 0:
        print()
        print(
            render_usage_table(
                usage.as_dict()["by_provider"], title="news enrichment usage"
            )
        )


if __name__ == "__main__":
    main()
