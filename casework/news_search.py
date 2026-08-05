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
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from html.parser import HTMLParser

log = logging.getLogger("casework.news_search")

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
        present because it IS the evidence note -- binding without one is the
        `bind_materials.py:143` blank-note behaviour this port exists to avoid.
        """
        return bool(
            self.relevant
            and self.confidence == "high"
            and self.event_type in EVENT_LIFECYCLE_ORDER
            and self.summary.strip()
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
            if parsed is not None:
                return parsed
    match = re.search(r"(?:प्रकाशित|मिति)[:\s]*(\d{4})[-/](\d{1,2})[-/](\d{1,2})", safe)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
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
    """The real URL behind a DuckDuckGo `uddg=` redirect. Donor-verbatim (donor:299)."""
    if "uddg=" not in url:
        return url
    uddg = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg", [""])[0]
    return urllib.parse.unquote(uddg) if uddg else url


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


class WebClient:
    """Throttled, cached HTTP reads for search / article / archive.

    THE THROTTLE IS THE POINT OF THE CLASS. The donor paced itself with two
    `time.sleep` calls buried in the search and fetch loops (donor:1365, 1154),
    which meant a caller could not slow a run down without editing the module,
    and a retry inside `_search_duckduckgo` ignored the pacing entirely. Here
    every outbound request goes through `_throttled`, keyed by KIND, so search /
    fetch / archive-save each hold their own minimum interval and a retry is
    spaced like any other call.

    THE CACHE IS PER-RUN AND IN-PROCESS ONLY. A batch searches the same accused
    name under several query templates and the same URL surfaces repeatedly, so
    within one run a repeat is free. It is deliberately NOT a disk cache: the
    project closed PR #409 (`casework/common/llm_cache.py`) because a cache that
    outlives a prompt revision serves a stale artefact as if it were fresh, and
    a cached news page has exactly the same defect.
    """

    def __init__(self, search_delay=1.5, fetch_delay=0.5, save_delay=6.0, timeout=20):
        self.delays = {"search": search_delay, "fetch": fetch_delay,
                       "archive": 0.0, "save": save_delay}
        self.timeout = timeout
        self._last = {}
        self._cache = {}
        self.calls = {"search": 0, "fetch": 0, "archive": 0, "save": 0}

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

    def get(self, url, kind, headers=None, expect_html=False):
        """`(status, text)` for a GET, or `(None, None)` on a transport error.

        Never raises: a dead news host is an ordinary outcome of searching the
        open web, and one of them must not end the case.
        """
        key = (kind, url)
        if key in self._cache:
            return self._cache[key]
        self._throttled(kind)
        self.calls[kind] = self.calls.get(kind, 0) + 1
        request = urllib.request.Request(url, headers=headers or FETCH_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
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
            result = (exc.code, None)
        except Exception as exc:  # noqa: BLE001
            log.debug("GET %s failed: %s", url[:90], exc)
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


#: Markers of the anti-bot interstitial described on `SearchUnavailable`. Matched
#: on the BODY, not the status, because the status is 202.
_ANOMALY_MARKERS = ("anomaly", "detected unusual activity", "verify you are")


def search(client, query):
    """`[{title, url, snippet}]` for one query.

    Returns `[]` only for a genuine no-match. Raises `SearchUnavailable` when the
    backend is refusing to serve results at all, so the caller can stop rather
    than record a silent zero per case.

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
            client._cache.pop(("search", url), None)
            time.sleep(delay)
    log.warning("search failed after %d attempts for %r", SEARCH_RETRY_MAX, query[:60])
    return []


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


def closest_snapshot(client, url):
    """The closest existing Wayback snapshot of `url`, or None.

    Normalises the returned scheme to https -- the availability API sometimes
    answers http (add_news_permalinks:228).
    """
    query = urllib.parse.urlencode({"url": url})
    status, body = client.get(f"{AVAILABILITY_API}?{query}", "archive",
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
    # (add_news_permalinks:262).
    return closest_snapshot(client, url)


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


def build_queries(case, llm_english_queries=None):
    """Up to `QUERY_LIMIT` search queries for one case. Donor-verbatim (donor:757).

    Prioritises accused name + location + corruption keywords over the case
    number, which mostly surfaces court/admin pages rather than newsrooms.
    """
    title = case.get("title") or ""
    names = accused_names(case)
    primary = _clean(names[0]) if names else ""

    events = []
    if primary and len(primary) >= 3:
        for event_type in ALL_EVENT_TYPES:
            for template in EVENT_QUERY_TEMPLATES.get(event_type, []):
                events.append(template.format(name=primary))

    english = list(llm_english_queries or [])
    if not english:
        roman = romanize_devanagari(primary)
        if roman and len(roman) >= 3:
            english += [f"{roman} CIAA Nepal corruption",
                        f"{roman} Nepal special court case"]

    general = []
    location = extract_location_from_title(title)
    org = extract_org_name_from_title(title)
    for index, name in enumerate(names[:3]):
        clean = _clean(name)
        if not clean or len(clean) < 3:
            continue
        if index == 0:
            if location:
                general += [f'"{clean}" {location} भ्रष्टाचार',
                            f"{clean} {location} अख्तियार"]
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
                general.append(f"{clean} {keyword} Nepal")
    if primary and len(primary) > 3 and location:
        general += [f"{primary} {location} भ्रष्टाचार", f"{primary} {location} अख्तियार"]

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
"""

ENGLISH_QUERY_SYSTEM_PROMPT = (
    "You are a Nepal-focused news search assistant. Output only clean search queries.")

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


def _batch_prompt(case_context, articles):
    lines = []
    for index, article in enumerate(articles):
        lines.append(f"Candidate {index}:\n"
                     f"Title: {article.title}\n"
                     f"URL: {article.url}\n"
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
    if gate:
        survivor_indexes = [i for i in range(len(articles))
                            if (gate.get(i) or {}).get("relevant")]
    else:
        # Fail OPEN at the gate -- a cheap-tier error or an unparseable reply
        # escalates the whole batch to premium rather than dropping it
        # (donor:1064). The gate is a cost optimisation; the premium tier is the
        # decision, so a broken gate must not become a silent rejection.
        survivor_indexes = list(range(len(articles)))

    out = [(article, Verdict(relevant=False, reason=str(SkipReason.GATE_REJECTED)))
           for article in articles]
    if not survivor_indexes:
        return out

    survivors = [articles[i] for i in survivor_indexes]
    verify_result, verify_error = _llm_json(
        invoke_json, VERIFY_SYSTEM_PROMPT, _batch_prompt(context, survivors),
        min(6000, 400 + 500 * len(survivors)), tier, usage)
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
