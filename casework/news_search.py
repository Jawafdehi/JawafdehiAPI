"""Find and verify news articles about a CIAA case. READ-ONLY -- writes nothing, anywhere.

The read half of `casework/enrich_news_articles.py`, split out because the two
halves have opposite risk profiles. Everything here does network READS (web
search, article fetch, web-archive lookup) and LLM calls; nothing here can
create a material, bind evidence, or touch the case API. The write half is one
function in the enricher (`apply_plan`), which is also the only place that
imports `CaseworkApi`. That is `bind_materials.py`'s single-writer discipline:
read-only candidate producers, one writer, a quarantine gate between them.

Ported from the donor `casework/enrich_news_articles.py` (recovered at
`0321a85`, 1,957 lines). The search plumbing, HTML/date parsing, URL screening,
query generation and the two-tier verification prompt are the donor's and are
kept; donor line numbers are cited where a constant or rule is pinned. The
web-archive half comes from the second donor, the deleted
`cases/management/commands/add_news_permalinks.py` (recovered at `4c39d8c^`).

Deliberate deviations from the donor live in the enricher's module docstring,
except the two that are wholly inside this module:

DEVIATION A -- AN ARTICLE WITH NO PUBLICATION DATE IS DROPPED, AND REPORTED.
The donor bound it and defaulted the date to `case_start_date` or *today*
(`_publication_date`, donor:1680). The permalinks donor refused the same rows
outright ("NEWS sources with no publication_date are skipped and reported, not
modified"), and this port follows the stricter one. Two reasons beyond
consistency: a citation dated "today" is a fabricated fact in the one artefact
whose job is provenance, and this stage derives a material's IRI from the
publication date (see `news_material_ident`), so a missing date would make the
IRI non-reproducible and defeat the idempotency check. `SkipReason.NO_DATE`
rows are counted and listed, never silently dropped.

DEVIATION B -- THE ACCEPTANCE BAR IS `confidence == "high"`, NOT `relevant ==
true`. The donor bound anything the premium verifier called relevant at any
confidence (donor:1080). The brief requires zero false positives on the
labelled set and near-misses reported for human confirmation rather than bound,
and the donor's own rubric is what makes `high` the right line: its prompt
grades "matching on case number alone" as STRONG and "defendant name +
corruption allegations" as MEDIUM (donor:173-174). Medium is therefore the
literal description of this enricher's worst failure -- the same accused's other
case. Production already carries two such binds on
`vikal-paudel-080-cr-0174-illegal-assets`; see `tests/casework/news_labelled_set.py`.
A relevant-but-not-high verdict becomes a `NearMiss`, which the review file
prints for a human and the writer never touches.
"""
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("casework.news_search")

# casework/news_search.py -> casework -> <repo-root>
_REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Donor-pinned constants. Changing any of these changes what the run finds, so
# each carries its donor line.
# ---------------------------------------------------------------------------

MAX_HTML_REGEX_LENGTH = 500_000            # donor:52
SEARCH_RESULTS_PER_QUERY = 8               # donor:388 (`results[:8]`)
QUERY_LIMIT = 12                           # donor:105
QUERY_RESERVED_ENGLISH_SLOTS = 4           # donor:106
QUERY_RESERVED_EVENT_SLOTS = 4             # donor:107
MAX_ARTICLES_PER_EVENT_TYPE = 1            # donor:104
CANDIDATE_BATCH_SIZE = 12                  # donor:898
SEARCH_RETRY_MAX = 3                       # donor:316
MIN_ARTICLE_CHARS = 100                    # donor:1132, 1218

#: Shortest evidence note that may be BOUND. Not a donor constant -- the donor
#: bound whatever the model returned. Measured floor across the 33 news notes on
#: the 15 IN_REVIEW cases is 351 chars (median 494, max 759); this sits well below
#: that so a terse-but-complete note still binds, while a `salvage_json`-repaired
#: truncation cannot masquerade as one. See `Verdict.is_bindable`.
MIN_NOTE_CHARS = 120

# The donor sent a self-identifying UA to DuckDuckGo and a browser-ish one to
# article hosts. Kept split: the WAF note in `casework/common/materials.py`
# (`fetch_markdown` sends a browser UA because the WAF 403s anything else) is
# about jawafdehi hosts, and news hosts behave the same way.
SEARCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JawafdehiAPI/1.0)"}   # donor:54
FETCH_HEADERS = {                                                              # donor:57
    "User-Agent": "Mozilla/5.0 (compatible; JawafdehiAPI/1.0)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en,ne;q=0.9",
}

OFFICIAL_PRESS_RELEASE_PATTERNS = (                                            # donor:65
    re.compile(r"^https?://(?:www\.)?ciaa\.gov\.np/(?:index\.php/)?pressrelease/",
               re.IGNORECASE),
)
URL_BLOCKLIST_PATTERNS = (                                                     # donor:71
    re.compile(r"/tag[/?]|/category[/?]|/author[/?]|/page/\d+", re.IGNORECASE),
)
NON_NEWS_DOMAIN_PATTERNS = (                                                   # donor:74
    re.compile(r"^https?://(?:[a-z-]+\.)?wikipedia\.org/", re.IGNORECASE),
    re.compile(r"^https?://(?:[a-z-]+\.)?facebook\.com/", re.IGNORECASE),
)

# Event types in a CIAA corruption-case lifecycle (donor:80-102).
EVENT_INVESTIGATION = "investigation"
EVENT_FILING = "filing"
EVENT_HEARING = "hearing"
EVENT_VERDICT = "verdict"
EVENT_APPEAL = "appeal"
EVENT_OTHER = "other"

ALL_EVENT_TYPES = (EVENT_INVESTIGATION, EVENT_FILING, EVENT_HEARING,
                   EVENT_VERDICT, EVENT_APPEAL)
EVENT_LIFECYCLE_ORDER = {
    EVENT_INVESTIGATION: 1, EVENT_FILING: 2, EVENT_HEARING: 3,
    EVENT_VERDICT: 4, EVENT_APPEAL: 5, EVENT_OTHER: 6,
}

EVENT_QUERY_TEMPLATES = {                                                      # donor:109
    EVENT_INVESTIGATION: ["{name} CIAA investigation"],
    EVENT_FILING: ["{name} अख्तियार मुद्दा दायर",
                   "{name} CIAA charge sheet special court"],
    EVENT_HEARING: ["{name} सुनुवाइ विशेष अदालत",
                    "{name} hearing special court corruption"],
    EVENT_VERDICT: ["{name} फैसला विशेष अदालत",
                    "{name} verdict special court corruption"],
    EVENT_APPEAL: ["{name} पुनरावेदन सर्वोच्च",
                   "{name} supreme court appeal corruption",
                   "{name} सर्वोच्च अदालत फैसला"],
}

#: Second template set, built from the words that actually occur in the 138
#: deduplicated news articles bound to PUBLISHED cases (measured 2026-08-06).
#: The donor's set above was written from intuition and several of its terms
#: barely exist in the corpus it is meant to match -- share of article BODIES:
#:
#:     अख्तियार 64%   भ्रष्टाचार 63%   मुद्दा दायर 57%   विशेष अदालत 57%
#:     बिगो 41%       ठहर 20%          फैसला 10%         सुनुवाइ 5%
#:     सर्वोच्च 8%     पुनरावेदन 3%      पेशी 1%           थुनामा 1%
#:
#: So the verdict template searched `फैसला` while `ठहर` is twice as common, and
#: the hearing template searched `सुनुवाइ`, which appears in ZERO headlines. The
#: donor set is kept and still searched first -- these are additional, never a
#: replacement, so a query that used to work still runs.
EVENT_QUERY_TEMPLATES_MEASURED = {
    EVENT_INVESTIGATION: ["{name} अख्तियार अनुसन्धान भ्रष्टाचार"],
    EVENT_FILING: ["{name} विरुद्ध भ्रष्टाचार मुद्दा दायर",
                   "{name} अख्तियार बिगो भ्रष्टाचार"],
    # `सुनुवाइ`/`पेशी` are effectively absent from the corpus AND only 2 of 169
    # bound articles are hearing coverage, so there is little to find. Custody
    # orders are what actually get reported at this stage.
    EVENT_HEARING: ["{name} थुनामा पुर्पक्ष विशेष अदालत"],
    EVENT_VERDICT: ["{name} विशेष अदालत ठहर भ्रष्टाचार",
                    "{name} सफाइ विशेष अदालत"],
    EVENT_APPEAL: ["{name} सर्वोच्च अदालत पुनरावेदन भ्रष्टाचार"],
}

#: Words that mark a timeline entry as a given lifecycle stage. Used to work out
#: which stages a case has actually REACHED, so query slots are not spent asking
#: for a verdict on a case still under investigation.
TIMELINE_EVENT_WORDS = {
    EVENT_APPEAL: ("पुनरावेदन", "सर्वोच्च"),
    EVENT_VERDICT: ("ठहर", "फैसला", "सफाइ", "सजाय", "कैद", "जरिवाना"),
    EVENT_HEARING: ("थुनामा", "पुर्पक्ष", "पेशी", "सुनुवाइ", "बहस", "बयान"),
    EVENT_FILING: ("अभियोगपत्र", "आरोपपत्र", "मुद्दा दायर", "दायर"),
    EVENT_INVESTIGATION: ("अनुसन्धान", "छानबिन", "उजुरी", "पक्राउ", "प्रतिवेदन"),
}

#: The same stages in English, for sorting the queries the MODEL returns.
#: `generate_english_queries` asks for "one query per event type" in lifecycle
#: order, and only the first `QUERY_RESERVED_ENGLISH_SLOTS` are sent -- so
#: without a re-sort the verdict and appeal queries are always the ones cut,
#: on exactly the cases whose timeline says the verdict is what to look for.
EVENT_WORDS_EN = {
    EVENT_APPEAL: ("appeal", "supreme court"),
    EVENT_VERDICT: ("verdict", "convict", "acquit", "sentenc", "ruling", "fined"),
    EVENT_HEARING: ("hearing", "custody", "remand", "testimony", "detention"),
    EVENT_FILING: ("charge sheet", "chargesheet", "indict", "filed", "prosecut"),
    EVENT_INVESTIGATION: ("investigat", "probe", "complaint", "arrest", "raid"),
}

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# Web-archive endpoints, from the permalinks donor (add_news_permalinks:42-46).
ARCHIVE_HOSTS = ("web.archive.org", "archive.org")
AVAILABILITY_API = "https://archive.org/wayback/available"
SAVE_BASE = "https://web.archive.org/save/"
ARCHIVE_UA = "Mozilla/5.0 (jawafdehi-news-permalinks)"


class SkipReason(str, Enum):
    """Why a candidate never reached the verifier, or was refused after it.

    A string enum so a reason lands in the events JSONL and the review file as
    itself rather than as a repr. Every one of these is COUNTED and listed --
    `bind_materials.py`'s rule that an unmet prerequisite is reported, never a
    silent skip, applies just as much to a dropped article.
    """
    OFFICIAL_PRESS_RELEASE = "official CIAA press release, not independent coverage"
    BLOCKLISTED = "tag/category/author listing page"
    NON_NEWS_DOMAIN = "non-news domain (wikipedia/facebook)"
    FETCH_FAILED = "could not fetch the page"
    NOT_HTML = "response was not HTML"
    TOO_SHORT = "under 100 chars of extracted text"
    THIN_OR_PAYWALLED = "paywall/redirect/404 shell, not an article body"
    NOT_FOUND_PAGE = "page says the article does not exist"
    NO_DATE = "no publication date (deviation A -- skipped and reported)"
    ALREADY_LINKED = "already bound to this case"
    GATE_REJECTED = "cheap-tier gate: not plausibly this case"
    VERIFY_REJECTED = "premium verifier: not this case"
    #: The verifier did NOT answer -- a provider error or an unparseable reply.
    #: Counted apart from VERIFY_REJECTED because a pile of these means the run
    #: is broken, where a pile of rejections is a normal day.
    VERIFY_FAILED = "premium verifier FAILED to answer (run is unreliable)"
    EVENT_TYPE_FULL = "an article for this event type is already bound"
    #: Verified, and would otherwise have been bindable, but `--max-articles` was
    #: already filled. Counted rather than dropped: these cost premium tokens, and
    #: a `medium` verdict cut off here still owes the reviewer a near-miss line.
    BUDGET_REACHED = "verified but --max-articles already filled"


@dataclass
class Article:
    """One fetched candidate. `published` is never None on an accepted article
    (deviation A drops those), so the writer can rely on it."""
    url: str
    title: str
    text: str
    published: date | None
    snippet: str = ""

    @property
    def outlet(self):
        return guess_outlet(self.url)


@dataclass
class Verdict:
    """The premium verifier's answer for one candidate.

    `failed` marks "the verifier did not answer" as distinct from "the verifier
    said no". Both refuse the bind; only one means the run is broken. Without the
    distinction a provider outage produces a full set of not-relevant verdicts,
    and for this stage that is a completely ordinary-looking result.
    """
    relevant: bool
    confidence: str = ""
    reason: str = ""
    event_type: str = ""
    summary: str = ""
    failed: bool = False

    @property
    def is_bindable(self):
        """Deviation B: `relevant` alone is not enough.

        All four conditions are load-bearing. `high` is the bar (see the module
        docstring). `event_type` must be a real lifecycle value because the
        per-event cap and the bind ordering both key on it. `summary` must be
        SUBSTANTIAL because it IS the evidence note -- binding without one is the
        `bind_materials.py:143` blank-note behaviour this port exists to avoid,
        and a one-line note is that behaviour with extra steps.

        The length floor is not cosmetic. `salvage_json` repairs a reply truncated
        at `max_tokens` by closing the open string, so an overflowing verify call
        yields a note cut off mid-sentence -- `"यो समाचार लेख यस मुद्दा (080-CR-0136) मा"`
        and nothing more. Non-blank, so the old check passed it, and it would then
        be published as a case's evidence note. The measured production floor is
        351 chars (median 494, max 759); `MIN_NOTE_CHARS` sits well under that so a
        genuinely terse but complete note still binds, while a truncation cannot.
        """
        return bool(
            self.relevant
            and self.confidence == "high"
            and self.event_type in EVENT_LIFECYCLE_ORDER
            and len(self.summary.strip()) >= MIN_NOTE_CHARS
        )


@dataclass
class NearMiss:
    """A candidate the verifier called relevant but not at `high` confidence.

    Reported for human confirmation and never bound (deviation B). Carries the
    verifier's own reason so a reviewer can see what it matched on.
    """
    article: Article
    verdict: Verdict


@dataclass
class Skipped:
    url: str
    reason: SkipReason
    detail: str = ""


@dataclass
class SearchOutcome:
    """Everything one case's read phase produced. No writes implied by any of it."""
    accepted: list = field(default_factory=list)     # [(Article, Verdict)]
    near_misses: list = field(default_factory=list)  # [NearMiss]
    skipped: list = field(default_factory=list)      # [Skipped]
    queries: list = field(default_factory=list)
    n_candidates: int = 0

    def add_skip(self, url, reason, detail=""):
        self.skipped.append(Skipped(url, reason, detail))


# ---------------------------------------------------------------------------
# HTML -> text/title/date. Donor-verbatim behaviour (donor:190-293).
# ---------------------------------------------------------------------------


def _truncate_for_regex(html):
    return html[:MAX_HTML_REGEX_LENGTH] if len(html) > MAX_HTML_REGEX_LENGTH else html


class _TextExtractor(HTMLParser):
    """Visible text, skipping script/style/nav/footer.

    `_skip_depth` is a counter, not a bool, so a `<script>` nested inside a
    `<nav>` does not re-enable extraction when the inner tag closes (donor:203).
    """

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._skip_depth = 0
        self._skip_tags = {"script", "style", "noscript", "nav", "footer"}

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        lowered = tag.lower()
        if lowered in self._skip_tags:
            self._skip_depth = max(0, self._skip_depth - 1)
        if lowered in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.text_parts.append(data.strip())


def fix_mojibake(text):
    """Repair UTF-8 Devanagari that was decoded as Latin-1 somewhere upstream.

    The `à¤...` pattern is the hallmark. Donor-verbatim (donor:225).
    """
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def extract_text_from_html(html):
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 -- a malformed page must not sink the case
        log.debug("HTML parse error", exc_info=True)
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    return fix_mojibake(text)


def extract_title_from_html(html):
    match = re.search(r"<title[^>]*>([^<]*)</title>", _truncate_for_regex(html),
                      re.IGNORECASE)
    if not match:
        return ""
    return fix_mojibake(re.sub(r"\s+", " ", match.group(1)).strip())


def parse_date_string(value):
    value = (value or "").strip()[:19]     # drops a trailing 'Z' / offset
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


#: A CIAA case reported in the news falls comfortably inside this window. The
#: lower bound is generous (the oldest cases in the corpus are FY076-era); the
#: upper bound is the real guard -- a Bikram Sambat year read as Gregorian lands
#: ~57 years ahead, so anything beyond next year is a mis-parse rather than a
#: scoop. Checked against the article's own claimed date only; nothing here
#: converts BS to AD.
PUBLICATION_YEAR_MIN = 1990
PUBLICATION_YEAR_MAX_AHEAD = 1


def _is_plausible_publication_date(parsed):
    """False for a date no real article could carry (chiefly a BS year)."""
    if parsed is None:
        return False
    if parsed.year < PUBLICATION_YEAR_MIN:
        return False
    # `date.today()` rather than a frozen constant: this is a live plausibility
    # bound, and a hardcoded year would silently start rejecting real articles.
    return parsed.year <= date.today().year + PUBLICATION_YEAR_MAX_AHEAD


def extract_publication_date(html):
    """The article's own publication date, or None. Donor-verbatim (donor:269)."""
    safe = _truncate_for_regex(html)
    patterns = (
        r'<meta[^>]+?property="article:published_time"[^>]+?content="([^"]+)"',
        r'<meta[^>]+?name="[^"]*date[^"]*"[^>]+?content="([^"]+)"',
        r'<meta[^>]+?itemprop="datePublished"[^>]+?content="([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, safe, re.IGNORECASE)
        if match:
            parsed = parse_date_string(match.group(1))
            # Checked here too, not just on the Devanagari fallback: a site that
            # renders Bikram Sambat in its prose often also emits it in
            # `"datePublished"`, and `%Y-%m-%d` parses "2082-04-20" happily.
            if parsed is not None and _is_plausible_publication_date(parsed):
                return parsed
    # ASCII digits ONLY, and a plausibility range. `\d` matches Devanagari ०-९
    # and `int()` accepts them, so `प्रकाशित: २०८२-०४-२०` -- an extremely common
    # Nepali byline -- parsed as date(2082, 4, 20). That is a Bikram Sambat date
    # read as Gregorian: 56 years in the future. Deviation A makes the date part
    # of the material IRI, so a BS date corrupts the idempotency key as well as
    # publishing a fabricated `datePublished` on a PUBLIC material. Converting BS
    # to AD here is out of scope (`convert_date` owns that); refusing the date is
    # correct, and deviation A already reports a dateless article as a skip.
    match = re.search(r"(?:प्रकाशित|मिति)[:\s]*([0-9]{4})[-/]([0-9]{1,2})[-/]([0-9]{1,2})",
                      safe)
    if match:
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return parsed if _is_plausible_publication_date(parsed) else None
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# URL screening. All deterministic -- these run before any LLM call.
# ---------------------------------------------------------------------------


def is_official_press_release(url):
    return any(p.search(url) for p in OFFICIAL_PRESS_RELEASE_PATTERNS)


def screen_url(url):
    """The `SkipReason` this URL is disqualified by, or None.

    Order matters only for the log line. The press-release check comes first
    because it is the one skip that is not a quality judgement: the CIAA's own
    release is already bound to the case, and this stage exists to add an
    INDEPENDENT publisher (donor:1119).
    """
    if is_official_press_release(url):
        return SkipReason.OFFICIAL_PRESS_RELEASE
    for pattern in URL_BLOCKLIST_PATTERNS:
        if pattern.search(url):
            return SkipReason.BLOCKLISTED
    for pattern in NON_NEWS_DOMAIN_PATTERNS:
        if pattern.search(url):
            return SkipReason.NON_NEWS_DOMAIN
    return None


def guess_outlet(url):
    """`kathmandupost.com/...` -> `Kathmandupost`. Donor-verbatim (donor:421)."""
    try:
        hostname = re.sub(r"^www\d*\.", "", urllib.parse.urlparse(url).hostname or "")
        parts = hostname.split(".")
        return parts[-2].title() if len(parts) >= 2 else hostname
    except Exception:  # noqa: BLE001
        return "Unknown"


PAYWALL_KEYWORDS = frozenset({"ciaa", "corruption", "akhtiyar",
                              "अख्तियार", "भ्रष्टाचार"})            # donor:1157
NOT_FOUND_SIGNALS = ("does not exist", "page not found", "article not found",
                     "content not found", "no longer available",
                     "nothing was found", "could not be found")     # donor:1160


def screen_body(text, title, url):
    """The `SkipReason` this fetched body is disqualified by, or None.

    Rejects paywall shells, redirect stubs and soft-404s before an LLM call is
    spent on them. Donor-verbatim (donor:1170), including the one subtlety
    worth keeping: the title-keyword check is SKIPPED when the title is English
    and the body Devanagari, because English title words never appear in a
    Nepali body and the check would reject valid Nepali articles wholesale
    (donor:1183).
    """
    body = (text or "").strip()
    if len(body) < MIN_ARTICLE_CHARS:
        return SkipReason.TOO_SHORT

    if len(body) < 500 and not DEVANAGARI_RE.search(body):
        if not any(kw in body.lower() for kw in PAYWALL_KEYWORDS):
            return SkipReason.THIN_OR_PAYWALLED

    english_title_nepali_body = (
        not DEVANAGARI_RE.search(title or "") and DEVANAGARI_RE.search(body))
    if title and len(title) > 10 and not english_title_nepali_body:
        words = [w.lower() for w in re.split(r"[\s\-–—|]+", title) if len(w) >= 4][:5]
        if words and not any(w in body[200:].lower() for w in words):
            return SkipReason.THIN_OR_PAYWALLED

    if any(sig in body.lower() for sig in NOT_FOUND_SIGNALS):
        return SkipReason.NOT_FOUND_PAGE
    return None


# ---------------------------------------------------------------------------
# Search. Throttled and cached (brief item 6) -- `--max-articles` bounds what
# is BOUND, which is not a throttle on what is FETCHED.
# ---------------------------------------------------------------------------


def extract_ddg_redirect(url):
    """The real URL behind a DuckDuckGo `uddg=` redirect.

    NOT donor-verbatim, deliberately. The donor unquoted `uddg` a second time
    after `parse_qs` had already percent-decoded it (donor:299), which
    double-decodes the target:

    - A Devanagari slug comes back as literal Devanagari, and
      `urllib.request.urlopen` then raises `UnicodeEncodeError: 'ascii' codec`.
      `WebClient.get` swallows that into `(None, None)`, so every Nepali news
      article with a Devanagari path was reported as a dead host -- silently, and
      exactly for the articles this enricher exists to find.
    - A target containing a literal `%20` arrives as `%2520`, decodes once to
      `%20` and again to a space: a DIFFERENT URL, which was then fetched, hashed
      into `news_material_ident` and stored as the material's RAW link.

    `parse_qs` already did the one decode the redirect calls for, so this returns
    its output unchanged.
    """
    if "uddg=" not in url:
        return url
    uddg = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg", [""])[0]
    return uddg or url


_DDG_LINK_RE = re.compile(
    r'<a[^>]{0,200}class="result__a"[^>]{0,100}href="([^"]{1,500})"[^>]{0,50}>'
    r"([^<]{1,500})</a>", re.IGNORECASE)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]{0,200}class="result__snippet"[^>]{0,100}>([^<]{1,1000})</a>', re.IGNORECASE)


def parse_ddg_html(html):
    """`[{title, url, snippet}]` from a DuckDuckGo HTML result page (donor:363)."""
    html = _truncate_for_regex(html)
    links = _DDG_LINK_RE.findall(html)
    snippets = _DDG_SNIPPET_RE.findall(html)
    results = []
    for i, (href, title_html) in enumerate(links):
        url = extract_ddg_redirect(href)
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        if url and title:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results[:SEARCH_RESULTS_PER_QUERY]


#: `host:port` of a SOCKS5 proxy for THIS module's traffic only. Set when the run
#: happens on a host search engines refuse (see `search_duckduckgo`) and an SSH
#: reverse tunnel from an unblocked connection is carrying the queries:
#:
#:     ssh -R 1080 you@sandbox                    # laptop, keeps the tunnel up
#:     export CASEWORK_SOCKS_PROXY=127.0.0.1:1080 # sandbox
SOCKS_PROXY_ENV = "CASEWORK_SOCKS_PROXY"


def _parse_proxy(spec):
    host, sep, port = spec.strip().rpartition(":")
    if not sep or not port.isdigit():
        raise SearchUnavailable(
            f"${SOCKS_PROXY_ENV}={spec!r} is not host:port "
            f"(e.g. 127.0.0.1:1080).")
    return host or "127.0.0.1", int(port)


def build_socks_opener(spec):
    """A urllib opener whose sockets dial out through a SOCKS5 proxy.

    SCOPED TO THIS MODULE'S CLIENT ON PURPOSE. The usual PySocks recipe --
    `socks.set_default_proxy()` then `socket.socket = socks.socksocket` -- is
    process-global, so it would push the case API and the LLM provider through
    the tunnel as well. Those must stay on the local interface: the API is
    reached over loopback during a smoke run, and routing an operator's personal
    connection into the write path is not something a search workaround should
    do silently.

    `rdns=True` resolves hostnames at the proxy. Resolving locally would leak
    every news host into this machine's DNS and, worse, could resolve to an
    address the tunnel's exit cannot reach.
    """
    import http.client

    # Validate the SPEC before importing, so a typo is reported as a typo. The
    # other order told an operator who wrote `CASEWORK_SOCKS_PROXY=1080` to go
    # install PySocks, which would not have helped.
    proxy_host, proxy_port = _parse_proxy(spec)

    try:
        import socks
    except ImportError as exc:
        raise SearchUnavailable(
            f"${SOCKS_PROXY_ENV} is set but PySocks is not installed. "
            f"Install it with `uv pip install PySocks`, or `uv sync --extra "
            f"casework-proxy`.") from exc

    def _dial(address, timeout):
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, proxy_host, proxy_port, rdns=True)
        if isinstance(timeout, (int, float)):
            sock.settimeout(timeout)
        sock.connect(address)
        return sock

    class _TunnelledHTTPS(http.client.HTTPSConnection):
        def connect(self):
            sock = _dial((self.host, self.port), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class _TunnelledHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = _dial((self.host, self.port), self.timeout)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_TunnelledHTTPS, req, context=self._context)

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_TunnelledHTTP, req)

    return urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)


#: Query parameters that carry a credential. Google's Custom Search API is the
#: reason this exists: it takes the key in the QUERY STRING with no header
#: alternative, unlike Brave/Serper/Tavily, so the URL itself is a secret.
_SECRET_QUERY_PARAMS = ("key", "api_key", "apikey", "token", "subscription-token")


def redact_url(url):
    """`url` with any credential-bearing query parameter masked.

    A URL reaches a log line on transport failure, and for google_cse that URL
    contains the API key. Cheap insurance against a credential landing in a run
    log that then gets pasted into an issue.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.lower() in _SECRET_QUERY_PARAMS for k, _ in pairs):
        return url
    masked = [(k, "REDACTED" if k.lower() in _SECRET_QUERY_PARAMS else v)
              for k, v in pairs]
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(masked)))


class WebClient:
    """Throttled, cached HTTP reads for search / article / archive.

    THE THROTTLE IS THE POINT OF THE CLASS. The donor paced itself with two
    `time.sleep` calls buried in the search and fetch loops (donor:1365, 1154),
    which meant a caller could not slow a run down without editing the module,
    and a retry inside `_search_duckduckgo` ignored the pacing entirely. Here
    every outbound request goes through `_throttled`, keyed by KIND, so search /
    fetch / archive-save each hold their own minimum interval and a retry is
    spaced like any other call.

    THE CACHE IS PER-CASE AND IN-PROCESS ONLY. A case searches the same accused
    name under several query templates and the same URL surfaces repeatedly, so
    within one case a repeat is free. It is deliberately NOT a disk cache: the
    project closed PR #409 (`casework/common/llm_cache.py`) because a cache that
    outlives a prompt revision serves a stale artefact as if it were fresh, and
    a cached news page has exactly the same defect.

    `clear_cache()` between cases keeps it bounded. The hit rate lives entirely
    inside one case -- two different cases share no candidate URLs -- while a
    run-lifetime cache retained every decoded search page and full article body
    until the process exited. Over a 238-case bulk run that is thousands of
    documents at 50-300 KB each, held for no benefit.
    """

    def __init__(self, search_delay=1.5, fetch_delay=0.5, save_delay=6.0, timeout=20,
                 proxy=None, budget=None):
        self.delays = {"search": search_delay, "fetch": fetch_delay,
                       "archive": 0.0, "save": save_delay}
        self.timeout = timeout
        self._last = {}
        self._cache = {}
        self.calls = {"search": 0, "fetch": 0, "archive": 0, "save": 0}
        #: Charged per billable query. Held HERE rather than in each provider
        #: so a new provider cannot forget it -- every one of them reaches the
        #: network through `get` or `post_json` and nowhere else.
        self.budget = budget
        # An explicit `proxy=""` disables the tunnel even when the env var is
        # set, which is how the tests keep themselves off any real socket.
        spec = os.environ.get(SOCKS_PROXY_ENV, "") if proxy is None else proxy
        self.proxy = spec.strip()
        #: `build_opener()` with no handlers still installs a ProxyHandler that
        #: reads `https_proxy`, so an ordinary HTTP proxy keeps working here with
        #: no SOCKS involvement and no extra dependency.
        self._opener = (build_socks_opener(self.proxy) if self.proxy
                        else urllib.request.build_opener())

    def invalidate(self, url, kind):
        """Forget one cached response so the next `get` is a real request.

        Public because two call sites need it and both were reaching into
        `_cache` directly -- which a client stub (the tests' `FakeWeb`) does not
        implement, so the branches that did it were untestable. A cached MISS is
        the hazard: retrying after a failure, or re-querying an archive
        availability endpoint after asking for a capture, both have to bypass it.
        """
        self._cache.pop((kind, url), None)

    def clear_cache(self):
        """Drop every cached response. Called between cases, not between runs."""
        self._cache.clear()

    def _charge(self, kind):
        """Debit the budget for one billable request, before it is sent.

        Only `search` counts. Fetching an article body hits the publisher, not
        the paid API, and archiving hits the Wayback Machine -- charging those
        would exhaust the cap on requests nobody bills us for.

        Called after the cache check on purpose: a cache hit sends nothing, so
        charging it would bill us for a request that never left the process and
        make the ledger disagree with the provider's own count.
        """
        if kind == "search" and self.budget is not None:
            self.budget.spend()

    def _throttled(self, kind):
        delay = self.delays.get(kind, 0.0)
        if delay <= 0:
            return
        wait = delay - (time.monotonic() - self._last.get(kind, 0.0))
        # A first call has no predecessor, so `_last` defaults to 0.0 and
        # `wait` comes out hugely negative -- i.e. no sleep. Correct, and the
        # reason this is `wait > 0` rather than a "have we called yet" flag.
        if wait > 0:
            time.sleep(wait)
        self._last[kind] = time.monotonic()

    def get(self, url, kind, headers=None, expect_html=False, error_body=False):
        """`(status, text)` for a GET, or `(None, None)` on a transport error.

        Never raises: a dead news host is an ordinary outcome of searching the
        open web, and one of them must not end the case.

        `error_body=True` returns the 4xx/5xx BODY instead of discarding it.
        Off by default and opt-in per call on purpose: a keyed JSON provider puts
        the reason in that body -- google_cse answers 403 for BOTH a revoked key
        and daily-quota exhaustion, and only the body tells them apart, which the
        operator needs because the next action is opposite (re-issue vs wait).
        Returning it unconditionally would be worse than not having it: the HTML
        callers test `if html:` and would hand a 403 error page to
        `parse_ddg_html` or to `screen_body`, turning a refusal into "no results"
        -- the silent zero this whole module is built to refuse.
        """
        key = (kind, url)
        if key in self._cache:
            return self._cache[key]
        self._charge(kind)
        self._throttled(kind)
        self.calls[kind] = self.calls.get(kind, 0) + 1
        request = urllib.request.Request(url, headers=headers or FETCH_HEADERS)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if expect_html:
                    content_type = (response.headers.get("content-type") or "").lower()
                    if ("text/html" not in content_type
                            and "application/xhtml" not in content_type):
                        result = (response.status, None)
                        self._cache[key] = result
                        return result
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                result = (response.status, body.decode(charset, errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = None
            if error_body:
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:2000]
                except Exception:  # noqa: BLE001
                    detail = None
            result = (exc.code, detail)
        except Exception as exc:  # noqa: BLE001
            log.debug("GET %s failed: %s", redact_url(url)[:120], exc)
            result = (None, None)
        self._cache[key] = result
        return result

    def post_json(self, url, kind, payload, headers=None):
        """`(status, text)` for a JSON POST. Same never-raises contract as `get`.

        Exists because two of the keyed search providers (Serper, Tavily) are
        POST-only. Routed through the same throttle, cache and call counter as
        `get` so a provider swap does not quietly bypass the pacing -- the whole
        point of this class.

        The cache key folds in a hash of the body: one URL serves every query,
        so keying on the URL alone would serve the first query's results to all
        twelve. On an HTTPError the body IS read and returned, unlike `get`:
        a keyed provider puts the reason ("invalid key", "quota exceeded") in
        the 4xx body, and that has to reach the operator.
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        key = (kind, f"{url}#{hashlib.sha256(body).hexdigest()[:16]}")
        if key in self._cache:
            return self._cache[key]
        self._charge(kind)
        self._throttled(kind)
        self.calls[kind] = self.calls.get(kind, 0) + 1
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json", **(headers or {})})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                result = (response.status,
                          response.read().decode(charset, errors="replace"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                detail = ""
            result = (exc.code, detail or None)
        except Exception as exc:  # noqa: BLE001
            log.debug("POST %s failed: %s", redact_url(url)[:120], exc)
            result = (None, None)
        self._cache[key] = result
        return result


class SearchUnavailable(RuntimeError):
    """The search backend answered, but not with results.

    A DISTINCT failure from "this query matched nothing", and the distinction is
    the whole reason this class exists. DuckDuckGo's HTML endpoints now answer an
    anti-bot interstitial -- HTTP **202** carrying an "anomaly" page and no
    result markup at all -- to every request from this host, on every endpoint
    (`html.`/`lite.`), under both the donor's UA and a browser UA (measured
    2026-08-05). A 202 is a success code, so the donor's error handling never
    fires: `parse_ddg_html` simply finds no `result__a` anchors and returns `[]`.

    Reported as "0 candidates" that is indistinguishable from "no news exists
    about this case" -- which for THIS enricher is a catastrophic silent failure,
    because binding nothing is a perfectly normal and correct outcome. A run
    against 238 cases would have produced 238 empty rows and a green summary.
    Raised instead, so the run says the backend is down.
    """


class SearchBudgetExceeded(SearchUnavailable):
    """The run hit its cap on billable search queries.

    Deliberately a `SearchUnavailable` subclass: the enricher already treats
    that as "abort the whole run, keep what shipped", which is exactly right
    here. Continuing would write one "found nothing" row per remaining case --
    a review file that looks like a completed run and means nothing.
    """


#: Ledger of billable queries, so the cap survives across runs.
_DEFAULT_LEDGER = _REPO_ROOT / "work" / "search-budget.json"

#: Monthly cap applied when the operator names no other. Sits just under
#: Brave's $5 credit (~1,000 queries at $5/1,000) so the free allowance is
#: never silently overspent. Only used for KEYED providers -- see
#: `default_budget_for`.
DEFAULT_KEYED_BUDGET = 900

SEARCH_BUDGET_ENV = "CASEWORK_SEARCH_BUDGET"
SEARCH_LEDGER_ENV = "CASEWORK_SEARCH_LEDGER"

#: `(cap, period)` per keyed provider, measured against each vendor's published
#: free allowance on 2026-08-06 and set a little under it so the free tier is
#: never overspent by a rounding error.
#:
#: The PERIOD is what that vendor actually refreshes on, and getting it wrong is
#: not cosmetic. Serper's 2,500 credits are granted ONCE, so a month-keyed
#: counter would hand the whole allowance back every 1st and quietly overspend
#: it; Google's 100 is daily, so a monthly counter would refuse all month after
#: one busy afternoon. One shared counter across providers was the original bug:
#: they have separate quotas, so 300 Serper queries must not consume Brave's.
PROVIDER_QUOTAS = {
    "brave": (900, "month"),        # $5 credit ~= 1,000 queries at $5/1,000
    "serper": (2200, "once"),       # 2,500 free credits, NOT recurring
    "tavily": (900, "month"),       # 1,000 credits/month
    "google_cse": (90, "day"),      # 100/day
}


def default_budget_for(provider):
    """Query cap for `provider`; 0 means uncapped.

    The cap exists because a keyed provider draws on a paid or finite
    allowance. DuckDuckGo has neither -- it cannot bill anyone -- so capping it
    would only interrupt a legitimate long run for no benefit: a 238-case pass
    is ~2,900 queries and would trip any credit-sized limit.
    """
    if provider == DEFAULT_SEARCH_PROVIDER:
        return 0
    return PROVIDER_QUOTAS.get(provider, (DEFAULT_KEYED_BUDGET, "month"))[0]


def quota_period_for(provider):
    """`"month"`, `"day"` or `"once"` -- when `provider` restores its allowance."""
    return PROVIDER_QUOTAS.get(provider, (DEFAULT_KEYED_BUDGET, "month"))[1]


class SearchBudget:
    """A hard ceiling on one provider's queries, persisted across runs.

    A per-run limit is not enough. Brave's card is charged the moment the $5
    monthly credit runs out and Brave publishes no spending cap, so three
    accidental re-runs of a 300-query batch spend the month's allowance and
    then start billing.

    The ledger holds a SEPARATE count per provider, because their quotas are
    separate: spending Serper's one-time 2,500 must not consume Brave's monthly
    ~1,000. Each count is filed under the bucket its vendor refreshes on, so it
    resets when the real allowance does -- and `"once"` never resets, which is
    the whole point for Serper.

    Buckets are keyed in UTC, which is exact for `"month"` but approximate for
    the one `"day"` provider: Google CSE resets its 100/day at midnight
    US/Pacific, so a run between 00:00 and ~08:00 UTC sees a fresh local bucket
    while Google is still counting the previous day. The cap is then optimistic
    by up to the previous day's spend and Google, not the ledger, is what stops
    the run. Not worth a timezone dependency while `google_cse` stays a
    fallback provider -- but do not read the day bucket as authoritative.

    Written after every query rather than at the end of the run, because the
    runs this guards against are the ones that die badly -- a crash or a
    `Ctrl-C` must not roll the counter back to zero.
    """

    def __init__(self, limit, provider, path=None, period=None, now=None):
        self.limit = int(limit or 0)
        self.provider = provider
        self.period = period or quota_period_for(provider)
        self.path = Path(path or os.environ.get(SEARCH_LEDGER_ENV)
                         or _DEFAULT_LEDGER)
        # Injectable so the rollover branches are testable without waiting for
        # a day or a month to pass.
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.bucket = self._bucket()
        self.spent = self._load()

    def _bucket(self):
        if self.period == "once":
            return "once"
        if self.period == "day":
            return self._now().strftime("%Y-%m-%d")
        return self._now().strftime("%Y-%m")

    def _read_all(self):
        """Every provider's row, or `{}` if the file is absent or unreadable."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        rows = data.get("spent") if isinstance(data, dict) else None
        if isinstance(rows, dict):
            return rows
        if rows is not None:
            # A ledger we cannot read resets the counter to zero, which hands
            # back an allowance that may already be spent -- on Brave that is
            # the difference between the free credit and a charged card. It
            # must not be the quiet path.
            log.warning("the search budget ledger at %s is not in the expected "
                        "format, so this run starts from zero spent. Check it "
                        "against the provider's own usage page before a large "
                        "batch.", self.path)
        return {}

    def _load(self):
        row = self._read_all().get(self.provider)
        if not isinstance(row, dict) or row.get("bucket") != self.bucket:
            return 0          # a new period restores the whole allowance
        try:
            return max(0, int(row.get("spent") or 0))
        except (TypeError, ValueError):
            return 0

    def _save(self):
        # Read-modify-write rather than overwrite: two providers share this file
        # and a plain dump would erase whichever one is not running.
        rows = self._read_all()
        rows[self.provider] = {"bucket": self.bucket, "spent": self.spent}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"spent": rows}, indent=2))
        except OSError as exc:
            # An unwritable ledger must not kill a run that is otherwise fine,
            # but it silently removes the protection -- so say so, loudly, once
            # per failure rather than at the end.
            log.warning("could not write the search budget ledger at %s: %s. "
                        "The cap will not survive this process.", self.path, exc)

    def remaining(self):
        return None if not self.limit else max(0, self.limit - self.spent)

    def spend(self, n=1):
        """Charge `n` queries, or raise `SearchBudgetExceeded` before spending.

        Refuses BEFORE the request goes out, so the query that would breach the
        cap is never billed.
        """
        if self.limit and self.spent + n > self.limit:
            window = {"once": "in total (this allowance does not refresh)",
                      "day": f"today ({self.bucket})"}.get(
                          self.period, f"this month ({self.bucket})")
            raise SearchBudgetExceeded(
                f"{self.provider} search budget exhausted: {self.spent} of "
                f"{self.limit} queries already spent {window}. Nothing was sent. "
                f"Raise it with --search-budget, or reset by editing "
                f"{self.path}.")
        self.spent += n
        self._save()


#: Markers of the anti-bot interstitial described on `SearchUnavailable`. Matched
#: on the BODY, not the status, because the status is 202.
_ANOMALY_MARKERS = ("anomaly", "detected unusual activity", "verify you are")


#: Env var naming the backend. Default keeps the donor's behaviour exactly.
SEARCH_PROVIDER_ENV = "CASEWORK_SEARCH_PROVIDER"
DEFAULT_SEARCH_PROVIDER = "duckduckgo"


def _keyed(env_name, provider):
    """The API key for `provider`, or a `SearchUnavailable` explaining its absence.

    Raising beats falling back to DuckDuckGo. A silent downgrade would hand back
    the anti-bot page the operator configured a key precisely to escape, and the
    run would report zero candidates per case -- the exact silent failure
    `SearchUnavailable` exists to prevent.
    """
    key = (os.environ.get(env_name) or "").strip()
    if not key:
        raise SearchUnavailable(
            f"{SEARCH_PROVIDER_ENV}={provider} but ${env_name} is unset or empty. "
            f"Refusing to fall back to duckduckgo, which is blocked from this "
            f"host and would report every case as having no coverage.")
    return key


def _provider_results(status, body, provider, extract):
    """Normalise one keyed provider's reply, or raise `SearchUnavailable`.

    `extract` maps the decoded JSON to `[{title, url, snippet}]`. Anything that
    is not a clean 2xx with parseable JSON is the backend declining to answer,
    never an empty result set -- the distinction this whole module turns on.
    """
    if status is None:
        raise SearchUnavailable(f"{provider} did not answer (transport error).")
    if status == 401 or status == 403:
        raise SearchUnavailable(
            f"{provider} rejected the API key (HTTP {status}): {(body or '')[:200]}")
    if status == 429:
        raise SearchUnavailable(
            f"{provider} rate-limited or out of quota (HTTP 429): "
            f"{(body or '')[:200]}")
    if status >= 400 or not body:
        raise SearchUnavailable(
            f"{provider} returned HTTP {status}: {(body or '')[:200]}")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise SearchUnavailable(
            f"{provider} returned HTTP {status} with a body that is not JSON "
            f"({exc}). First 200 chars: {body[:200]!r}") from exc
    # Valid JSON is not necessarily an OBJECT. A proxy or error shim can answer
    # `[]` or a bare string with a 200, and every `extract` below calls `.get`
    # on this -- an AttributeError, which is NOT SearchUnavailable and so slips
    # past the enricher's abort handler and kills the run with a traceback
    # instead of "the backend is refusing to answer".
    if not isinstance(payload, dict):
        raise SearchUnavailable(
            f"{provider} returned HTTP {status} with JSON that is not an object "
            f"({type(payload).__name__}). First 200 chars: {body[:200]!r}")
    # The payload being an object does not make its INNARDS well-shaped. Each
    # `extract` walks `payload["web"]["results"]` and friends, and this loop then
    # calls `row.get`. A shim answering `{"web": {"results": ["..."]}}` raises
    # AttributeError from inside the lambda or from here -- which, not being
    # SearchUnavailable, bypasses the enricher's abort handler and kills the run
    # with a traceback. Same hazard as the top-level guard above, one level down.
    try:
        rows = extract(payload) or []
    except (AttributeError, TypeError) as exc:
        raise SearchUnavailable(
            f"{provider} returned JSON this code cannot walk ({exc}). "
            f"First 200 chars: {body[:200]!r}") from exc
    if not isinstance(rows, (list, tuple)):
        raise SearchUnavailable(
            f"{provider} returned a result set that is not a list "
            f"({type(rows).__name__}). First 200 chars: {body[:200]!r}")
    results = []
    for row in rows:
        if not isinstance(row, dict):
            raise SearchUnavailable(
                f"{provider} returned a result row that is not an object "
                f"({type(row).__name__}). First 200 chars: {body[:200]!r}")
        url = (row.get("url") or "").strip()
        if not url:
            continue
        results.append({"title": (row.get("title") or "").strip(),
                        "url": url,
                        "snippet": (row.get("snippet") or "").strip()})
        if len(results) >= SEARCH_RESULTS_PER_QUERY:
            break
    return results


def search_brave(client, query):
    """Brave Search API. GET, key in `X-Subscription-Token`.

    UNVERIFIED AGAINST A LIVE KEY -- no Brave/Serper/Tavily credential exists in
    this repo or environment, so the response shapes below come from each
    vendor's published schema and are covered only by recorded-fixture tests.
    Run one real query and check the count before trusting a bulk run.
    """
    key = _keyed("BRAVE_SEARCH_API_KEY", "brave")
    url = ("https://api.search.brave.com/res/v1/web/search?"
           + urllib.parse.urlencode({"q": query,
                                     "count": SEARCH_RESULTS_PER_QUERY}))
    status, body = client.get(url, "search", error_body=True,
                              headers={"Accept": "application/json",
                                       "X-Subscription-Token": key})
    return _provider_results(
        status, body, "brave",
        lambda p: [{"title": r.get("title"), "url": r.get("url"),
                    "snippet": r.get("description")}
                   for r in ((p.get("web") or {}).get("results") or [])])


def search_serper(client, query):
    """Serper.dev — Google's index as JSON. POST, key in `X-API-KEY`.

    See the caveat on `search_brave`: shape is from the vendor schema, untested
    against a live key.
    """
    key = _keyed("SERPER_API_KEY", "serper")
    status, body = client.post_json(
        "https://google.serper.dev/search", "search",
        {"q": query, "num": SEARCH_RESULTS_PER_QUERY, "gl": "np"},
        headers={"X-API-KEY": key})
    return _provider_results(
        status, body, "serper",
        lambda p: [{"title": r.get("title"), "url": r.get("link"),
                    "snippet": r.get("snippet")}
                   for r in (p.get("organic") or [])])


def search_tavily(client, query):
    """Tavily. POST, key as a bearer token.

    See the caveat on `search_brave`: shape is from the vendor schema, untested
    against a live key.
    """
    key = _keyed("TAVILY_API_KEY", "tavily")
    status, body = client.post_json(
        "https://api.tavily.com/search", "search",
        {"query": query, "max_results": SEARCH_RESULTS_PER_QUERY},
        headers={"Authorization": f"Bearer {key}"})
    return _provider_results(
        status, body, "tavily",
        lambda p: [{"title": r.get("title"), "url": r.get("url"),
                    "snippet": r.get("content")}
                   for r in (p.get("results") or [])])


#: Google's free tier. Small, but it is the only provider that can be signed up
#: for without a payment card, which is why it exists here.
GOOGLE_CSE_FREE_QUERIES_PER_DAY = 100


def search_google_cse(client, query):
    """Google Programmable Search, JSON API. GET, key + engine id in the query.

    TWO credentials, unlike every other provider: `GOOGLE_CSE_API_KEY` and
    `GOOGLE_CSE_CX`, the id of the Programmable Search Engine itself. A key
    without a cx returns 400, which would otherwise read as a mystery.

    The free tier is 100 queries/day. At `QUERY_LIMIT` = 12 that is 8 cases a
    day, so a bulk run WILL exhaust it partway through -- which is fine, because
    a case already carrying its news evidence gets budget 0 on the next run and
    is skipped. Resuming tomorrow continues where the quota stopped.

    Exhaustion must not read as a bad key: both arrive as HTTP 403 and the
    operator's next action is completely different (wait vs re-issue). Google
    puts the distinction in the body, so it is pulled out here rather than left
    to `_provider_results`.

    Shape is from the vendor schema and is UNVERIFIED against a live key.
    """
    key = _keyed("GOOGLE_CSE_API_KEY", "google_cse")
    cx = (os.environ.get("GOOGLE_CSE_CX") or "").strip()
    if not cx:
        raise SearchUnavailable(
            "GOOGLE_CSE_API_KEY is set but $GOOGLE_CSE_CX is not. Google needs "
            "both: the API key, and the id of the Programmable Search Engine to "
            "query (create one at programmablesearchengine.google.com and set it "
            "to search the entire web).")
    url = ("https://www.googleapis.com/customsearch/v1?"
           + urllib.parse.urlencode({
               "key": key, "cx": cx, "q": query,
               # `num` maxes out at 10 on this API; asking for more is a 400.
               "num": min(SEARCH_RESULTS_PER_QUERY, 10)}))
    status, body = client.get(url, "search", error_body=True,
                              headers={"Accept": "application/json"})
    lowered = (body or "").lower()
    if status == 403 and ("dailylimitexceeded" in lowered
                          or "quota" in lowered or "ratelimitexceeded" in lowered):
        raise SearchUnavailable(
            f"google_cse daily quota exhausted "
            f"({GOOGLE_CSE_FREE_QUERIES_PER_DAY}/day on the free tier). This is "
            f"NOT a bad key -- wait for the quota to reset and re-run; cases "
            f"already bound are skipped, so the run resumes where it stopped.")
    return _provider_results(
        status, body, "google_cse",
        # No "items" key at all is Google's genuine no-match, not an error.
        lambda p: [{"title": r.get("title"), "url": r.get("link"),
                    "snippet": r.get("snippet")}
                   for r in (p.get("items") or [])])


#: name -> callable(client, query) -> [{title, url, snippet}]. Every entry obeys
#: one contract: `[]` means the query genuinely matched nothing, and anything
#: else raises `SearchUnavailable`.
SEARCH_PROVIDERS = {
    "duckduckgo": lambda client, query: search_duckduckgo(client, query),
    "brave": search_brave,
    "serper": search_serper,
    "tavily": search_tavily,
    "google_cse": search_google_cse,
}


def resolve_search_provider(name=None):
    """`(name, callable)` for the configured backend. Raises on an unknown name."""
    chosen = (name or os.environ.get(SEARCH_PROVIDER_ENV)
              or DEFAULT_SEARCH_PROVIDER).strip().lower()
    if chosen not in SEARCH_PROVIDERS:
        raise SearchUnavailable(
            f"unknown search provider {chosen!r}. Set ${SEARCH_PROVIDER_ENV} to "
            f"one of: {', '.join(sorted(SEARCH_PROVIDERS))}.")
    return chosen, SEARCH_PROVIDERS[chosen]


def search(client, query, provider=None):
    """`[{title, url, snippet}]` for one query, from the configured backend.

    Returns `[]` only for a genuine no-match. Raises `SearchUnavailable` when the
    backend is refusing to serve results at all, so the caller can stop rather
    than record a silent zero per case.
    """
    _, fn = resolve_search_provider(provider)
    return fn(client, query)


def search_duckduckgo(client, query):
    """Scrape DuckDuckGo's HTML endpoint. The donor's backend, and the default.

    BLOCKED FROM DATACENTRE ADDRESSES. Measured 2026-08-06 from this host
    (Oracle Cloud, AS31898): 1 of 6 Nepali queries answered, the rest HTTP 202
    with an anti-bot page, and pacing does not help -- gaps of 45s and 90s were
    refused too. A browser User-Agent does not fix it either; the block keys on
    the address, not the identity. The same query from a residential connection
    returns results.

    The donor DID work: production's news materials were created 2026-06-08. It
    ran from an unblocked machine -- these sandboxes did not exist yet. So this
    is not a regression in the code and not necessarily a tightening upstream;
    the execution moved onto an address search engines refuse. Configure a keyed
    provider instead.

    Retries a transport error / 403 / 429 with the donor's exponential backoff
    (donor:322): `5 * 3**attempt`, i.e. 5s then 15s, on top of the client's own
    throttle. An anomaly page is NOT retried -- it is not transient, and 3
    attempts x 12 queries x N cases of pointless traffic is how a scraper earns a
    longer ban.
    """
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"
    for attempt in range(1, SEARCH_RETRY_MAX + 1):
        status, html = client.get(url, "search", headers=SEARCH_HEADERS)
        if html:
            lowered = html[:20000].lower()
            if any(marker in lowered for marker in _ANOMALY_MARKERS):
                raise SearchUnavailable(
                    f"the search backend served an anti-bot page (HTTP {status}) "
                    f"instead of results. Every query this run would return zero "
                    f"candidates, which is indistinguishable from 'no coverage "
                    f"exists' -- refusing to report that as a result.")
            return parse_ddg_html(html)
        if attempt < SEARCH_RETRY_MAX:
            delay = 5 * 3 ** (attempt - 1)
            log.warning("search attempt %d/%d for %r -> %s; retrying in %ds",
                        attempt, SEARCH_RETRY_MAX, query[:60], status, delay)
            # The client caches by (kind, url), so a bare retry would replay
            # the cached failure. Drop the entry so the retry is a real request.
            client.invalidate(url, "search")
            time.sleep(delay)
    # Every attempt came back with no body at all -- a transport error, a 403 or a
    # 429. That is the backend declining to answer, NOT a query that matched
    # nothing, and returning `[]` here collapsed the two: a host blocked with 403
    # on every request produced one "no article cleared the verification gate"
    # NOOP per case and a green summary across all 238. The anomaly-page check
    # above only catches the ONE signature measured on 2026-08-05 (a 202 body);
    # this covers every other way the backend can refuse.
    raise SearchUnavailable(
        f"the search backend did not answer after {SEARCH_RETRY_MAX} attempts "
        f"(last status {status!r}). Zero candidates from an unanswered query is "
        f"indistinguishable from 'no coverage exists' -- refusing to report that "
        f"as a result.")


def fetch_article(client, candidate):
    """`(Article, None)` or `(None, SkipReason)` for one search result.

    Deviation A lives here: an article whose publication date cannot be read is
    returned as `SkipReason.NO_DATE`, not dated to today.
    """
    url = candidate["url"]
    reason = screen_url(url)
    if reason:
        return None, reason

    status, html = client.get(url, "fetch", headers=FETCH_HEADERS, expect_html=True)
    if html is None:
        return None, (SkipReason.NOT_HTML if status and 200 <= status < 300
                      else SkipReason.FETCH_FAILED)

    text = extract_text_from_html(html)
    title = extract_title_from_html(html) or candidate.get("title") or ""
    reason = screen_body(text, title, url)
    if reason:
        return None, reason

    published = extract_publication_date(html)
    if published is None:
        return None, SkipReason.NO_DATE
    return Article(url=url, title=title, text=text, published=published,
                   snippet=candidate.get("snippet") or ""), None


# ---------------------------------------------------------------------------
# Web-archive permalink. From the permalinks donor.
# ---------------------------------------------------------------------------


def is_archive_url(url):
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    return any(host == h or host.endswith("." + h) for h in ARCHIVE_HOSTS)


def closest_snapshot(client, url, *, fresh=False):
    """The closest existing Wayback snapshot of `url`, or None.

    Normalises the returned scheme to https -- the availability API sometimes
    answers http (add_news_permalinks:228).

    `fresh=True` drops any cached answer first. Required after a Save Page Now
    request: the client caches by (kind, url), so the post-save re-query replayed
    the pre-save MISS and the whole `save_missing` branch could never return a
    permalink. Every article that was not already archived silently got none,
    having paid a 6s-throttled SPN request for it.
    """
    query = urllib.parse.urlencode({"url": url})
    availability_url = f"{AVAILABILITY_API}?{query}"
    if fresh:
        client.invalidate(availability_url, "archive")
    status, body = client.get(availability_url, "archive",
                              headers={"User-Agent": ARCHIVE_UA})
    if status != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    closest = ((data.get("archived_snapshots") or {}).get("closest") or {})
    if closest.get("available") and closest.get("url"):
        return closest["url"].replace("http://web.archive.org",
                                      "https://web.archive.org", 1)
    return None


def resolve_permalink(client, url, *, save_missing=True):
    """A stable archival copy of `url`, or None.

    Existing snapshot first, else a Save Page Now capture (throttled by the
    client's `save` delay, because SPN is rate-limited -- add_news_permalinks:14).
    An already-archival URL needs no permalink and returns None.

    A MISS IS NOT FATAL. The permalinks donor reported an unarchivable source
    rather than refusing it, and so does this: the RAW link is still the
    citation, the permalink is insurance against link rot. Wayback answered 429
    during this port's own checks, which is exactly the transient this must
    survive.
    """
    if is_archive_url(url):
        return None
    snapshot = closest_snapshot(client, url)
    if snapshot or not save_missing:
        return snapshot
    status, _ = client.get(SAVE_BASE + url, "save", headers={"User-Agent": ARCHIVE_UA})
    # SPN returns the capture location in a header urllib does not expose
    # through `get`, so re-query availability rather than parse the response --
    # the same fallback the donor used when the header was absent
    # (add_news_permalinks:262). `fresh=True` is load-bearing: without it this
    # replays the cached pre-save miss and always answers None.
    return closest_snapshot(client, url, fresh=True)


# ---------------------------------------------------------------------------
# Query generation. Donor-verbatim (donor:433-797).
# ---------------------------------------------------------------------------

ROMANIZATION_REPLACEMENTS = (
    ("क्ष", "ksh"), ("त्र", "tr"), ("ज्ञ", "gy"), ("श्र", "shr"),
    ("अ", "a"), ("आ", "aa"), ("इ", "i"), ("ई", "ee"), ("उ", "u"), ("ऊ", "oo"),
    ("ए", "e"), ("ऐ", "ai"), ("ओ", "o"), ("औ", "au"),
    ("क", "k"), ("ख", "kh"), ("ग", "g"), ("घ", "gh"), ("ङ", "n"),
    ("च", "ch"), ("छ", "chh"), ("ज", "j"), ("झ", "jh"), ("ञ", "n"),
    ("ट", "t"), ("ठ", "th"), ("ड", "d"), ("ढ", "dh"), ("ण", "n"),
    ("त", "t"), ("थ", "th"), ("द", "d"), ("ध", "dh"), ("न", "n"),
    ("प", "p"), ("फ", "ph"), ("ब", "b"), ("भ", "bh"), ("म", "m"),
    ("य", "y"), ("र", "r"), ("ल", "l"), ("व", "w"),
    ("श", "sh"), ("ष", "sh"), ("स", "s"), ("ह", "h"),
    ("ा", "a"), ("ि", "i"), ("ी", "i"), ("ु", "u"), ("ू", "u"),
    ("े", "e"), ("ै", "ai"), ("ो", "o"), ("ौ", "au"),
    ("ं", "n"), ("ँ", "n"), ("ः", ""), ("्", ""),
)


def romanize_devanagari(text):
    """The donor's hand table, NOT `indic-transliteration`.

    The repo does depend on `indic-transliteration` for search, but this table
    is what generated the queries that found the articles now bound in
    production, and a different romanisation is a different query set. Kept
    verbatim (donor:501); it is only reached for the FALLBACK English queries,
    since the primary English queries come from the cheap-tier LLM pass.
    """
    romanized = text
    for devanagari, roman in ROMANIZATION_REPLACEMENTS:
        romanized = romanized.replace(devanagari, roman)
    romanized = re.sub(r"[^A-Za-z0-9\s\"-]", " ", romanized)
    return re.sub(r"\s+", " ", romanized).strip()


def is_english_query(query):
    return not DEVANAGARI_RE.search(query) and bool(re.search(r"[A-Za-z]", query))


def with_nepal_keyword(query):
    """Nepali/English `Nepal` disambiguator -- a bare name matches the world."""
    if re.search(r"(?:\bNepal\b|नेपाल)", query, flags=re.IGNORECASE):
        return query
    return f"{query} {'नेपाल' if DEVANAGARI_RE.search(query) else 'Nepal'}"


def _dedupe(queries):
    return list(dict.fromkeys(queries))


def normalize_search_queries(queries):
    """Add the Nepal keyword, then interleave English-first (donor:523)."""
    normalized = [with_nepal_keyword(q) for q in queries]
    english = [q for q in normalized if is_english_query(q)]
    devanagari = [q for q in normalized if q not in english]
    return english[:4] + devanagari + english[4:]


def resolve_case_number(case):
    """The case's court-case number, or None.

    THE DONOR'S VERSION IS DEAD ON CURRENT DATA. It split `court_cases` entries
    on ":" and took the tail (donor:540), which assumed a `special:081-CR-0121`
    reference. Entries are now full IRIs
    (`https://.../courtcase/special/081-cr-0091`), so the donor's split returns
    the whole `https` scheme fragment. `casework/common/select.court_number`
    already reads the current shape and is used instead; `enrich_timeline`
    documents the same dead reference. UPPERCASED because the IRI is lowercase
    and this string goes into a prompt that is told to prefer specifics from
    context -- the same defect fixed in `enrich_description._generate_description`.
    """
    from casework.common.select import court_number

    return (court_number(case) or "").upper() or None


def accused_names(case):
    """Accused display names, with the donor's title fallback (donor:548)."""
    names = []
    for entity in case.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("type") != "accused":
            continue
        name = (entity.get("display_name") or entity.get("nes_id") or "").strip()
        if name:
            names.append(name)
    if names:
        return names[:5]

    title = case.get("title") or ""
    match = re.search(r"(?:विरुद्ध|vs\.?|versus)\s+(.{1,200})(?:\s+मुद्दा|\s+मा\.?\s|$)",
                      title) if title else None
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


_ORG_SUFFIXES = ("सहकारी", "संस्था", "कम्पनी", "स्कुल", "कलेज", "अस्पताल", "बैंक",
                 "विकास बैंक", "फाइनान्स", "जलस्रोत", "खानेपानी", "उपभोक्ता समिति",
                 "विद्युत", "सिंचाइ", "निर्माण सेवा")


def extract_org_name_from_title(title):
    for suffix in sorted(_ORG_SUFFIXES, key=len, reverse=True):
        match = re.search(rf"(\S{{2,60}}\s*{re.escape(suffix)})", title or "")
        if match:
            return match.group(1).strip()
    return ""


def extract_location_from_title(title):
    """In Nepali the place name PRECEDES the administrative division
    ("काठमाडौं महानगरपालिका"), so that order is tried first (donor:610)."""
    title = title or ""
    before = re.search(
        r"(\S{1,50})\s*(?:महानगरपालिका|उपमहानगरपालिका|नगरपालिका|गाउँपालिका|जिल्ला)", title)
    if before:
        return before.group(1)
    after = re.search(r"कार्यालय\s+(\S{1,50})", title)
    if after:
        return after.group(1)
    match = re.search(r"(\S{1,50})(?:को|का|मा)\s+(?:नापी|मालपोत|स्वास्थ्य)", title)
    return match.group(1) if match else ""


def extract_title_keywords(title):
    if not title:
        return ""
    parts = re.split(r"[,।\n]", title)
    if len(parts) > 1 and len(parts[0].strip()) > 10:
        return parts[0].strip()[:80]
    cleaned = re.sub(r"\b(?:मुद्दा|विरुद्ध|सम्बन्धी|सम्बन्धमा|मा\.?)\b", "", title)
    return re.sub(r"\s+", " ", cleaned).strip()[:100]


_CORRUPTION_TERMS = ("घुस", "रिश्वत", "भ्रष्टाचार", "अवैध सम्पत्ति", "हिनामिना",
                     "पद दुरुपयोग", "किर्ते", "नक्कली", "बिगो", "अख्तियार",
                     "विशेष अदालत", "bribery", "corruption", "illegal property",
                     "embezzlement", "forgery", "abuse of authority")


def extract_corruption_keywords(key_allegations):
    text = " ".join(key_allegations or []).lower()
    return [t for t in _CORRUPTION_TERMS if t.lower() in text][:3]


def _clean(name):
    return re.sub(r"\s+", " ", name or "").strip()


def timeline_events(case):
    """`{event_type: (ad_year, bs_year)}` for the stages this case reached.

    Read off the case's own timeline, which nothing in the query builder used
    to look at. Two things come out of it, both measured against production:

    * WHICH stages to ask about. 19 of 61 published cases carry a verdict in
      their timeline that no bound article covers, while hearing coverage is 2
      articles in 169 -- so slots spent on stages a case never reached are pure
      waste, and the verdict is where the missing coverage actually is.
    * WHICH YEAR to ask about. `{name} फैसला विशेष अदालत` competes with every
      year of that person's coverage; the timeline knows the verdict landed in
      2082 BS / 2026 AD.

    The latest entry per stage wins: a case that was heard three times is best
    found by its most recent hearing, and re-tries can fall back to the rest.
    "Latest" is decided on the WHOLE date, not the year: BS rolls over in
    mid-April, so two verdict entries in the same AD year can sit in different
    BS years and picking the wrong one narrows the query onto a year the story
    was not filed under.

    A stage counts as reached whether or not its entry carries a usable date.
    The date is needed for the year term and nothing else, so requiring one
    would drop the stage ORDERING too -- and an undated `विशेष अदालतको ठहर`
    entry is still proof the case reached a verdict.
    """
    found = {}
    for entry in (case.get("timeline") or []):
        text = f"{entry.get('title') or ''} {entry.get('description') or ''}"
        for event_type, words in TIMELINE_EVENT_WORDS.items():
            if not any(word in text for word in words):
                continue
            ad_date = (entry.get("date") or "")
            ad, bs = ad_date[:4], (entry.get("date_bs") or "")[:4]
            previous = found.get(event_type)
            if previous is None or ad_date >= previous[0]:
                found[event_type] = (ad_date,
                                     ad if ad.isdigit() else "",
                                     bs if bs.isdigit() else "")
            break          # first (most specific) stage wins for this entry
    # The full date was only needed to pick a winner; callers want the years.
    return {event: (ad, bs) for event, (_sort_key, ad, bs) in found.items()}


def _stage_priority(case):
    """Lifecycle stages, the ones this case reached first, most advanced first.

    The verdict is both the least-covered stage in production and the most
    newsworthy, so on a decided case it must not queue behind an investigation
    query. Stages the timeline does not mention still follow, in lifecycle
    order -- an empty timeline is the whole DRAFT backlog and must keep the
    full spread.
    """
    reached = timeline_events(case)
    ordered = sorted(reached, key=lambda e: EVENT_LIFECYCLE_ORDER.get(e, 9),
                     reverse=True)
    return ordered + [e for e in ALL_EVENT_TYPES if e not in reached], reached


def _order_by_stage(queries, case, words_by_stage):
    """`queries` re-sorted so the stages this case reached come first.

    Stable, and anything that matches no stage keeps its position at the back
    rather than being dropped -- a query the map does not recognise is still a
    query, and the map is a keyword heuristic, not an authority.
    """
    if not queries:
        return []
    ordered, _reached = _stage_priority(case)
    rank = {event: index for index, event in enumerate(ordered)}

    def key(item):
        index, query = item
        lowered = query.lower()
        for event, words in words_by_stage.items():
            if any(word in lowered for word in words):
                return (rank.get(event, len(rank)), index)
        return (len(rank), index)

    return [q for _, q in sorted(enumerate(queries), key=key)]


def _event_queries(primary, case, devanagari_primary=""):
    """Event queries, BREADTH FIRST: one per stage before any stage gets two.

    The ordering is the load-bearing part. `build_queries` reserves
    `QUERY_RESERVED_EVENT_SLOTS` (4) slots for this list and truncates the rest
    away, so emitting all of a stage's templates before moving to the next
    stage spends every slot on one or two stages. Measured on a case with a
    verdict in its timeline, that put 5 verdict queries in the 12 sent and
    dropped filing and hearing entirely -- while `मुद्दा दायर` (filing) is in
    57% of article bodies, the single most common phrase in the corpus.

    Round-robin instead: the first pass takes each stage's best template, the
    second pass its next, and so on. The 4 reserved slots therefore always
    cover 4 DIFFERENT stages, led by whichever one the timeline points at.

    The breadth defect above is DETERMINISTIC -- read off the sent list, no
    provider involved. The recall effect is not measurable on the labelled set:
    four orderings run against Serper through one shared cache on 2026-08-06
    scored 7/6/6/6 of 10, and the same configuration scored 5, 6 and 7 across
    three runs the same afternoon, because several hits sit at rank 7 of 8 and
    Serper's ranking drifts hour to hour. Ten cases cannot resolve a difference
    of one. This ordering is chosen for the defect it provably fixes, NOT on a
    claim that it finds more -- and the same caveat applies to the
    corpus-measured template set, which has never been shown to improve recall.

    A Devanagari template gets the Devanagari name when one survived the
    skeleton check, and the Latin name otherwise. Mixing scripts inside one
    query -- "Bikal Poudel विशेष अदालत ठहर" against an article that says
    "विकल पौडेल" -- is what left 3 findable articles unfound.
    """
    if not primary or len(primary) < 3:
        return []
    ordered, reached = _stage_priority(case)

    by_stage = {}
    for event_type in ordered:
        templates = (EVENT_QUERY_TEMPLATES_MEASURED.get(event_type, [])
                     + EVENT_QUERY_TEMPLATES.get(event_type, []))
        queries = []
        for index, template in enumerate(templates):
            # A Devanagari template needs a Devanagari name; an English one
            # must keep the Latin. Decided per template, not per case, because
            # both kinds sit in the same list.
            name = (devanagari_primary
                    if devanagari_primary and DEVANAGARI_RE.search(template)
                    else primary)
            base = template.format(name=name)
            queries.append(base)
            year = _year_term(reached.get(event_type))
            # One year-qualified variant, on the FIRST template of a stage that
            # is under-covered. Measured share of article bodies carrying the
            # BS year at all is 33%, so a year narrows the query for a third of
            # the corpus and excludes the rest -- it earns a slot beside the
            # plain query, never instead of it, and not on every template.
            #
            # Being a round-2 entry, it reaches the wire only when the front
            # block leaves room -- in practice on cases whose title yields no
            # location, organisation or keywords, so `general` is short. That
            # is the right way round: where the title IS rich, a location query
            # is the better use of the slot. Still UNMEASURED either way; every
            # labelled pair has `published=None`, so no run has exercised it.
            if year and index == 0 and event_type in YEAR_QUALIFIED_EVENTS:
                queries.append(f"{base} {year}")
        by_stage[event_type] = queries

    interleaved = []
    for round_index in range(max((len(q) for q in by_stage.values()), default=0)):
        for event_type in ordered:
            queries = by_stage[event_type]
            if round_index < len(queries):
                interleaved.append(queries[round_index])
    return interleaved


#: Stages worth spending a year-qualified slot on. Verdict and appeal are where
#: production coverage is missing (19 of 61 published cases have an uncovered
#: verdict) and where a bare name query loses to the filing-day story that got
#: an order of magnitude more pickup.
YEAR_QUALIFIED_EVENTS = (EVENT_VERDICT, EVENT_APPEAL)


def _year_term(years):
    """The BS year in Devanagari digits, which is how Nepali reports print it.

    Measured over the 135 published-case articles with body text: the BS year
    appears as `२०८२` in 33% and as `2082` in 9%. The AD year is NOT used --
    it matched 100% of pages, which is page furniture (datelines, copyright
    footers) rather than the story, so as a query term it discriminates
    nothing while still costing a slot.
    """
    if not years:
        return ""
    bs = years[1]
    return bs.translate(_ASCII_TO_DEVANAGARI_DIGITS) if bs else ""


def build_queries(case, llm_english_queries=None, devanagari_names=None):
    """Up to `QUERY_LIMIT` search queries for one case. Donor-verbatim (donor:757).

    Prioritises accused name + location + corruption keywords over the case
    number, which mostly surfaces court/admin pages rather than newsrooms.
    Measured against the 138 articles bound to published cases: the case number
    appears in 0 of 138 headlines, so it earns no slot; the accused's name
    reaches the article BODY in 89%, so it earns every slot it has.
    """
    title = case.get("title") or ""
    names = accused_names(case)
    primary = _clean(names[0]) if names else ""

    # Keyed by the NES name (see `generate_devanagari_names`), under both the
    # raw and the cleaned spelling so either lookup lands.
    devanagari_by_name = {}
    for raw, spelling in (devanagari_names or {}).items():
        devanagari_by_name[raw] = spelling
        devanagari_by_name[_clean(raw)] = spelling

    def in_devanagari(clean_name):
        """The Devanagari spelling for a Devanagari template, else the Latin.

        Every Devanagari template needs this, not just the event ones -- the
        `general` block below is half Devanagari too, and leaving the Latin
        name in those queries reproduces the exact mixed-script miss the
        Devanagari names exist to fix.
        """
        return devanagari_by_name.get(clean_name, clean_name)

    devanagari_primary = devanagari_by_name.get(primary, "") if primary else ""
    events = _event_queries(primary, case, devanagari_primary)

    # The two short name queries come FIRST, with the model's queries after --
    # not instead of. Measured on the labelled set, Serper, 2026-08-06: the
    # model's queries alone scored 5/10 against the short queries' 6/10,
    # because it over-specifies ("Nepal CIAA investigation Nepal Airlines
    # A330-200 widebody aircraft purchase Jiban...") and the plain "Jiban
    # Bahadur Shahi CIAA Nepal corruption" that had found the article at rank 7
    # was no longer being sent. But the model also found the one article no
    # name query reaches -- the Melamchi case, via the PROJECT name and the
    # contractor. The two are complementary and the budget has room for both.
    #
    # EXCEPT when the NES name is itself Devanagari. `romanize_devanagari` is a
    # lossy hand table -- "जीवन बहादुर शाही" comes back "jiwn bhadur shahi",
    # the exact defect the model's own prompt calls out ("Bahadur not
    # wahadur") -- so promoting it would spend two of the four English slots on
    # a misspelling and displace the model's correct romanisation. There it
    # stays the donor's last resort, used only if the model returned nothing.
    llm_english = _order_by_stage(list(llm_english_queries or []), case,
                                  EVENT_WORDS_EN)
    roman = romanize_devanagari(primary)
    short = ([f"{roman} CIAA Nepal corruption", f"{roman} Nepal special court case"]
             if roman and len(roman) >= 3 else [])
    if primary and DEVANAGARI_RE.search(primary):
        english = llm_english + short
    else:
        english = short + llm_english

    general = []
    location = extract_location_from_title(title)
    org = extract_org_name_from_title(title)
    for index, name in enumerate(names[:3]):
        clean = _clean(name)
        if not clean or len(clean) < 3:
            continue
        if index == 0:
            if location:
                general += [f'"{in_devanagari(clean)}" {location} भ्रष्टाचार',
                            f"{in_devanagari(clean)} {location} अख्तियार"]
        else:
            general.append(f"{clean} CIAA corruption Nepal")
    if org:
        general += [f"{org} भ्रष्टाचार", f"{org} अख्तियार"]
    keywords = extract_title_keywords(title)
    if keywords:
        general.append(f"{keywords} भ्रष्टाचार")
    for name in names[:2]:
        clean = _clean(name)
        if clean and len(clean) >= 3:
            for keyword in extract_corruption_keywords(case.get("key_allegations"))[:2]:
                # The keyword decides the script: `घुस Nepal` is Devanagari.
                who = in_devanagari(clean) if DEVANAGARI_RE.search(keyword) else clean
                general.append(f"{who} {keyword} Nepal")
    if primary and len(primary) > 3 and location:
        general += [f"{in_devanagari(primary)} {location} भ्रष्टाचार",
                    f"{in_devanagari(primary)} {location} अख्तियार"]

    english, events, general = _dedupe(english), _dedupe(events), _dedupe(general)
    n_english = min(QUERY_RESERVED_ENGLISH_SLOTS, len(english))
    n_events = min(QUERY_RESERVED_EVENT_SLOTS, len(events))
    combined = (english[:n_english] + events[:n_events]
                + general[:QUERY_LIMIT - n_english - n_events]
                + events[n_events:] + english[n_english:])
    return normalize_search_queries(_dedupe(combined))[:QUERY_LIMIT]


def fallback_queries(case, attempt):
    """Broader queries for retry `attempt` (0-2). Donor-verbatim (donor:1383)."""
    names = accused_names(case)
    title = case.get("title") or ""
    if attempt == 0:
        queries = [f'"{_clean(n)}" Nepal' for n in names[:2]
                   if _clean(n) and len(_clean(n)) >= 3]
        if title and len(title) > 10:
            queries.append(title[:100])
        return queries[:5]
    if attempt == 1:
        queries = []
        for name in names[:2]:
            clean = _clean(name)
            if clean and len(clean) >= 3:
                queries += [f"{clean} corruption Nepal", f"{clean} भ्रष्टाचार"]
        return queries[:5]
    keywords = extract_title_keywords(title)
    if keywords:
        return [f"{keywords} Nepal"]
    if names and _clean(names[0]) and len(_clean(names[0])) >= 3:
        return [f"{_clean(names[0])} Nepal"]
    return ["CIAA Nepal corruption"]


# ---------------------------------------------------------------------------
# Verification. The defamation guard.
# ---------------------------------------------------------------------------

GATE_SYSTEM_PROMPT = """\
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

# Donor-verbatim (donor:152) except the CONFIDENCE RULES block, which is new and
# is what makes deviation B's `high` bar meaningful: the donor asked for a
# confidence label without ever saying what earned each level, so "high" was
# whatever the model felt. The three levels below restate the donor's own
# evidence rubric (donor:173-175) as the definition of the label.
VERIFY_SYSTEM_PROMPT = """\
You are a fact-checking assistant for a Nepal corruption accountability platform. You
are given ONE CIAA Special Court corruption case and a NUMBERED LIST of candidate news
articles. For EACH candidate, decide whether it is genuinely about the SAME case.

Respond with ONLY a JSON object containing exactly one result per candidate index:
{"results": [
  {"index": 0, "relevant": true, "confidence": "high|medium|low", "reason": "<English>", "event_type": "investigation|filing|hearing|verdict|appeal|other", "summary": "<Nepali (Devanagari) evidence note, 350-500 characters>"},
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
- If the article is about a different corruption case involving the same person, it is NOT relevant. This is the single most common error: a senior official often faces several separate CIAA cases, and an article about another of them is not evidence for this one.
- If the article is about the same person but not about corruption allegations, it is NOT relevant.
- If a candidate excerpt is mostly navigation menus, category listings, or site boilerplate with only a headline and one sentence of real content, return relevant=false with reason "insufficient article content — likely paywalled or thin page".
- When relevant=true, BOTH "event_type" and "summary" are REQUIRED. event_type must never be an empty string (use "other" if unsure); summary must be in Nepali (Devanagari).
- When relevant=false, omit event_type and summary.

CONFIDENCE RULES — only "high" is ever attached to this case as evidence, so grade strictly:
- "high": the article identifies THIS case unambiguously. Either it states the court case number, or it matches on a combination that could not describe a different case — the same institution AND the same scheme AND a बिगो/loss figure or defendant set that matches this case's.
- "medium": the accused's name and a corruption allegation match, but nothing rules out a DIFFERENT case involving the same person or a similarly-named person. A shared surname, a shared scheme type, or a shared institution alone is medium, not high.
- "low": only weak or circumstantial overlap.
When you cannot tell "high" from "medium", answer "medium". A missed article costs a caseworker five minutes; a wrongly attached one publicly links named people to a case they may have nothing to do with.

SUMMARY — write the Nepali evidence note a caseworker would file, matching this register:
"यो समाचार लेख यस मुद्दा (080-CR-0136) मा अभियोग दायर भएको तथ्य र आयोगका प्रमुख दाबीहरू — रु. ३ करोड २१ लाख स्रोत नखुलेको सम्पत्ति आर्जन — लाई पुष्टि गर्ने पूरक सामग्रीका रूपमा रहन्छ। यो प्राथमिक कानूनी अभिलेख नभई सञ्चारमाध्यमको प्रतिवेदन भएकोले सम्बद्ध आरोपलाई समर्थन गर्ने गौण प्रमाणका रूपमा यसको भूमिका रहन्छ।"
State the case number, what specifically the article confirms (amounts, named
defendants, ऐन/दफा, the outcome), and close by placing its evidentiary weight —
whether it is a primary record (a verdict it reports directly) or secondary
journalistic corroboration. Ground every claim in the excerpt; invent nothing.

DATES IN THE SUMMARY — this note is stored permanently, so a date that only made sense on the day the article ran is worthless to the caseworker who reads it in three years:
- NEVER copy a relative day from the article. "आइतबारदेखि बहस सुरु भयो" tells a later reader nothing. Resolve it against the candidate's "Published" line and write the actual date.
- ALWAYS carry the year. "असार १८ गते" without a year is unusable; "२०८१ असार १८ गते" is a fact.
- For the article's own publication date, write Bikram Sambat first with Gregorian in brackets: २०८१ असार २ (16 June 2024). The "Published" line gives you both calendars — COPY from it. Do not convert between AD and BS yourself, and do not infer a BS date the line does not state. If "Published" is unknown and the article gives only a relative day, omit the date rather than guessing one.
- For a date the ARTICLE ITSELF states (a filing date, a hearing date, a verdict date), keep whichever calendar the article used and do NOT convert it — but always write it in Devanagari: "सन् २०२४ फेब्रुअरी १८", never "18 February 2024". A Latin-script date in a Nepali note is a defect.
"""

ENGLISH_QUERY_SYSTEM_PROMPT = (
    "You are a Nepal-focused news search assistant. Output only clean search queries.")

DEVANAGARI_NAME_SYSTEM_PROMPT = (
    "You transcribe Nepali personal names into Devanagari for search. Copy the "
    "spelling from the case text you are given whenever it appears there; do "
    "not invent a spelling you cannot see. If you are unsure of a name, omit "
    "it -- a missing entry costs one search query, a wrong one sends the whole "
    "search after the wrong person. Output only the JSON object asked for.")

# Donor excerpt budgets for the verify prompt (donor:1010).
VERIFY_EXCERPT_CHARS = 900
VERIFY_EXCERPT_CHARS_DEVANAGARI = 700


def trim_excerpt(text, max_chars=VERIFY_EXCERPT_CHARS,
                 devanagari_max=VERIFY_EXCERPT_CHARS_DEVANAGARI):
    """Donor-verbatim (donor:840): a shorter cap for Devanagari-dominant text,
    which costs more tokens per character."""
    text = text or ""
    return text[:devanagari_max] if DEVANAGARI_RE.search(text) else text[:max_chars]


def build_case_context(case, press_release_text=None):
    """The case block prepended to every gate/verify prompt (donor:981)."""
    allegations = case.get("key_allegations") or []
    context = (
        f"Case Title: {case.get('title') or 'Unknown'}\n"
        f"Court Case Number: {resolve_case_number(case) or 'Unknown'}\n"
        f"Short Description: {case.get('short_description') or 'Not provided'}\n"
        f"Key Allegations: "
        f"{', '.join(allegations[:5]) if allegations else 'None'}"
    )
    if press_release_text:
        context += ("\n\nPress Release Text (official CIAA document):\n"
                    f"{press_release_text[:1200]}")
    else:
        context += "\n\nNo official press release text available."
    return context


def _llm_json(invoke_json, system, content, max_tokens, tier, usage):
    """One LLM call. Returns `(result_dict_or_None, error_string_or_empty)`.

    THE ERROR IS RETURNED, NOT SWALLOWED. The donor logged the exception at debug
    and returned None (donor:925), which makes a provider outage arrive at the
    caller as an empty verdict set -- indistinguishable from the model answering
    "not relevant". For this stage that difference is everything: "no article is
    about this case" is a normal, correct, extremely common outcome, so a failed
    call that reads as a rejection turns a broken run into a plausible-looking
    one. Measured on this host, the `claude_cli` provider fails a premium call
    often enough for that to be the likely reading of any zero.

    Logged at WARNING too, so an operator watching a run sees it live rather than
    reading it off a summary afterwards.
    """
    try:
        result = invoke_json(system=system, content=content, max_tokens=max_tokens,
                            tier=tier, usage=usage)
    except Exception as exc:  # noqa: BLE001 -- reported, not raised: see docstring
        log.warning("%s-tier LLM call failed: %s: %s", tier, type(exc).__name__, exc)
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(result, dict):
        log.warning("%s-tier LLM returned %s, not a JSON object",
                    tier, type(result).__name__)
        return None, f"response was {type(result).__name__}, not a JSON object"
    return result, ""


def generate_english_queries(case, invoke_json, usage):
    """Cheap-tier romanised English queries, or `[]` (donor:941).

    The cheap tier romanises Nepali names correctly where the hand table above
    does not ("Bahadur", not "wahadur").
    """
    names = accused_names(case)
    prompt = (
        "Generate 5 English search queries to find Nepali news articles "
        "about this CIAA corruption case. One query per event type: "
        "investigation, chargesheet filing, court hearing, verdict, appeal. "
        "Use correct English romanization of Nepali names (e.g. Bahadur not wahadur). "
        "Include the word Nepal in each query. "
        'Respond with ONLY: {"queries": ["q1", "q2", "q3", "q4", "q5"]}\n\n'
        f"Case Title: {case.get('title') or 'Unknown'}\n"
        f"Accused: {', '.join(names[:3]) if names else 'Unknown'}"
    )
    # A failure here is genuinely harmless: `build_queries` falls back to the
    # romanisation table, so the run loses query QUALITY, not correctness. This
    # is the one `_llm_json` caller that can ignore the error string.
    result, _error = _llm_json(invoke_json, ENGLISH_QUERY_SYSTEM_PROMPT, prompt, 300,
                              "cheap", usage)
    queries = (result or {}).get("queries")
    if not isinstance(queries, list):
        return []
    return [q for q in queries
            if isinstance(q, str) and is_english_query(q) and len(q) > 10][:5]


#: Letters Nepali romanisation renders interchangeably, folded to one symbol so
#: "Bikal"/"wikl" and "Bisht"/"wist" compare equal.
#:
#: `c` is deliberately NOT folded to `k`. `romanize_devanagari` spells च as
#: "ch" and ख as "kh", so the fold made those two identical and
#: `devanagari_names_match("Chandra Khadka", "खन्द्र छड्का")` returned True --
#: a swapped-syllable name passing the only guard against a hallucinated one.
#: Dropping it costs nothing: "Chandra" and "chndr" already share a skeleton.
_SKELETON_FOLD = {"w": "b", "v": "b", "f": "p", "z": "j", "x": "s", "q": "k"}


#: Titles NES carries in `display_name` but a news report does not print, and
#: which `generate_devanagari_names`' prompt tells the model to drop. Stripped
#: before comparing, or the model's correct answer for "Dr. Bikal Poudel" reads
#: as two consonants short and the guard rejects it.
_HONORIFIC_RE = re.compile(
    r"^(dr|mr|mrs|ms|miss|prof|er|adv|sri|shri|hon)\b\.?\s*", re.IGNORECASE)


def name_skeleton(name):
    """Consonants only, interchangeable letters folded, doubles collapsed.

    The only reliable way to compare a NES Latin name with a Devanagari one.
    Both transliteration directions are lossy -- `romanize_devanagari` drops
    inherent vowels ("गजेन्द्र महर्जन" -> "gjendr mhrjn") and ITRANS the other
    way emits "Bइकल् Pओउदेल्" -- but the consonant sequence survives both, so
    "Gajendra Maharjan" and its Devanagari spelling both reduce to "gjndrmhrjn".
    """
    name = _HONORIFIC_RE.sub("", (name or "").strip())
    stripped = re.sub(r"[^a-z]", "", name.lower())
    folded = "".join(_SKELETON_FOLD.get(ch, ch) for ch in stripped
                     if ch not in "aeiou")
    return re.sub(r"(.)\1+", r"\1", folded)


def devanagari_names_match(latin, devanagari):
    """Is `devanagari` a plausible spelling of the Latin `latin`?

    The guard on the LLM. A hallucinated or drifted name would produce queries
    that quietly match nothing -- reported as "no coverage exists", which is the
    silent zero this module refuses everywhere else. Comparing skeletons costs
    nothing and turns a wrong name into a discarded one.
    """
    want, got = name_skeleton(latin), name_skeleton(romanize_devanagari(devanagari))
    if len(want) < 3 or len(got) < 3:
        return False
    if want == got:
        return True
    # Not equality alone: romanisation of a conjunct can add or drop one
    # consonant ("प्रसाद" -> "prsad"/"prasad"). But containment on its own
    # accepts any superset, so a model that answered "विकल पौडेल पण्डित शर्मा"
    # for "Bikal Poudel" -- a second person's name appended -- passed the
    # guard. Bound the slack to the one consonant the conjunct explains.
    if abs(len(want) - len(got)) > 1:
        return False
    return want in got or got in want


def generate_devanagari_names(case, invoke_json, usage):
    """`{latin_name: devanagari_name}` for the accused, cheap tier, guarded.

    NES stores `display_name` in Latin ("Bikal Poudel") while Nepali newsrooms
    write Devanagari ("विकल पौडेल"), so every Devanagari query template was
    interpolating a Latin name and matching the wrong script. Measured on the
    labelled set: 3 of the 4 articles we could not find ARE in Google's index,
    and their headlines carry the exact keywords we search for -- only the name
    was in the wrong script.

    An LLM rather than a transliteration table because the failures are
    structural, not phonetic: three-word names, `Dr.` prefixes, aliases, and
    organisations defeated a deterministic matcher on 16 of 24 real cases. The
    model also has the case's own Devanagari prose in front of it, so it is
    extracting a spelling that already exists in the record, not inventing one.

    Every answer is checked with `devanagari_names_match` and dropped if it
    disagrees. A failure here costs query quality, never correctness.

    Keyed by the NES NAME, not by the key the model echoed back. The prompt
    tells it to drop "Dr."/"Mr.", so for `display_name` "Dr. Bikal Poudel" it
    correctly answers `{"Bikal Poudel": "विकल पौडेल"}` -- and `build_queries`,
    looking up the NES name, used to miss and fall back to the mixed-script
    query this whole function exists to prevent. Silently, because the table
    was non-empty so nothing warned.
    """
    # A name already in Devanagari needs no transcription, and asking for one
    # wastes a cheap-tier call to get an answer the skeleton check must then
    # reject: `name_skeleton` keeps only `[a-z]`, so a Devanagari `latin` side
    # reduces to "" and every entry fails. That fired the "the model failed"
    # warning on cases where the model was right and the queries already were.
    names = [n for n in accused_names(case) if not DEVANAGARI_RE.search(n)]
    if not names:
        return {}
    prompt = (
        "Give the Nepali (Devanagari) spelling of each accused person's name "
        "below, as it would be written in a Nepali news report. The case text "
        "that follows already contains these names -- copy the spelling from "
        "it rather than transliterating. Skip anything that is an organisation "
        "rather than a person, and drop titles like Dr. or Mr.\n"
        'Respond with ONLY: {"names": {"<latin>": "<devanagari>"}}\n\n'
        f"Accused: {', '.join(names[:5])}\n\n"
        f"Case title: {case.get('title') or ''}\n"
        f"Case text: {(case.get('description') or '')[:2000]}"
    )
    result, _error = _llm_json(invoke_json, DEVANAGARI_NAME_SYSTEM_PROMPT, prompt,
                               400, "cheap", usage)
    table = (result or {}).get("names")
    if not isinstance(table, dict):
        return {}

    verified = {}
    for devanagari in table.values():
        if not isinstance(devanagari, str):
            continue
        devanagari = devanagari.strip()
        if not DEVANAGARI_RE.search(devanagari):
            continue
        # Matched against the NES names rather than the model's own key, so the
        # check compares the answer with what we asked about instead of with
        # the model's restatement of the question.
        match = next((n for n in names
                      if devanagari_names_match(n, devanagari)), None)
        if match is None:
            log.debug("dropped Devanagari name %r: matches no accused name",
                      devanagari)
            continue
        verified.setdefault(match, devanagari)
    if table and not verified:
        log.warning("every Devanagari name the model returned failed the "
                    "skeleton check; falling back to Latin names in queries")
    return verified


def _verdicts_from_response(result, n_items):
    """`{candidate_index: raw_verdict}` from a batched reply (donor:1027).

    An index the model invented, repeated, or returned as a non-integer is
    dropped rather than mapped onto whichever candidate happens to sit there --
    a mis-indexed verdict would attach one article's judgement to another's URL,
    which is the wrong-article bind arriving through the back door.
    """
    verdicts = {}
    for row in (result or {}).get("results") or []:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= index < n_items and index not in verdicts:
            verdicts[index] = row
    return verdicts


#: Bikram Sambat month names, indexed 1-12. Only test fixtures carried these
#: before; the note register uses them (`२०८१ असार २`), so they belong in source.
NEPALI_MONTHS = ("", "बैशाख", "जेठ", "असार", "साउन", "भदौ", "असोज",
                 "कात्तिक", "मंसिर", "पुष", "माघ", "फागुन", "चैत")

_ASCII_TO_DEVANAGARI_DIGITS = str.maketrans("0123456789", "०१२३४५६७८९")


def format_publication_date(published):
    """`published` rendered for the prompt in BOTH calendars, or `""`.

    THE CONVERSION HAPPENS HERE, NOT IN THE MODEL. Asking an LLM for calendar
    arithmetic across AD and BS is how a note ends up citing a date that never
    existed, and this note is the one artefact whose job is provenance. The model
    is handed both forms and told to copy, not compute.

    Best-effort on the BS half: `ad_to_bs` is total and returns None for an
    out-of-range year or a missing `nepali` package, in which case the AD date
    alone still anchors any relative day in the article.
    """
    if not published:
        return ""
    # A `datetime` slips through `date`-shaped call sites and `isoformat()` then
    # emits "2024-06-16T10:30:00" -- a wall-clock time, in a permanent evidence
    # note, for an article whose publication hour we do not actually know.
    if isinstance(published, datetime):
        published = published.date()
    gregorian = published.isoformat()
    try:
        from jawafdehi_shared.dates import ad_to_bs
        bs = ad_to_bs(published)
    except Exception:  # noqa: BLE001 - a date is never worth failing a run over
        bs = None
    if not bs:
        return gregorian
    try:
        year, month, day = (int(part) for part in bs.split("-"))
        nepali = (f"{year} {NEPALI_MONTHS[month]} {day}"
                  .translate(_ASCII_TO_DEVANAGARI_DIGITS))
    except (ValueError, IndexError):
        return gregorian
    return f"{gregorian} (BS {bs} = {nepali})"


def _batch_prompt(case_context, articles):
    lines = []
    for index, article in enumerate(articles):
        # The publication date was absent from this prompt entirely, which made
        # the "resolve relative days" rule below unfollowable: a model told to
        # convert "आइतबार" into a real date had no anchor to convert it against.
        published = format_publication_date(getattr(article, "published", None))
        lines.append(f"Candidate {index}:\n"
                     f"Title: {article.title}\n"
                     f"URL: {article.url}\n"
                     f"Published: {published or 'unknown'}\n"
                     f"Excerpt: {trim_excerpt(article.text.strip())}")
    return (f"CASE CONTEXT:\n{case_context}\n\n"
            "CANDIDATES (return exactly one result per index):\n\n"
            + "\n\n".join(lines))


def _parse_verdict(row):
    """One raw verdict row -> `Verdict`, canonicalising `event_type`.

    A non-canonical label (casing, whitespace, an invented value) is coerced to
    "" and then to `EVENT_OTHER`, so it can never bypass the per-event cap or
    the bind ordering by being a key neither knows (donor:1096).
    """
    if not row.get("relevant"):
        return Verdict(relevant=False, reason=str(row.get("reason") or ""))
    event_type = str(row.get("event_type") or "").strip().lower()
    if event_type not in EVENT_LIFECYCLE_ORDER:
        event_type = EVENT_OTHER
    return Verdict(
        relevant=True,
        confidence=str(row.get("confidence") or "").strip().lower(),
        reason=str(row.get("reason") or ""),
        event_type=event_type,
        summary=str(row.get("summary") or "").strip(),
    )


#: Answer budget per verified candidate, and the reasoning headroom on top.
#: The old `min(6000, 400 + 500 * n)` capped a full 12-candidate batch at 6,000
#: tokens. Each row carries an English `reason` plus a Devanagari `summary` the
#: prompt asks to run 350-500 characters, and Devanagari costs well under one
#: character per token -- so twelve rows reached the cap before any reasoning
#: tokens, and `salvage_json` turned the overflow into partial verdicts and notes
#: truncated mid-sentence rather than an error. The same budgeting trap this repo
#: already fixed once in PR #411/#412.
VERIFY_TOKENS_PER_CANDIDATE = 900
VERIFY_TOKENS_REASONING_HEADROOM = 8000
VERIFY_TOKENS_CEILING = 32000


def verify_max_tokens(n_survivors):
    """Answer budget for one verify call. Scales with the batch, with headroom."""
    return min(VERIFY_TOKENS_CEILING,
               VERIFY_TOKENS_REASONING_HEADROOM
               + VERIFY_TOKENS_PER_CANDIDATE * max(1, n_survivors))


def verify_batch(articles, case, invoke_json, usage, press_release_text=None,
                 tier="premium"):
    """Two-tier verification of one batch. Returns `[(Article, Verdict)]`.

    Cheap gate drops clearly-irrelevant candidates in ONE call; the premium tier
    re-checks the survivors in ONE call and returns the authoritative verdict
    plus the Nepali note. A gate call that fails or fails to parse escalates the
    WHOLE batch to premium rather than dropping everything on a transient cheap
    -model error (donor:1064) -- fail-open is correct at the gate precisely
    because the premium tier is the decision.

    Every returned pair is (article, verdict) in input order; a candidate the
    gate dropped comes back with `Verdict(relevant=False)` so the caller can
    account for it. Nothing here decides what gets BOUND -- `Verdict.is_bindable`
    and the caller's event-coverage rules do.
    """
    if not articles:
        return []
    context = build_case_context(case, press_release_text)

    gate_result, _gate_error = _llm_json(
        invoke_json, GATE_SYSTEM_PROMPT, _batch_prompt(context, articles),
        min(4000, 200 + 200 * len(articles)), "cheap", usage)
    gate = _verdicts_from_response(gate_result, len(articles))
    # Fail OPEN at the gate -- a cheap-tier error or an unparseable reply
    # escalates to premium rather than dropping the candidate (donor:1064). The
    # gate is a cost optimisation; the premium tier is the decision, so a broken
    # gate must not become a silent rejection.
    #
    # PER INDEX, not per batch. `if gate:` was truthy as soon as ONE row parsed,
    # so a reply truncated at max_tokens -- which `llm.invoke.salvage_json`
    # repairs into a PARTIAL results array, exactly the case it exists for --
    # left every unanswered index falling through `gate.get(i) -> None` to
    # GATE_REJECTED. Seven of twelve fetched candidates could be recorded as "not
    # plausibly this case" when the gate never judged them, with nothing saying
    # the answer was incomplete. An index the gate did not answer is an index the
    # gate did not reject.
    survivor_indexes = [i for i in range(len(articles))
                        if i not in gate or (gate.get(i) or {}).get("relevant")]
    if len(gate) < len(articles):
        log.warning("  cheap gate answered %d/%d candidates; escalating the "
                    "%d unanswered to premium rather than dropping them",
                    len(gate), len(articles), len(articles) - len(gate))

    out = [(article, Verdict(relevant=False, reason=str(SkipReason.GATE_REJECTED)))
           for article in articles]
    if not survivor_indexes:
        return out

    survivors = [articles[i] for i in survivor_indexes]
    verify_result, verify_error = _llm_json(
        invoke_json, VERIFY_SYSTEM_PROMPT, _batch_prompt(context, survivors),
        verify_max_tokens(len(survivors)), tier, usage)
    verdicts = _verdicts_from_response(verify_result, len(survivors))
    for position, original_index in enumerate(survivor_indexes):
        row = verdicts.get(position)
        if row is None:
            # No verdict for this survivor. Either way it is NOT bound -- a
            # missing answer is not a yes (donor:1080) -- but `failed` records
            # WHICH it was, so the run can report "the verifier broke on N
            # candidates" instead of "N candidates were not about the case".
            out[original_index] = (
                articles[original_index],
                Verdict(relevant=False, failed=True,
                        reason=verify_error or "no verdict returned for this candidate"))
            continue
        out[original_index] = (articles[original_index], _parse_verdict(row))
    return out


# ---------------------------------------------------------------------------
# Material identity. Derived from the article, so a re-run is idempotent.
# ---------------------------------------------------------------------------


def normalize_article_url(url):
    """The URL form the material ident is derived from.

    Lowercases scheme+host, drops a `www.` prefix, strips a trailing slash and
    removes the tracking query keys that make the same article look like two.
    Without this a re-run that finds `?utm_source=...` on the same story mints a
    second material and binds it alongside the first.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return (url or "").strip()
    host = re.sub(r"^www\d*\.", "", (parts.hostname or "").lower())
    if parts.port:
        host = f"{host}:{parts.port}"
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
         if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref"))])
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower() or "https", host, path,
                                    query, ""))


def news_material_ident(url, published):
    """`<YYYYMMDD>.<8 hex>` -- the ident half of `/material/news/<ident>`.

    THE FORM IS THE EXISTING ONE, NOT A NEW ONE. All 48 `/material/news/*` rows
    in production read `<YYYYMMDD>.<hex8>`, minted by
    `materials.jsonld.documentsource_to_jsonld` via
    `jawafdehi_shared.entities.ids.build_source_material_iri` from a legacy
    `DocumentSource.source_id` (`source:20251206:14f0f3ec`). This port has no
    DocumentSource to take an id from, so it supplies the two segments itself.

    WHAT CHANGED, AND WHY. In the legacy scheme both segments were incidental:
    the date was the row's CREATION date (every one of the 33 IN_REVIEW news
    materials reads `20260608`, the day of that batch) and the hex was random.
    Random is not re-derivable, so a second run over the same case would mint a
    SECOND material for an article it already had and bind it alongside the
    first. Here both segments come from the article: the date is its
    PUBLICATION date and the hex is `sha256(normalized_url)`. The same article
    therefore always yields the same IRI, which turns "have I bound this
    already?" into one `probe_material` call -- the primitive
    `bind_materials.py` already uses. Deviation A (no publication date -> skip)
    is what makes the date segment total.
    """
    digest = hashlib.sha256(normalize_article_url(url).encode("utf-8")).hexdigest()[:8]
    return f"{published.strftime('%Y%m%d')}.{digest}"
