"""Tests for the news enricher. NO TEST MAY REACH THE LIVE WEB OR WAYBACK.

Network is mocked at the LOWEST layer -- `news_search.WebClient.get` -- rather
than by stubbing `search`/`fetch_article`/`resolve_permalink` out. That keeps the
donor plumbing under test: a test that feeds real DuckDuckGo result HTML and real
article HTML exercises `parse_ddg_html`, `extract_publication_date`,
`screen_body`, the URL blocklist and the archive lookup for real, and a
regression in any of them fails here. Stubbing those functions would leave the
whole read half unverified while still looking green.

THE CENTRAL ASSERTION is `test_no_labelled_non_match_is_bound_when_the_model_says
_medium`: every `no_match` row in `news_labelled_set.py` is offered to the
pipeline with the verdict a SLOPPY model returns on it -- `relevant: true` at
`medium` confidence, with a reason naming the shared surname or scheme it matched
on -- and none of them may be bound. That is the real defamation guard, because a
stub that simply echoed the fixture's own label would assert nothing about the
code. The bound-vs-near-miss split is the behaviour under test; see deviation B
in `casework/news_search.py`.

WHAT THESE TESTS CANNOT PROVE is that the MODEL grades correctly -- a
high-confidence false positive would bind, and no mocked test can catch that.
That is measured against the same labelled set with the real verifier and
reported in this task's `findings.md`; it is a prompt property, not a code one.
"""

import collections
import importlib.util
import json
import logging
import pathlib
import sys
import types
import urllib.parse
from datetime import date

import pytest

from casework import enrich_news_articles as en
from casework import news_search as ns
from tests.casework.fakes import FakeUsage
from tests.casework.news_labelled_set import LABELLED_PAIRS, MATCHES, NON_MATCHES

# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------

CASE_SLUG = "case-080-cr-0032-toran-karki-illegal-assets"


def ddg_html(results):
    """A DuckDuckGo HTML result page carrying `results` ({title, url, snippet})."""
    blocks = []
    for row in results:
        blocks.append(
            f'<a class="result__a" href="{row["url"]}">{row["title"]}</a>'
            f'<a class="result__snippet">{row.get("snippet", "")}</a>')
    return "<html><body>" + "".join(blocks) + "</body></html>"


def article_html(title, text, published="2024-05-12"):
    """An article page with a readable `<title>` and a `datePublished` meta.

    The headline is repeated in the body PAST the 200th character on purpose:
    `news_search.screen_body` rejects a page whose body does not contain its own
    title keywords, because that is what a paywall shell or a redirect stub looks
    like. A real article restates its headline in the lede, so a fixture that did
    not would be screened out for being unrealistic rather than for anything the
    test meant to check.
    """
    meta = (f'<meta property="article:published_time" content="{published}">'
            if published else "")
    return (f"<html><head><title>{title}</title>{meta}</head>"
            f"<body><p>{text}</p><p>{title}</p><p>{text}</p></body></html>")


class FakeWeb:
    """Stands in for `news_search.WebClient`. Serves canned pages, records calls.

    Implements the surface the module uses -- `get(url, kind, headers=,
    expect_html=)`, `invalidate`, `clear_cache`, `calls` -- so it can be swapped
    in without touching the code under test. A URL with no canned page returns
    `(404, None)`, which is what a dead news host looks like; nothing here can
    reach the network.

    It CACHES like the real client, and that is deliberate. The fake used to have
    no cache at all, which made it silently incapable of catching the two bugs the
    cache causes: `search`'s retry replaying a cached failure, and
    `resolve_permalink`'s post-save re-query replaying the cached pre-save miss.
    A fake that cannot reproduce the real client's memory is not a stand-in for it.
    """

    def __init__(self, pages=None, search_results=None, snapshot=None,
                 snapshot_after_save=None):
        self.pages = dict(pages or {})
        self.search_results = list(search_results or [])
        self.snapshot = snapshot
        #: What the availability API answers AFTER a Save Page Now request, so a
        #: test can assert the SPN branch actually yields a permalink.
        self.snapshot_after_save = snapshot_after_save
        self.saved = []
        self.calls = {"search": 0, "fetch": 0, "archive": 0, "save": 0}
        self.requested = []
        self._cache = {}
        #: The real client exposes this and the enricher's preflight prints it.
        #: Nothing here ever tunnels -- a fake that reaches the network is a bug
        #: -- but the ATTRIBUTE has to exist or the fake stops standing in for
        #: the real thing, which is how the preflight broke nine tests at once.
        self.proxy = ""
        #: Same reason. The real client charges a budget per search; these tests
        #: are uncapped, but the attribute must exist for the fake to stand in.
        self.budget = None

    def invalidate(self, url, kind):
        self._cache.pop((kind, url), None)

    def clear_cache(self):
        self._cache.clear()

    def get(self, url, kind, headers=None, expect_html=False, error_body=False):
        key = (kind, url)
        if key in self._cache:
            return self._cache[key]
        self.calls[kind] = self.calls.get(kind, 0) + 1
        self.requested.append((kind, url))
        result = self._serve(url, kind)
        self._cache[key] = result
        return result

    def _serve(self, url, kind):
        if kind == "search":
            return 200, ddg_html(self.search_results)
        if kind == "archive":
            if self.saved and self.snapshot_after_save:
                return 200, json.dumps(
                    {"archived_snapshots": {"closest": {"available": True,
                                                        "url": self.snapshot_after_save}}})
            if self.snapshot is None:
                return 429, None            # Wayback rate-limited, as seen in prod
            return 200, json.dumps(
                {"archived_snapshots": {"closest": {"available": True,
                                                    "url": self.snapshot}}})
        if kind == "save":
            self.saved.append(url)
            return 200, ""
        page = self.pages.get(url)
        return (200, page) if page is not None else (404, None)


class FakeApi:
    """Loopback `CaseworkApi` stand-in. Records every write."""

    def __init__(self, case, etag='W/"1"', base_url="http://127.0.0.1:48010/api"):
        self.case = case
        self.etag = etag
        self.base_url = base_url
        self.materials = []
        self.replaced = []
        #: Counts detail fetches, so a test can pin that the preflight failed
        #: BEFORE any case was touched rather than after the list was walked.
        self.fetched = 0

    def iter_cases(self, params=None, timeout=60, progress=None):
        return iter([{"slug": self.case["slug"], "state": self.case.get("state")}])

    def get_case_with_etag(self, slug, timeout=60):
        self.fetched += 1
        return self.case, self.etag

    def create_material(self, doc, material_type, timeout=60):
        self.materials.append((doc, material_type))
        return doc

    def replace_list(self, slug, path, items, timeout=60, if_match=None):
        self.replaced.append({"slug": slug, "path": path, "items": items,
                              "if_match": if_match})
        return {}


def case_payload(pair=None, *, state="DRAFT", evidence=None, slug=CASE_SLUG):
    """A case DETAIL payload that clears the `news` stage's prerequisites."""
    source = (pair or LABELLED_PAIRS[0])["case"]
    return {
        "slug": slug,
        "state": state,
        "title": source["title"],
        "short_description": source["short_description"],
        "key_allegations": list(source["key_allegations"]),
        "court_cases": [
            "https://jawafdehi.org/courtcase/special/"
            + source["court_case_no"].lower()],
        "entities": [{"display_name": name, "type": "accused"}
                     for name in source["accused"]] or
                    [{"display_name": "तोरण बहादुर कार्की", "type": "accused"}],
        "evidence": list(evidence or []),
    }


def news_evidence_entry(url, iri, note="existing note"):
    """An already-bound news evidence entry, in the resolved DETAIL shape."""
    return {"material_iri": iri, "additional_details": note,
            "material": {"material_type": "news",
                         "urls": [{"link": url, "role": "RAW"}]}}


def verdict_json(rows):
    """A batched verifier reply. `rows` is `[dict]`, index added positionally."""
    return {"results": [dict(row, index=i) for i, row in enumerate(rows)]}


def stub_invoke_json(gate_relevant=True, verify_rows=None, queries=None):
    """An `invoke_json` stub that answers the three prompts this stage sends.

    Dispatches on the system prompt, so the test declares WHAT each tier says
    without depending on call order. `verify_rows` is a list of raw verdict dicts
    applied positionally to the candidates in the premium call.
    """
    seen = {"tiers": [], "systems": []}

    def invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        seen["tiers"].append(tier)
        seen["systems"].append(system[:40])
        if system is ns.ENGLISH_QUERY_SYSTEM_PROMPT:
            return {"queries": list(queries or [])}
        n = content.count("\nCandidate ") + content.count("Candidate 0:")
        n = max(1, content.count("Candidate "))
        if system is ns.GATE_SYSTEM_PROMPT:
            return verdict_json([{"relevant": gate_relevant} for _ in range(n)])
        return verdict_json(list(verify_rows or [])[:n] or [{"relevant": False}])

    invoke_json.seen = seen
    return invoke_json


BASE_ARGV = ["--api-base-url", "http://127.0.0.1:48010", "--slug", CASE_SLUG]


def run_main(monkeypatch, api, web, invoke_json, argv=(), tmp_path=None):
    """Drive `main()` with every external dependency faked."""
    monkeypatch.setattr(en, "build_api", lambda args: api)
    monkeypatch.setattr(en, "bootstrap", lambda *a, **k: None)
    monkeypatch.setattr(en, "WebClient", lambda **kwargs: web)

    fake_invoke = types.ModuleType("llm.invoke")
    fake_invoke.invoke_json = invoke_json
    fake_usage = types.ModuleType("llm.usage")
    fake_usage.UsageAccumulator = FakeUsage
    fake_usage.render_usage_table = lambda *a, **k: ""
    monkeypatch.setitem(sys.modules, "llm.invoke", fake_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_usage)
    return en.main(BASE_ARGV + list(argv))


# ---------------------------------------------------------------------------
# The labelled set -- the false-positive gate.
# ---------------------------------------------------------------------------


def _plan_for_pair(pair, verdict_row, snapshot="https://web.archive.org/web/1/x"):
    """Run one labelled pair through search -> verify -> plan and return the plan.

    The article is served as a real HTML page from a real DuckDuckGo result page,
    so everything between the search and the plan is the production code path.
    """
    article = pair["article"]
    published = article["published"] or "2024-05-12"
    web = FakeWeb(
        pages={article["url"]: article_html(article["title"] or "Untitled",
                                           article["text"], published)},
        search_results=[{"title": article["title"] or "Untitled",
                         "url": article["url"], "snippet": article["text"][:180]}],
        snapshot=snapshot,
    )
    case = case_payload(pair)
    outcome = en.collect_for_case(
        case, web, stub_invoke_json(verify_rows=[verdict_row]), FakeUsage(),
        max_articles=3)
    return en.plan_case(case, 'W/"1"', outcome, client=web, save_permalinks=False)


SLOPPY_MEDIUM = {
    "relevant": True,
    "confidence": "medium",
    "event_type": "filing",
    "reason": "the accused name and a corruption allegation both match",
    "summary": "यो समाचार लेख यस मुद्दासँग सम्बन्धित देखिन्छ। " * 6,
}


@pytest.mark.parametrize(
    "pair", NON_MATCHES,
    ids=[f'{p["case"]["court_case_no"]}<-{p["article_from"][:28]}' for p in NON_MATCHES])
def test_no_labelled_non_match_is_bound_when_the_model_says_medium(pair):
    """ZERO FALSE POSITIVES. A `medium` yes on a no-match row must never bind.

    `medium` is the verdict a sloppy model returns on exactly these rows -- the
    donor's own rubric calls "defendant name + corruption allegations" medium
    evidence, which is the literal description of the same accused's other case.
    Production carries two such binds. The pipeline must route every one of them
    to `near_misses` and bind nothing.
    """
    plan = _plan_for_pair(pair, SLOPPY_MEDIUM)
    assert plan.action == "NOOP", (
        f"{pair['case']['court_case_no']} would bind an article about "
        f"{pair['article_from']} -- {pair['why']}")
    assert not plan.materials
    assert not plan.patch_items
    assert len(plan.outcome.near_misses) == 1, "the refusal must be REPORTED, not silent"


@pytest.mark.parametrize(
    "pair", NON_MATCHES,
    ids=[f'{p["case"]["court_case_no"]}<-{p["article_from"][:28]}' for p in NON_MATCHES])
def test_no_labelled_non_match_is_bound_when_the_verifier_rejects_it(pair):
    """The ordinary path: the verifier says no, and nothing is bound or reported
    as a near miss -- a rejection is a skip, which is a different row in the
    review file from "a human should look at this"."""
    plan = _plan_for_pair(pair, {"relevant": False, "reason": "different case"})
    assert plan.action == "NOOP"
    assert not plan.materials
    assert not plan.outcome.near_misses
    assert any(s.reason is ns.SkipReason.VERIFY_REJECTED for s in plan.outcome.skipped)


@pytest.mark.parametrize(
    "pair", MATCHES,
    ids=[f'{p["case"]["court_case_no"]}' for p in MATCHES])
def test_every_labelled_match_binds_when_the_model_says_high(pair):
    """The other direction: the bar must not be so tight that nothing passes.

    Without this, a bug that rejected everything would satisfy the
    zero-false-positive assertion perfectly.
    """
    row = dict(SLOPPY_MEDIUM, confidence="high")
    plan = _plan_for_pair(pair, row)
    assert plan.action == "WOULD_BIND", pair["why"]
    assert len(plan.materials) == 1
    iri = plan.bound_iris[0]
    assert iri.startswith("https://jawafdehi.org/material/news/")


def test_the_bound_material_iri_is_derived_from_the_article_not_the_clock():
    """Same article, two runs -> the same IRI, so a re-run cannot duplicate it."""
    pair = MATCHES[0]
    row = dict(SLOPPY_MEDIUM, confidence="high")
    first = _plan_for_pair(pair, row).bound_iris
    second = _plan_for_pair(pair, row).bound_iris
    assert first == second != []


def test_a_tracking_query_does_not_mint_a_second_material():
    published = date(2024, 5, 12)
    plain = ns.news_material_ident("https://ekantipur.com/news/story-1", published)
    noisy = ns.news_material_ident(
        "https://WWW.ekantipur.com/news/story-1/?utm_source=twitter&fbclid=abc",
        published)
    assert plain == noisy


# ---------------------------------------------------------------------------
# Selection rules.
# ---------------------------------------------------------------------------


def _outcome_for(verify_rows, results, pages, max_articles=5, gate_relevant=True,
                 case=None):
    web = FakeWeb(pages=pages, search_results=results, snapshot=None)
    return en.collect_for_case(
        case or case_payload(), web,
        stub_invoke_json(gate_relevant=gate_relevant, verify_rows=verify_rows),
        FakeUsage(), max_articles=max_articles), web


def test_no_search_results_accepts_nothing_and_does_not_call_the_verifier():
    invoke_json = stub_invoke_json(verify_rows=[dict(SLOPPY_MEDIUM, confidence="high")])
    web = FakeWeb(search_results=[])
    outcome = en.collect_for_case(case_payload(), web, invoke_json, FakeUsage(),
                                 max_articles=3)
    assert outcome.accepted == []
    assert web.calls["search"] > 0, "it must actually have searched"
    assert ns.GATE_SYSTEM_PROMPT[:40] not in invoke_json.seen["systems"]


def test_only_one_article_per_event_type_is_bound():
    """Three verdict-event articles offered, one bound; the rest are reported as
    EVENT_TYPE_FULL rather than silently dropped."""
    results, pages = [], {}
    for i in range(3):
        url = f"https://outlet{i}.test/story-{i}"
        results.append({"title": f"Verdict story {i}", "url": url,
                        "snippet": "corruption verdict special court " * 6})
        pages[url] = article_html(f"Verdict story {i}",
                                 "CIAA corruption verdict special court. " * 20)
    rows = [dict(SLOPPY_MEDIUM, confidence="high", event_type="verdict")
            for _ in range(3)]
    outcome, _ = _outcome_for(rows, results, pages)
    assert len(outcome.accepted) == 1
    full = [s for s in outcome.skipped if s.reason is ns.SkipReason.EVENT_TYPE_FULL]
    assert len(full) == 2


def test_distinct_event_types_are_all_bound_in_lifecycle_order():
    events = ["appeal", "filing", "verdict"]
    results, pages, rows = [], {}, []
    for i, event in enumerate(events):
        url = f"https://outlet{i}.test/story-{i}"
        results.append({"title": f"{event} story", "url": url,
                        "snippet": "corruption special court " * 6})
        pages[url] = article_html(f"{event} story",
                                 "CIAA corruption special court. " * 20)
        rows.append(dict(SLOPPY_MEDIUM, confidence="high", event_type=event))
    outcome, _ = _outcome_for(rows, results, pages)
    assert [v.event_type for _, v in outcome.accepted] == ["filing", "verdict", "appeal"]


def test_max_articles_caps_what_is_bound():
    events = ["investigation", "filing", "hearing", "verdict", "appeal"]
    results, pages, rows = [], {}, []
    for i, event in enumerate(events):
        url = f"https://outlet{i}.test/story-{i}"
        results.append({"title": f"{event} story", "url": url,
                        "snippet": "corruption special court " * 6})
        pages[url] = article_html(f"{event} story",
                                 "CIAA corruption special court. " * 20)
        rows.append(dict(SLOPPY_MEDIUM, confidence="high", event_type=event))
    outcome, _ = _outcome_for(rows, results, pages, max_articles=2)
    assert len(outcome.accepted) == 2


def test_an_article_with_no_publication_date_is_skipped_and_reported():
    """Deviation A. The donor dated it to today; this refuses it and says so."""
    url = "https://outlet.test/undated"
    results = [{"title": "Undated story", "url": url,
                "snippet": "corruption special court " * 6}]
    pages = {url: article_html("Undated story",
                              "CIAA corruption special court. " * 20, published=None)}
    rows = [dict(SLOPPY_MEDIUM, confidence="high")]
    outcome, _ = _outcome_for(rows, results, pages)
    assert outcome.accepted == []
    assert [s.reason for s in outcome.skipped] == [ns.SkipReason.NO_DATE]


def test_an_official_ciaa_press_release_url_is_never_a_news_candidate():
    url = "https://ciaa.gov.np/pressrelease/9912"
    results = [{"title": "CIAA press release", "url": url, "snippet": "x" * 120}]
    outcome, web = _outcome_for([dict(SLOPPY_MEDIUM, confidence="high")], results,
                               {url: article_html("PR", "text " * 40)})
    assert outcome.accepted == []
    assert [s.reason for s in outcome.skipped] == [ns.SkipReason.OFFICIAL_PRESS_RELEASE]
    assert ("fetch", url) not in web.requested, "screened by URL, before any fetch"


def test_a_tag_listing_page_is_screened_out():
    url = "https://outlet.test/tag/corruption"
    results = [{"title": "Corruption tag", "url": url, "snippet": "x" * 120}]
    outcome, _ = _outcome_for([dict(SLOPPY_MEDIUM, confidence="high")], results,
                             {url: article_html("Tag", "text " * 40)})
    assert [s.reason for s in outcome.skipped] == [ns.SkipReason.BLOCKLISTED]


def test_an_already_bound_url_is_skipped_not_re_verified():
    url = "https://outlet.test/known"
    iri = "https://jawafdehi.org/material/news/20240512.deadbeef"
    case = case_payload(evidence=[news_evidence_entry(url, iri)])
    results = [{"title": "Known story", "url": url, "snippet": "x" * 120}]
    outcome, web = _outcome_for([dict(SLOPPY_MEDIUM, confidence="high")], results,
                               {url: article_html("Known", "text " * 40)},
                               case=case)
    assert outcome.accepted == []
    assert [s.reason for s in outcome.skipped] == [ns.SkipReason.ALREADY_LINKED]
    assert ("fetch", url) not in web.requested


def test_a_gate_rejection_costs_no_premium_call():
    url = "https://outlet.test/offtopic"
    results = [{"title": "Off topic", "url": url,
                "snippet": "corruption special court " * 6}]
    pages = {url: article_html("Off topic", "CIAA corruption special court. " * 20)}
    invoke_json = stub_invoke_json(gate_relevant=False,
                                  verify_rows=[dict(SLOPPY_MEDIUM, confidence="high")])
    web = FakeWeb(pages=pages, search_results=results)
    outcome = en.collect_for_case(case_payload(), web, invoke_json, FakeUsage(),
                                 max_articles=3)
    assert outcome.accepted == []
    assert [s.reason for s in outcome.skipped] == [ns.SkipReason.GATE_REJECTED]
    assert "premium" not in invoke_json.seen["tiers"]


def test_a_gate_failure_escalates_the_batch_instead_of_dropping_it():
    """Fail-open at the gate, because the premium tier is the decision."""
    url = "https://outlet.test/story"
    results = [{"title": "Story", "url": url,
                "snippet": "corruption special court " * 6}]
    pages = {url: article_html("Story", "CIAA corruption special court. " * 20)}
    high = dict(SLOPPY_MEDIUM, confidence="high")

    def invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if system is ns.ENGLISH_QUERY_SYSTEM_PROMPT:
            return {"queries": []}
        if system is ns.GATE_SYSTEM_PROMPT:
            raise RuntimeError("cheap tier is down")
        return verdict_json([high])

    web = FakeWeb(pages=pages, search_results=results)
    outcome = en.collect_for_case(case_payload(), web, invoke_json, FakeUsage(),
                                 max_articles=3)
    assert len(outcome.accepted) == 1


def _one_article_run(invoke_json):
    url = "https://outlet.test/story"
    results = [{"title": "Story", "url": url,
                "snippet": "corruption special court " * 6}]
    pages = {url: article_html("Story", "CIAA corruption special court. " * 20)}
    web = FakeWeb(pages=pages, search_results=results)
    return en.collect_for_case(case_payload(), web, invoke_json, FakeUsage(),
                               max_articles=3)


def test_a_survivor_with_no_verdict_returned_is_refused():
    """A missing answer is not a yes."""
    def invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if system is ns.ENGLISH_QUERY_SYSTEM_PROMPT:
            return {"queries": []}
        if system is ns.GATE_SYSTEM_PROMPT:
            return verdict_json([{"relevant": True}])
        return {"results": []}

    outcome = _one_article_run(invoke_json)
    assert outcome.accepted == []
    assert [s.reason for s in outcome.skipped] == [ns.SkipReason.VERIFY_FAILED]


def test_a_crashing_verifier_is_reported_as_FAILED_not_as_not_relevant():
    """The distinction that decides whether a run means anything.

    "No article is about this case" is a normal outcome for this stage, so a
    provider outage that read as a rejection would make a broken run
    indistinguishable from a clean one. Measured on this host, the only working
    provider (`claude_cli`) fails premium calls often enough for this to be the
    likely reading of any zero.
    """
    def invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if system is ns.ENGLISH_QUERY_SYSTEM_PROMPT:
            return {"queries": []}
        if system is ns.GATE_SYSTEM_PROMPT:
            return verdict_json([{"relevant": True}])
        raise RuntimeError("error_max_turns")

    outcome = _one_article_run(invoke_json)
    assert outcome.accepted == []
    assert [s.reason for s in outcome.skipped] == [ns.SkipReason.VERIFY_FAILED]
    assert "error_max_turns" in outcome.skipped[0].detail


def test_a_genuine_rejection_is_not_reported_as_a_failure():
    def invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if system is ns.ENGLISH_QUERY_SYSTEM_PROMPT:
            return {"queries": []}
        if system is ns.GATE_SYSTEM_PROMPT:
            return verdict_json([{"relevant": True}])
        return verdict_json([{"relevant": False, "reason": "a different case"}])

    outcome = _one_article_run(invoke_json)
    assert [s.reason for s in outcome.skipped] == [ns.SkipReason.VERIFY_REJECTED]


def test_a_failed_verifier_marks_the_case_unreliable_in_the_review_file(monkeypatch,
                                                                       tmp_path):
    web, pair = _main_web()

    def invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if system is ns.ENGLISH_QUERY_SYSTEM_PROMPT:
            return {"queries": []}
        if system is ns.GATE_SYSTEM_PROMPT:
            return verdict_json([{"relevant": True}])
        raise RuntimeError("provider down")

    api = FakeApi(case_payload(pair))
    review = tmp_path / "review.md"
    report = run_main(monkeypatch, api, web, invoke_json,
                      ["--review-file", str(review)])
    assert "error" in report.summary()
    text = review.read_text(encoding="utf-8")
    assert "VERIFIER FAILED" in text
    assert "UNRELIABLE" in text
    assert api.materials == []


def test_a_verdict_carrying_an_invented_index_is_dropped_not_mapped():
    """A mis-indexed verdict would attach one article's judgement to another's URL."""
    verdicts = ns._verdicts_from_response(
        {"results": [{"index": 7, "relevant": True}, {"index": 0, "relevant": True}]}, 2)
    assert set(verdicts) == {0}


def test_a_relevant_verdict_with_no_summary_is_not_bindable():
    """The summary IS the evidence note; binding without one is the blank-note bug."""
    verdict = ns.Verdict(relevant=True, confidence="high", event_type="filing",
                        summary="   ")
    assert not verdict.is_bindable


def test_an_invented_event_type_becomes_other_rather_than_bypassing_the_cap():
    verdict = ns._parse_verdict({"relevant": True, "confidence": "high",
                                "event_type": "  TOTALLY MADE UP ",
                                "summary": "note"})
    assert verdict.event_type == ns.EVENT_OTHER


# ---------------------------------------------------------------------------
# The search backend refusing to serve results at all.
# ---------------------------------------------------------------------------


class AnomalyWeb(FakeWeb):
    """Serves DuckDuckGo's current anti-bot interstitial: HTTP 202, no results.

    This is what every DDG endpoint actually returned from this host on
    2026-08-05, under both the donor UA and a browser UA.
    """

    def get(self, url, kind, headers=None, expect_html=False):
        self.calls[kind] = self.calls.get(kind, 0) + 1
        if kind == "search":
            return 202, ("<html><head><title>DuckDuckGo</title></head><body>"
                         "<div class='anomaly-modal__title'>Unfortunately, bots "
                         "use DuckDuckGo too.</div></body></html>")
        return super().get(url, kind, headers=headers, expect_html=expect_html)


def test_an_anti_bot_page_raises_rather_than_reporting_zero_results():
    """A 202 anomaly page must NOT read as "no news exists about this case".

    Binding nothing is a normal, correct outcome for this stage, so a silent zero
    is indistinguishable from success. Before this guard a 238-case run would
    have produced 238 empty rows and a green summary.
    """
    with pytest.raises(ns.SearchUnavailable, match="anti-bot"):
        ns.search(AnomalyWeb(), "any query")


def test_an_anti_bot_page_is_not_retried():
    """Not transient. Retrying 3x per query per case is how a ban gets longer."""
    web = AnomalyWeb()
    with pytest.raises(ns.SearchUnavailable):
        ns.search(web, "any query")
    assert web.calls["search"] == 1


def test_the_run_aborts_on_the_first_dead_search_instead_of_per_case(monkeypatch,
                                                                    tmp_path):
    web = AnomalyWeb()
    api = FakeApi(case_payload())
    review = tmp_path / "review.md"
    # SystemExit, not a returned report: an aborted run must not exit 0. The
    # review file and the summary are still written before it.
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, api, web, stub_invoke_json(),
                 ["--review-file", str(review)])
    assert exc.value.code not in (0, None)
    text = review.read_text(encoding="utf-8")
    assert "SEARCH BACKEND DOWN" in text
    assert api.materials == [] and api.replaced == []


def test_a_genuine_empty_result_page_is_still_zero_not_an_error():
    """The distinction the exception exists to make: an honest no-match."""
    assert ns.search(FakeWeb(search_results=[]), "obscure query") == []


# ---------------------------------------------------------------------------
# Wayback.
# ---------------------------------------------------------------------------


def test_an_existing_snapshot_becomes_a_permalink_role_link():
    pair = MATCHES[0]
    plan = _plan_for_pair(pair, dict(SLOPPY_MEDIUM, confidence="high"),
                          snapshot="https://web.archive.org/web/2024/story")
    doc = plan.materials[0][1]
    roles = {m["jawafdehi:linkRole"]: m["contentUrl"] for m in doc["associatedMedia"]}
    assert roles["PERMALINK"] == "https://web.archive.org/web/2024/story"
    assert roles["RAW"] == pair["article"]["url"]


def test_wayback_unavailable_still_binds_with_the_raw_link_alone():
    """A 429 from the availability API is a transient, not a reason to lose the
    article. Prod answered 429 during this port's own checks."""
    pair = MATCHES[0]
    plan = _plan_for_pair(pair, dict(SLOPPY_MEDIUM, confidence="high"), snapshot=None)
    assert plan.action == "WOULD_BIND"
    doc = plan.materials[0][1]
    roles = [m["jawafdehi:linkRole"] for m in doc["associatedMedia"]]
    assert roles == ["RAW"]


def test_a_dry_run_never_requests_a_save_page_now_capture():
    pair = MATCHES[0]
    article = pair["article"]
    web = FakeWeb(
        pages={article["url"]: article_html(article["title"] or "x", article["text"])},
        search_results=[{"title": article["title"] or "x", "url": article["url"],
                         "snippet": article["text"][:180]}],
        snapshot=None)
    case = case_payload(pair)
    outcome = en.collect_for_case(case, web,
                                 stub_invoke_json(verify_rows=[
                                     dict(SLOPPY_MEDIUM, confidence="high")]),
                                 FakeUsage(), max_articles=3)
    en.plan_case(case, 'W/"1"', outcome, client=web, save_permalinks=False)
    assert web.calls["save"] == 0


def test_an_archive_url_is_not_itself_archived_again():
    assert ns.resolve_permalink(FakeWeb(snapshot="x"),
                                "https://web.archive.org/web/1/y") is None


# ---------------------------------------------------------------------------
# The evidence union-merge.
# ---------------------------------------------------------------------------


def test_the_merge_appends_and_never_disturbs_an_existing_entry():
    current = [{"material_iri": "https://jawafdehi.org/material/press_release/1",
                "additional_details": "a human wrote this"},
               {"material_iri": "https://jawafdehi.org/material/court_order/2",
                "additional_details": ""}]
    merged = en.merge_news_evidence(
        current, [("https://jawafdehi.org/material/news/20240101.aaaaaaaa", "नयाँ नोट")])
    assert merged[:2] == current
    assert merged[2] == {
        "material_iri": "https://jawafdehi.org/material/news/20240101.aaaaaaaa",
        "additional_details": "नयाँ नोट"}


def test_the_merge_never_overwrites_a_note_on_an_iri_already_present():
    iri = "https://jawafdehi.org/material/news/20240101.aaaaaaaa"
    current = [{"material_iri": iri, "additional_details": "a human edited this"}]
    merged = en.merge_news_evidence(current, [(iri, "the model would say this")])
    assert merged == current


def test_a_bound_entry_carries_its_note_rather_than_binding_blank():
    """Deviation 2 -- `bind_materials.py:143` appends `""`; this must not."""
    pair = MATCHES[0]
    plan = _plan_for_pair(pair, dict(SLOPPY_MEDIUM, confidence="high"))
    appended = plan.patch_items[-1]
    assert appended["additional_details"].strip()
    assert len(appended["additional_details"]) > 120


# ---------------------------------------------------------------------------
# The writer's refusals.
# ---------------------------------------------------------------------------


def _bindable_plan(state="DRAFT", etag='W/"1"'):
    pair = MATCHES[0]
    plan = _plan_for_pair(pair, dict(SLOPPY_MEDIUM, confidence="high"))
    plan.state = state
    plan.if_match = etag
    return plan


def test_the_writer_refuses_a_non_loopback_host_even_with_remote_writes_allowed():
    api = FakeApi(case_payload(), base_url="https://api.jawafdehi.org/api")
    api.allow_remote_writes = True
    with pytest.raises(ValueError, match="loopback ONLY"):
        en.apply_plan(api, _bindable_plan())
    assert api.materials == [] and api.replaced == []


@pytest.mark.parametrize("state", ["IN_REVIEW", "PUBLISHED", ""])
def test_the_writer_refuses_any_state_but_draft(state):
    api = FakeApi(case_payload())
    with pytest.raises(RuntimeError, match="destructive whole-list replace"):
        en.apply_plan(api, _bindable_plan(state=state))
    assert api.materials == [] and api.replaced == []


def test_the_writer_refuses_when_no_etag_was_captured():
    api = FakeApi(case_payload())
    with pytest.raises(RuntimeError, match="no ETag"):
        en.apply_plan(api, _bindable_plan(etag=None))
    assert api.materials == [] and api.replaced == []


def test_the_writer_refuses_a_plan_that_is_not_would_bind():
    api = FakeApi(case_payload())
    plan = en.NewsPlan(slug="x", action="NOOP")
    with pytest.raises(ValueError, match="NOOP plan"):
        en.apply_plan(api, plan)


def test_the_writer_creates_materials_before_it_binds_them():
    api = FakeApi(case_payload())
    plan = _bindable_plan()
    en.apply_plan(api, plan)
    assert [mt for _, mt in api.materials] == ["news"]
    assert len(api.replaced) == 1
    write = api.replaced[0]
    assert write["path"] == "evidence"
    assert write["if_match"] == 'W/"1"'
    bound = {item["material_iri"] for item in write["items"]}
    assert set(plan.bound_iris) <= bound


def test_the_write_sends_the_whole_merged_list_not_a_delta():
    existing = news_evidence_entry("https://outlet.test/old",
                                   "https://jawafdehi.org/material/news/20230101.aaaa")
    api = FakeApi(case_payload(evidence=[existing]))
    plan = _bindable_plan()
    plan.patch_items = en.merge_news_evidence(
        en.current_evidence(api.case), [(plan.bound_iris[0], "नोट")])
    en.apply_plan(api, plan)
    items = api.replaced[0]["items"]
    assert len(items) == 2
    assert items[0]["material_iri"] == existing["material_iri"]
    assert items[0]["additional_details"] == "existing note"


# ---------------------------------------------------------------------------
# `main()` end to end.
# ---------------------------------------------------------------------------


def _main_web(pair=None, snapshot=None):
    pair = pair or MATCHES[0]
    article = pair["article"]
    return FakeWeb(
        pages={article["url"]: article_html(article["title"] or "x", article["text"])},
        search_results=[{"title": article["title"] or "x", "url": article["url"],
                         "snippet": article["text"][:180]}],
        snapshot=snapshot), pair


def test_a_dry_run_writes_nothing_and_still_ships_a_review_file(monkeypatch, tmp_path):
    web, pair = _main_web()
    api = FakeApi(case_payload(pair))
    review = tmp_path / "review.md"
    report = run_main(monkeypatch, api, web,
                      stub_invoke_json(verify_rows=[dict(SLOPPY_MEDIUM,
                                                         confidence="high")]),
                      ["--review-file", str(review)])
    assert api.materials == [] and api.replaced == []
    assert report.summary() == {"would-enrich": 1}
    text = review.read_text(encoding="utf-8")
    assert "DRY RUN" in text
    assert pair["article"]["url"] in text
    assert "material/news/" in text


def test_apply_writes_and_the_review_file_records_it(monkeypatch, tmp_path):
    web, pair = _main_web()
    api = FakeApi(case_payload(pair))
    review = tmp_path / "review.md"
    report = run_main(monkeypatch, api, web,
                      stub_invoke_json(verify_rows=[dict(SLOPPY_MEDIUM,
                                                         confidence="high")]),
                      ["--apply", "--review-file", str(review)])
    assert report.summary() == {"enriched": 1}
    assert len(api.materials) == 1 and len(api.replaced) == 1
    assert "APPLIED" in review.read_text(encoding="utf-8")


def test_a_saturated_case_is_skipped_before_any_search(monkeypatch, tmp_path):
    bound = [news_evidence_entry(f"https://outlet.test/{i}",
                                f"https://jawafdehi.org/material/news/2023010{i}.aaaa")
             for i in range(3)]
    web, pair = _main_web()
    api = FakeApi(case_payload(pair, evidence=bound))
    report = run_main(monkeypatch, api, web, stub_invoke_json(),
                      ["--max-articles", "3", "--review-file", str(tmp_path / "r.md")])
    assert report.summary() == {"already": 1}
    assert web.calls["search"] == 0


def test_a_near_miss_reaches_the_review_file_for_a_human(monkeypatch, tmp_path):
    web, pair = _main_web()
    api = FakeApi(case_payload(pair))
    review = tmp_path / "review.md"
    report = run_main(monkeypatch, api, web,
                      stub_invoke_json(verify_rows=[SLOPPY_MEDIUM]),
                      ["--review-file", str(review)])
    assert report.summary() == {"skipped": 1}
    text = review.read_text(encoding="utf-8")
    assert "Near misses" in text
    assert pair["article"]["url"] in text
    assert api.materials == []


def test_an_unmet_prerequisite_is_reported_not_silently_skipped(monkeypatch, tmp_path):
    web, pair = _main_web()
    case = case_payload(pair)
    case["entities"] = []
    api = FakeApi(case)
    review = tmp_path / "review.md"
    report = run_main(monkeypatch, api, web, stub_invoke_json(),
                      ["--review-file", str(review)])
    assert report.summary() == {"unmet": 1}
    assert "entities" in review.read_text(encoding="utf-8")
    assert web.calls["search"] == 0


def test_a_fetch_failure_on_one_case_does_not_sink_the_run(monkeypatch, tmp_path):
    web, pair = _main_web()

    class Broken(FakeApi):
        def get_case_with_etag(self, slug, timeout=60):
            raise RuntimeError("boom")

    api = Broken(case_payload(pair))
    report = run_main(monkeypatch, api, web, stub_invoke_json(),
                      ["--review-file", str(tmp_path / "r.md")])
    assert report.summary() == {"error": 1}


def test_the_run_logs_an_events_line_per_step(monkeypatch, tmp_path):
    web, pair = _main_web()
    api = FakeApi(case_payload(pair))
    run_main(monkeypatch, api, web,
             stub_invoke_json(verify_rows=[dict(SLOPPY_MEDIUM, confidence="high")]),
             ["--review-file", str(tmp_path / "r.md")])
    events = list(tmp_path.glob("*.events.jsonl"))
    assert events, "configure_run_logging must have written an events file"
    steps = [json.loads(line)["step"]
             for line in events[0].read_text(encoding="utf-8").splitlines() if line]
    assert steps[0] == "start"
    assert "search" in steps and "write" in steps


def test_max_articles_must_be_non_negative():
    with pytest.raises(SystemExit, match="non-negative"):
        en.main(BASE_ARGV + ["--max-articles", "-1"])


def test_the_stage_is_registered_with_the_premium_tier():
    from casework.common.llm import tier_for
    from casework.common.pipeline import STAGES

    assert tier_for("news") == "premium"
    assert STAGES["news"].requires_stages == ("card", "entities")
    assert STAGES["news"].provides == ("evidence",)


def test_the_stage_gates_on_the_three_query_fields():
    from casework.common.pipeline import STAGES, unmet_prerequisites

    for field in ("title", "entities", "court_cases"):
        case = case_payload()
        case[field] = [] if field != "title" else ""
        unmet = unmet_prerequisites(STAGES["news"], case)
        assert any(field in reason for reason in unmet), field


def test_the_stage_needs_an_accused_not_merely_some_entity():
    """A case carrying only `location`/`related` binds must NOT be attempted.

    "entities is non-empty" let it through, and `accused_names` then fell back to
    the importer's template title, so 12 searches and a premium batch were spent
    on queries built from "CIAA Special Court Case 076-CR-0182: …".
    """
    from casework.common.pipeline import STAGES, unmet_prerequisites

    case = case_payload()
    case["entities"] = [
        {"display_name": "Tokha Municipality, Kathmandu", "type": "location"},
        {"display_name": "जलस्रोत अनुसन्धान विकास केन्द्र", "type": "related"},
    ]
    unmet = unmet_prerequisites(STAGES["news"], case)
    assert any("accused" in reason for reason in unmet), unmet
    # And it must not double-report when entities is empty outright.
    empty = case_payload()
    empty["entities"] = []
    assert len(unmet_prerequisites(STAGES["news"], empty)) == 1


# ---------------------------------------------------------------------------
# Regressions from the code review. Each of these shipped once.
# ---------------------------------------------------------------------------


def test_a_bikram_sambat_byline_is_refused_not_read_as_gregorian():
    """`प्रकाशित: २०८२-०४-२०` parsed as date(2082, 4, 20) -- 57 years ahead.

    `\\d` matches Devanagari ०-९ and `int()` accepts them. Deviation A puts the
    date in the material IRI, so a BS date corrupted the idempotency key AND
    published a fabricated `datePublished` on a public material.
    """
    assert ns.extract_publication_date("<p>प्रकाशित: २०८२-०४-२०</p>") is None
    # Same year arriving through a meta tag, where %Y-%m-%d parses it happily.
    assert ns.extract_publication_date(
        '<html>"datePublished": "2082-04-20"</html>') is None
    # A real Gregorian date still works.
    assert ns.extract_publication_date(
        '<html>"datePublished": "2024-08-18"</html>') == date(2024, 8, 18)
    assert ns.extract_publication_date(
        "<p>प्रकाशित: 2024-08-18</p>") == date(2024, 8, 18)


def test_a_ddg_redirect_is_decoded_exactly_once():
    """The donor unquoted `uddg` after `parse_qs` had already decoded it.

    A Devanagari slug came back as literal Devanagari, which `urlopen` cannot
    encode -- so every Nepali article with a Devanagari path looked like a dead
    host. A literal `%20` decoded to a space: a different URL, then fetched and
    hashed into the material ident.
    """
    target = "https://setopati.test/समाचार"
    encoded = urllib.parse.quote(target, safe=":/")
    href = "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(encoded, safe="") + "&rut=x"
    out = ns.extract_ddg_redirect(href)
    assert out == encoded
    assert out.isascii(), "a non-ascii URL cannot be fetched at all"

    percent = "https://x.test/a%20b"
    assert ns.extract_ddg_redirect(
        "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(percent, safe="")) == percent


def test_save_page_now_actually_yields_a_permalink():
    """The post-save availability re-query was served from the cache.

    It replayed the pre-save MISS, so the whole `save_missing` branch could never
    return a permalink -- every unarchived article paid a 6s-throttled SPN request
    for nothing.
    """
    web = FakeWeb(snapshot=None,
                  snapshot_after_save="https://web.archive.org/web/2024/x")
    got = ns.resolve_permalink(web, "https://ekantipur.test/a",
                                        save_missing=True)
    assert web.saved, "Save Page Now was never requested"
    assert got == "https://web.archive.org/web/2024/x"


def test_a_partial_cheap_gate_reply_escalates_the_unanswered_to_premium():
    """`if gate:` was truthy on one parsed row, so unanswered indexes were
    marked GATE_REJECTED instead of escalating -- the opposite of fail-open."""
    articles = [
        ns.Article(url=f"https://ekantipur.test/{i}", title=f"t{i}",
                            text="x" * 400, published=date(2024, 7, 1))
        for i in range(4)
    ]
    seen = {}

    def invoke_json(*, system, content, max_tokens, tier, usage=None):
        if tier == "cheap":
            # Answers ONLY index 0: a reply truncated at max_tokens and repaired
            # by salvage_json looks exactly like this.
            return {"results": [{"index": 0, "relevant": True}]}
        seen["n"] = content.count("Candidate ")
        return {"results": [{"index": i, "relevant": False, "reason": "no"}
                            for i in range(seen["n"])]}

    ns.verify_batch(articles, case_payload(), invoke_json,
                             FakeUsage(), tier="premium")
    assert seen["n"] == 4, (
        "the 3 candidates the gate never judged must still reach premium")


def test_a_truncated_note_is_not_bindable():
    """`salvage_json` closes an open string, so an overflowing verify reply yields
    a note cut off mid-sentence. Non-blank passed the old check and would have
    been published as a case's evidence note."""
    truncated = ns.Verdict(
        relevant=True, confidence="high", event_type="verdict",
        summary="यो समाचार लेख यस मुद्दा (080-CR-0136) मा")
    assert not truncated.is_bindable
    full = ns.Verdict(
        relevant=True, confidence="high", event_type="verdict",
        summary="य" * ns.MIN_NOTE_CHARS)
    assert full.is_bindable


def test_an_unanswered_search_raises_rather_than_reporting_zero_results():
    """403/429 on every attempt returned `[]`, which reads as 'no coverage
    exists'. Only the one measured 202 anomaly body was caught."""

    class Blocked(FakeWeb):
        def _serve(self, url, kind):
            return 403, None

    with pytest.raises(ns.SearchUnavailable):
        ns.search(Blocked(), "अख्तियार मुद्दा")


def _case_with_news(n):
    case = case_payload()
    case["evidence"] = [
        {"material_iri": f"https://jawafdehi.org/material/news/2024010{i}.aaaaaaaa",
         "additional_details": "n",
         "material": {"material_type": "news", "urls": [
             {"link": f"https://old.test/{i}", "role": "RAW"}]}}
        for i in range(n)
    ]
    return case


def test_max_articles_is_a_total_not_a_per_run_addition():
    """A case with 4 news entries and --max-articles 5 could take 5 MORE.

    The saturation skip reads the constant as a TOTAL
    (`n_current >= args.max_articles`) while the selector read it as this run's
    addition, so the two disagreed and a case documented to cap at 5 could reach 9
    -- through a destructive whole-list replace.
    """
    # Already at the cap: no budget left, so it must not even build queries.
    outcome = en.collect_for_case(
        _case_with_news(4), FakeWeb(), lambda **kw: {}, FakeUsage(), max_articles=4)
    assert not outcome.accepted
    assert not outcome.queries, "a saturated case must not spend a single search"

    # One slot left of five: the budget is the REMAINDER, not another five.
    pairs = [
        (ns.Article(url=f"https://a.test/{i}", title="t", text="x",
                    published=date(2024, 7, i + 1)),
         ns.Verdict(relevant=True, confidence="high", event_type=event,
                    summary="य" * 200))
        for i, event in enumerate(("filing", "hearing", "verdict"))
    ]
    partial = ns.SearchOutcome()
    en._select_accepted(pairs, partial, 5 - 4, collections.Counter())
    assert len(partial.accepted) == 1, (
        "4 bound + --max-articles 5 must admit exactly 1 more")


def test_the_budget_records_what_it_cuts_off_instead_of_dropping_it():
    """A bare `break` dropped remaining verified pairs out of accepted,
    near_misses AND skipped -- premium tokens spent, nothing in the review file,
    and a `medium` verdict past the cap vanished silently."""
    outcome = ns.SearchOutcome()
    outcome.accepted.append(("already", "there"))
    pairs = [
        (ns.Article(url="https://a.test/1", title="t", text="x",
                             published=date(2024, 7, 1)),
         ns.Verdict(relevant=True, confidence="high", event_type="filing",
                             summary="य" * 200)),
        (ns.Article(url="https://a.test/2", title="t", text="x",
                             published=date(2024, 7, 2)),
         ns.Verdict(relevant=True, confidence="medium", event_type="filing",
                             summary="य" * 200)),
    ]
    en._select_accepted(pairs, outcome, 1, collections.Counter())
    assert len(outcome.accepted) == 1, "the budget still caps the accept"
    assert [s.reason for s in outcome.skipped] == [
        ns.SkipReason.BUDGET_REACHED]
    assert len(outcome.near_misses) == 1, (
        "a medium verdict past the cap is still owed to the reviewer")


def test_no_permalink_actually_suppresses_the_archive_lookup():
    """`--no-permalink` set args.permalink and nothing ever read it."""
    parser = en.build_parser()
    assert parser.parse_args([]).permalink is True
    assert parser.parse_args(["--no-permalink"]).permalink is False
    assert "args.permalink" in pathlib.Path(
        en.__file__).read_text(encoding="utf-8"), (
        "the flag must be READ, not merely registered")


def test_the_material_iri_comes_from_the_shapers_own_id():
    """Re-deriving it with a hardcoded host could bind evidence to an IRI the
    material was not created at, and the server does not check existence."""
    iri, doc = en._material_doc(
        ns.Article(url="https://ekantipur.test/a", title="t",
                            text="x" * 400, published=date(2024, 8, 18)),
        "य" * 200, None)
    assert iri == doc["@id"]


@pytest.fixture(autouse=True)
def _quiet(caplog):
    caplog.set_level(logging.CRITICAL, logger="casework.news_search")


# ---------------------------------------------------------------------------
# Swappable search providers. DuckDuckGo blocks datacentre addresses (measured
# 2026-08-06 from AS31898: 1/6 queries answered, and 45s/90s pacing did not
# help), so the backend has to be configurable. These fixtures are the vendors'
# published response shapes -- no live key exists in this repo, so a real
# credential is still owed one smoke query before any bulk run.
# ---------------------------------------------------------------------------

BRAVE_BODY = json.dumps({"web": {"results": [
    {"title": "विकल पौडेललाई कैद",
     "url": "https://ekantipur.test/2024/08/bikal",
     "description": "विशेष अदालतको फैसला"},
    {"title": "no url row", "url": "", "description": "dropped"},
]}})

SERPER_BODY = json.dumps({"organic": [
    {"title": "अख्तियारको आरोपपत्र",
     "link": "https://setopati.test/2024/06/ciaa",
     "snippet": "३२ जनाविरुद्ध"},
]})

TAVILY_BODY = json.dumps({"results": [
    {"title": "मेलम्ची बहस",
     "url": "https://ratopati.test/2024/07/melamchi",
     "content": "अन्तिम बहस सुरु"},
]})


class RecordedApi:
    """Serves one canned reply to `get`/`post_json`, recording what was sent."""

    def __init__(self, status=200, body="", *, capture=None):
        self.status, self.body = status, body
        self.capture = capture if capture is not None else []
        self.calls = {"search": 0}

    def get(self, url, kind, headers=None, expect_html=False, error_body=False):
        self.calls[kind] = self.calls.get(kind, 0) + 1
        self.capture.append(("GET", url, dict(headers or {})))
        # MODELS the real client rather than just accepting the argument: a
        # 4xx/5xx body is DISCARDED unless the caller opted in. Handing the body
        # back unconditionally is what let google_cse's quota-vs-revoked-key
        # branch pass its test while being unreachable in production -- the real
        # `get` returned `(403, None)` and the check read an empty string.
        if self.status >= 400 and not error_body:
            return self.status, None
        return self.status, self.body

    def post_json(self, url, kind, payload, headers=None):
        self.calls[kind] = self.calls.get(kind, 0) + 1
        self.capture.append(("POST", url, dict(headers or {}), payload))
        return self.status, self.body


@pytest.mark.parametrize("provider,env,body,expected_url,expected_snippet", [
    ("brave", "BRAVE_SEARCH_API_KEY", BRAVE_BODY,
     "https://ekantipur.test/2024/08/bikal", "विशेष अदालतको फैसला"),
    ("serper", "SERPER_API_KEY", SERPER_BODY,
     "https://setopati.test/2024/06/ciaa", "३२ जनाविरुद्ध"),
    ("tavily", "TAVILY_API_KEY", TAVILY_BODY,
     "https://ratopati.test/2024/07/melamchi", "अन्तिम बहस सुरु"),
])
def test_each_keyed_provider_normalises_to_the_same_shape(
        provider, env, body, expected_url, expected_snippet, monkeypatch):
    """Every backend must hand `collect_for_case` identical dicts. The vendors
    disagree on field names (`description`/`snippet`/`content`, `url`/`link`),
    and getting that mapping wrong yields candidates with no snippet -- which
    the verifier then grades on a title alone."""
    monkeypatch.setenv(ns.SEARCH_PROVIDER_ENV, provider)
    monkeypatch.setenv(env, "test-key")
    api = RecordedApi(200, body)
    results = ns.search(api, "विकल पौडेल")
    assert [r["url"] for r in results] == [expected_url]
    assert results[0]["snippet"] == expected_snippet
    assert results[0]["title"]


def test_a_missing_key_refuses_rather_than_falling_back_to_duckduckgo():
    """A silent downgrade would serve the anti-bot page the key exists to
    escape, and report every case as having no coverage."""
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.search(RecordedApi(), "क", provider="brave")
    assert "BRAVE_SEARCH_API_KEY" in str(exc.value)
    assert "duckduckgo" in str(exc.value)


@pytest.mark.parametrize("status,marker", [
    (401, "rejected the API key"),
    (403, "rejected the API key"),
    (429, "rate-limited or out of quota"),
    (500, "returned HTTP 500"),
])
def test_a_provider_refusal_raises_and_never_reads_as_no_coverage(
        status, marker, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.search(RecordedApi(status, "denied"), "क", provider="brave")
    assert marker in str(exc.value)


def test_an_html_error_page_is_not_mistaken_for_empty_results(monkeypatch):
    """A 200 carrying a proxy/WAF HTML page must not decode to zero results."""
    monkeypatch.setenv("SERPER_API_KEY", "k")
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.search(RecordedApi(200, "<html>gateway</html>"), "क",
                  provider="serper")
    assert "not JSON" in str(exc.value)


def test_a_keyed_provider_may_still_return_a_genuine_empty(monkeypatch):
    """The distinction the module turns on: refusal raises, no-match is []."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    assert ns.search(RecordedApi(200, json.dumps({"web": {"results": []}})),
                     "क", provider="brave") == []


def test_results_are_capped_per_query(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    many = {"web": {"results": [
        {"title": f"t{i}", "url": f"https://x.test/{i}", "description": "s"}
        for i in range(40)]}}
    got = ns.search(RecordedApi(200, json.dumps(many)), "क", provider="brave")
    assert len(got) == ns.SEARCH_RESULTS_PER_QUERY


def test_the_key_travels_in_the_header_not_the_query_string(monkeypatch):
    """A key in the URL lands in logs, caches and the client's cache key."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "secret-key")
    cap = []
    ns.search(RecordedApi(200, BRAVE_BODY, capture=cap), "क", provider="brave")
    method, url, headers = cap[0][0], cap[0][1], cap[0][2]
    assert method == "GET"
    assert "secret-key" not in url
    assert headers["X-Subscription-Token"] == "secret-key"


def test_default_provider_is_still_duckduckgo(monkeypatch):
    monkeypatch.delenv(ns.SEARCH_PROVIDER_ENV, raising=False)
    assert ns.resolve_search_provider()[0] == "duckduckgo"


def test_an_unknown_provider_name_is_refused_not_ignored(monkeypatch):
    monkeypatch.setenv(ns.SEARCH_PROVIDER_ENV, "gogle")
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.resolve_search_provider()
    assert "gogle" in str(exc.value)


def test_post_cache_keys_on_the_body_not_just_the_url(monkeypatch):
    """Every Serper/Tavily query POSTs to ONE url. Keying the cache on the url
    alone would serve query 1's articles as the answer to all twelve."""
    client = ns.WebClient(search_delay=0, proxy="")
    served = iter([(200, json.dumps({"organic": [
        {"title": "a", "link": "https://a.test/1", "snippet": "s"}]})),
        (200, json.dumps({"organic": [
            {"title": "b", "link": "https://b.test/2", "snippet": "s"}]}))])

    class _Op:
        """Patched at the OPENER, not at `urllib.request.urlopen`. Patching the
        module function let this test escape to the live network the moment
        `WebClient` started routing through `self._opener` -- it really did POST
        to serper.dev once, and got a 403. The client's own seam is the only
        one that stays true when the transport changes."""

        def open(self, request, timeout=None):
            status, body = next(served)
            return _FakeResponse(status, body)

    monkeypatch.setattr(client, "_opener", _Op())
    monkeypatch.setenv("SERPER_API_KEY", "k")
    first = ns.search(client, "पहिलो", provider="serper")
    second = ns.search(client, "दोस्रो", provider="serper")
    assert first[0]["url"] != second[0]["url"], (
        "two different queries must not share one cache entry")
    assert client.calls["search"] == 2


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")
        self.headers = types.SimpleNamespace(
            get_content_charset=lambda: "utf-8",
            get=lambda k, d=None: "application/json")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# SOCKS tunnel. A search API key needs a payment card, so the fallback for a
# blocked host is an SSH reverse tunnel (`ssh -R 1080`) from an unblocked
# connection. urllib cannot speak SOCKS unaided; these cover the seam.
# ---------------------------------------------------------------------------


def test_no_proxy_configured_leaves_the_client_on_the_plain_opener():
    client = ns.WebClient(proxy="")
    assert client.proxy == ""
    assert not any(type(h).__name__.startswith("_Tunnelled")
                   for h in client._opener.handlers)


@pytest.mark.skipif(importlib.util.find_spec("socks") is None,
                    reason="needs PySocks to build the opener; CI installs the "
                           "casework-proxy extra so this runs there")
def test_the_proxy_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(ns.SOCKS_PROXY_ENV, "127.0.0.1:1080")
    assert ns.WebClient().proxy == "127.0.0.1:1080"


def test_an_explicit_empty_proxy_overrides_the_environment(monkeypatch):
    """Tests and loopback smokes must be able to stay off the tunnel."""
    monkeypatch.setenv(ns.SOCKS_PROXY_ENV, "127.0.0.1:1080")
    assert ns.WebClient(proxy="").proxy == ""


@pytest.mark.parametrize("spec", ["1080", "not-a-port", "127.0.0.1:", ""])
def test_a_malformed_proxy_spec_is_refused_with_the_expected_form(spec):
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns._parse_proxy(spec)
    assert "127.0.0.1:1080" in str(exc.value), "the message must show the form"


@pytest.mark.skipif(importlib.util.find_spec("socks") is None,
                    reason="PySocks absent: uv sync --extra casework-proxy. CI "
                           "installs it so this DOES run there -- the skip is "
                           "for a local venv without the optional extra.")
def test_the_tunnel_does_not_capture_the_whole_process(monkeypatch):
    """THE POINT OF THE SCOPED OPENER. The common PySocks recipe replaces
    `socket.socket` globally, which would route the case API and the LLM
    provider through the operator's personal connection as a side effect of a
    search workaround. Only this client's sockets may move."""
    import socket as socket_module
    original = socket_module.socket
    client = ns.WebClient(proxy="127.0.0.1:1080")
    assert socket_module.socket is original, "global socket was monkeypatched"
    assert any(type(h).__name__ == "_TunnelledHTTPSHandler" or
               type(h).__name__.startswith("_HTTPS")
               for h in client._opener.handlers), "no tunnelled handler installed"


def test_a_tunnelled_client_still_throttles_and_caches(monkeypatch):
    """A proxy swap must not bypass the pacing -- the reason WebClient exists."""
    opened = []

    class _Op:
        def open(self, request, timeout=None):
            opened.append(request.full_url)
            return _FakeResponse(200, "{}")

    client = ns.WebClient(proxy="", search_delay=0)
    monkeypatch.setattr(client, "_opener", _Op())
    client.get("https://x.test/a", "search")
    client.get("https://x.test/a", "search")
    assert len(opened) == 1, "the cache must still short-circuit the second call"
    assert client.calls["search"] == 1


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    """Enforce this module's docstring. Nothing enforced it, and a test really
    did POST to serper.dev once -- it patched `urllib.request.urlopen` while
    `WebClient` had moved to `self._opener`, so the stub silently stopped
    intercepting and the call went out. Patch at a seam the transport cannot
    slip out of: the socket itself."""
    import socket as socket_module

    def _refuse(self, address):
        raise AssertionError(
            f"a test tried to open a real connection to {address!r}. Stub the "
            f"client's `_opener` (or use FakeWeb) rather than patching "
            f"`urllib.request.urlopen`, which the transport no longer calls.")

    monkeypatch.setattr(socket_module.socket, "connect", _refuse)


# ---------------------------------------------------------------------------
# The date rule. Notes were carrying relative and year-less dates -- "hearing
# began आइतबार", "acquitted असार १८ गते" -- into a permanently stored record.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ad,expected_bs,expected_nepali", [
    (date(2024, 6, 16), "2081-03-02", "२०८१ असार २"),
    (date(2024, 8, 18), "2081-05-02", "२०८१ भदौ २"),
    (date(2023, 4, 14), "2080-01-01", "२०८० बैशाख १"),   # Nepali new year
])
def test_the_publication_date_is_rendered_in_both_calendars(
        ad, expected_bs, expected_nepali):
    """Converted IN CODE. An LLM doing AD<->BS arithmetic is how a provenance
    note ends up citing a date that never existed."""
    rendered = ns.format_publication_date(ad)
    assert ad.isoformat() in rendered
    assert expected_bs in rendered
    assert expected_nepali in rendered


def test_an_undated_article_renders_empty_rather_than_guessing():
    assert ns.format_publication_date(None) == ""


def test_the_prompt_actually_carries_the_publication_date():
    """The rule below is unfollowable without this. The candidate block listed
    Title/URL/Excerpt only, so a model told to resolve "आइतबार" into a real date
    had nothing to resolve it against."""
    prompt = ns._batch_prompt("ctx", [ns.Article(
        url="https://ekantipur.test/a", title="t", text="x" * 300,
        published=date(2024, 6, 16))])
    assert "Published: 2024-06-16" in prompt
    assert "२०८१ असार २" in prompt


def test_a_dateless_candidate_says_unknown_and_does_not_crash():
    prompt = ns._batch_prompt("ctx", [ns.Article(
        url="https://ekantipur.test/a", title="t", text="x" * 300,
        published=None)])
    assert "Published: unknown" in prompt


def test_the_verify_prompt_states_the_date_rule():
    p = ns.VERIFY_SYSTEM_PROMPT
    assert "आइतबार" in p, "the relative-day ban must name a concrete example"
    assert "COPY from it" in p, "the model must copy the date, not convert it"
    assert "omit the date rather than guessing" in p
    # A date the article itself states has no entry on the "Published" line, so
    # a model forbidden from converting fell back to Latin script -- one bound
    # note came back reading "18 February 2024" inside Devanagari prose.
    assert "18 February 2024" in p and "Latin-script date" in p, (
        "the rule must cover dates stated IN the article, not just the byline")


def test_a_broken_bs_conversion_still_yields_the_gregorian_anchor(monkeypatch):
    """`ad_to_bs` is best-effort (out-of-range year, missing `nepali` package).
    Losing the BS half must not lose the anchor that makes 'आइतबार' resolvable."""
    import jawafdehi_shared.dates as shared_dates
    monkeypatch.setattr(shared_dates, "ad_to_bs", lambda _: None)
    assert ns.format_publication_date(date(2024, 6, 16)) == "2024-06-16"


# ---------------------------------------------------------------------------
# Google Programmable Search. The only provider signable-up without a payment
# card, so it is the one this project can actually reach -- at 100 queries/day.
# ---------------------------------------------------------------------------

GOOGLE_BODY = json.dumps({"items": [
    {"title": "विकल पौडेललाई कैद",
     "link": "https://ekantipur.test/2024/08/bikal",
     "snippet": "विशेष अदालतको फैसला"},
]})


def test_google_cse_normalises_link_and_snippet(monkeypatch):
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_CX", "cx123")
    got = ns.search(RecordedApi(200, GOOGLE_BODY), "क", provider="google_cse")
    assert got == [{"title": "विकल पौडेललाई कैद",
                    "url": "https://ekantipur.test/2024/08/bikal",
                    "snippet": "विशेष अदालतको फैसला"}]


def test_google_cse_without_a_cx_says_so_instead_of_400ing(monkeypatch):
    """Two credentials, unlike every other provider. A key with no engine id
    returns a bare 400 from Google, which reads as a mystery."""
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
    monkeypatch.delenv("GOOGLE_CSE_CX", raising=False)
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.search(RecordedApi(), "क", provider="google_cse")
    assert "GOOGLE_CSE_CX" in str(exc.value)
    assert "programmablesearchengine" in str(exc.value)


def test_google_cse_quota_exhaustion_is_not_reported_as_a_bad_key(monkeypatch):
    """Both arrive as HTTP 403 and the operator's next action is opposite:
    wait for the reset, versus re-issue the credential."""
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_CX", "cx123")
    body = json.dumps({"error": {"code": 403, "errors": [
        {"reason": "dailyLimitExceeded",
         "message": "Quota exceeded for quota metric 'Queries'"}]}})
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.search(RecordedApi(403, body), "क", provider="google_cse")
    msg = str(exc.value)
    assert "quota exhausted" in msg and "NOT a bad key" in msg
    assert "rejected the API key" not in msg


def test_google_cse_a_genuinely_bad_key_still_says_bad_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_CX", "cx123")
    body = json.dumps({"error": {"code": 403, "errors": [
        {"reason": "forbidden", "message": "API key not valid"}]}})
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.search(RecordedApi(403, body), "क", provider="google_cse")
    assert "rejected the API key" in str(exc.value)


def test_google_cse_no_items_key_is_a_genuine_no_match(monkeypatch):
    """Google omits "items" entirely when nothing matched. That is [], not an
    error -- the distinction this module turns on."""
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_CX", "cx123")
    body = json.dumps({"searchInformation": {"totalResults": "0"}})
    assert ns.search(RecordedApi(200, body), "क", provider="google_cse") == []


def test_google_cse_never_asks_for_more_than_the_api_allows(monkeypatch):
    """`num` maxes out at 10; a larger value is a 400."""
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_CX", "cx123")
    cap = []
    ns.search(RecordedApi(200, GOOGLE_BODY, capture=cap), "क",
              provider="google_cse")
    num = int(urllib.parse.parse_qs(
        urllib.parse.urlparse(cap[0][1]).query)["num"][0])
    assert 1 <= num <= 10


def test_a_credential_in_the_query_string_is_redacted_before_logging():
    """google_cse takes its key in the URL -- no header option -- and that URL
    reaches a debug log on transport failure."""
    red = ns.redact_url(
        "https://www.googleapis.com/customsearch/v1"
        "?key=AIzaSyREAL_SECRET_VALUE&cx=abc123&q=%E0%A4%95&num=8")
    assert "AIzaSyREAL_SECRET_VALUE" not in red
    assert "key=REDACTED" in red
    assert "cx=abc123" in red, "only the secret is masked, not the whole query"


def test_redaction_leaves_an_ordinary_url_untouched():
    url = "https://html.duckduckgo.com/html/?q=nepal+corruption"
    assert ns.redact_url(url) == url


# ---------------------------------------------------------------------------
# Review findings on the provider/proxy/date work. Each of these shipped once.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["[]", '["a","b"]', '"a string"', "42"])
def test_valid_json_that_is_not_an_object_raises_rather_than_crashing(
        body, monkeypatch):
    """Every `extract` calls `.get` on the parsed payload. A proxy or error shim
    answering `[]` with a 200 produced an AttributeError -- which is NOT
    SearchUnavailable, so it slipped past the enricher's abort handler and killed
    the run with a traceback instead of naming the backend."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.search(RecordedApi(200, body), "क", provider="brave")
    assert "not an object" in str(exc.value)


def test_a_datetime_publication_date_does_not_put_a_clock_time_in_the_note():
    """`isoformat()` on a datetime emits "2024-06-16T10:30:00" -- a wall-clock
    time we do not know, written into a permanent evidence note."""
    from datetime import datetime
    rendered = ns.format_publication_date(datetime(2024, 6, 16, 10, 30))
    assert rendered.startswith("2024-06-16 ")
    assert "T10:30" not in rendered
    assert rendered == ns.format_publication_date(date(2024, 6, 16))


def test_a_broken_proxy_is_reported_at_startup_not_as_a_traceback(monkeypatch,
                                                                  capsys):
    """`WebClient` is built OUTSIDE the per-case try, so a malformed
    $CASEWORK_SOCKS_PROXY escaped as a bare traceback. The preflight catches it
    before any case is touched."""
    monkeypatch.setenv(ns.SOCKS_PROXY_ENV, "not-a-host-port")
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.WebClient()
    assert "127.0.0.1:1080" in str(exc.value), "the message shows the form"


def test_the_enricher_preflights_search_config_before_touching_a_case():
    """The check must sit ahead of the case loop, not inside it."""
    src = pathlib.Path(en.__file__).read_text(encoding="utf-8")
    preflight = src.index("resolve_search_provider()")
    loop = src.index("for index, summary in enumerate(cases, 1):")
    assert preflight < loop, "config is validated after work has begun"
    assert "search is not configured" in src


# ---------------------------------------------------------------------------
# PR #429 review findings. Each of these shipped once.
# ---------------------------------------------------------------------------


def test_the_real_client_hands_a_4xx_body_to_a_provider_that_asks(monkeypatch):
    """The quota-vs-revoked-key branch was UNREACHABLE. `get` returned
    `(exc.code, None)` for an HTTPError, so google_cse's body check always read
    "" and every 403 said "rejected the API key" -- sending the operator to
    re-issue a credential when the fix was to wait for the reset."""
    import io
    import urllib.error

    body = json.dumps({"error": {"code": 403, "errors": [
        {"reason": "dailyLimitExceeded"}], "message": "Quota exceeded"}}).encode()

    def raise_403(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                     io.BytesIO(body))

    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_CX", "cx")
    client = ns.WebClient(search_delay=0, proxy="")
    monkeypatch.setattr(client, "_opener",
                        types.SimpleNamespace(open=raise_403))
    with pytest.raises(ns.SearchUnavailable) as exc:
        ns.search(client, "क", provider="google_cse")
    assert "quota exhausted" in str(exc.value)
    assert "rejected the API key" not in str(exc.value)


def test_an_html_error_page_is_still_never_parsed_as_search_results(monkeypatch):
    """The counterpart risk of the fix above. `error_body` is opt-in precisely
    so the HTML callers keep getting None on a 4xx -- handing a 403 error page to
    `parse_ddg_html` would turn a refusal into "no coverage exists"."""
    import io
    import urllib.error

    page = b"<html><body>403 Forbidden, go away</body></html>"

    def raise_403(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                     io.BytesIO(page))

    client = ns.WebClient(search_delay=0, proxy="")
    monkeypatch.setattr(client, "_opener",
                        types.SimpleNamespace(open=raise_403))
    status, body = client.get("https://x.test/", "search")
    assert (status, body) == (403, None), "the HTML path must not see the body"
    assert client.get("https://y.test/", "search", error_body=True)[1] is not None


@pytest.mark.parametrize("payload", [
    {"web": {"results": ["a bare string"]}},
    {"web": {"results": [42]}},
    {"web": {"results": "not even a list"}},
    {"web": []},
])
def test_a_malformed_result_row_raises_rather_than_crashing(payload, monkeypatch):
    """The top-level object guard did not reach the rows. Each `extract` calls
    `.get` per element, so a shim answering a list of strings raised
    AttributeError -- past the abort handler, traceback, run dead."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    try:
        got = ns.search(RecordedApi(200, json.dumps(payload)), "क",
                        provider="brave")
    except ns.SearchUnavailable:
        return                      # refused, which is the contract
    assert got == [], f"a malformed payload must not yield candidates: {got!r}"


def test_a_review_row_survives_an_article_with_no_title():
    """`_review_row` runs on the ERROR path too, inside the except block, so a
    TypeError here aborts before `review.write()` and loses the review file for
    every case already processed."""
    article = ns.Article(url="https://ekantipur.test/a", title=None,
                         text="य" * 400, published=date(2024, 8, 18))
    verdict = ns.Verdict(relevant=True, confidence="high", event_type="verdict",
                         summary="य" * 400)
    outcome = ns.SearchOutcome()
    outcome.accepted.append((article, verdict))
    plan = en.NewsPlan(slug="slug", action="WOULD_BIND", outcome=outcome,
                       materials=[("https://jawafdehi.org/material/news/20240818.abc",
                                   {}, "य" * 400, article)])
    row = en._review_row("slug", "would-enrich", plan)
    assert row is not None
    assert any("news article" in str(src) for src in row.sources), (
        "the source label must use the outlet fallback, not raise a TypeError")


def test_a_failed_preflight_exits_non_zero(monkeypatch):
    """`main()` is invoked bare at the bottom of the module, so `return report`
    exits 0 and a misconfigured provider reports SUCCESS to a scheduler.

    Driven through `main()` rather than grepped out of the source, so a
    refactor that reintroduces the bug fails here instead of passing on a
    string that happens to still be present."""
    monkeypatch.setenv("CASEWORK_SEARCH_PROVIDER", "brave")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, FakeApi(case_payload()), FakeWeb(),
                 stub_invoke_json())
    assert exc.value.code not in (0, None), (
        "a keyed provider with no key must exit non-zero")
    assert "brave" in str(exc.value).lower(), exc.value


def test_a_missing_key_is_caught_before_the_case_list_is_fetched(monkeypatch):
    """`resolve_search_provider` used to validate only the provider NAME, so a
    missing key sailed through the preflight, fetched the case list, printed the
    run header and then aborted on the first query of case 1 -- which reads like
    the backend went down mid-run rather than like it was never configured."""
    monkeypatch.setenv("CASEWORK_SEARCH_PROVIDER", "brave")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    api = FakeApi(case_payload())
    with pytest.raises(SystemExit):
        run_main(monkeypatch, api, FakeWeb(), stub_invoke_json())
    assert api.fetched == 0, (
        "the preflight must fail before a single case is fetched")


def test_an_aborted_run_exits_non_zero_but_still_ships_what_it_had(monkeypatch,
                                                                  capsys):
    """A backend that dies MID-run -- rate limits, the anti-bot page, a revoked
    key -- aborts the loop. Returning `report` there exits 0 and tells a
    scheduler that a batch which stopped at case 1 of N succeeded. The summary
    and the review file must still be written before the non-zero exit."""
    class _DeadSearch(FakeWeb):
        def get(self, url, kind, **kwargs):
            if kind == "search":
                raise ns.SearchUnavailable("429 from the provider")
            return super().get(url, kind, **kwargs)

    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, FakeApi(case_payload()), _DeadSearch(),
                 stub_invoke_json())
    assert exc.value.code not in (0, None)
    out = capsys.readouterr()
    assert "review file:" in out.out, "the review file must still be written"
    assert "ABORTED at case 1/1" in out.err


def test_bind_materials_records_the_reciprocal_duplicate_contract():
    """Both stages PATCH the same destructive whole-list /evidence, so both
    normalisers must stay identical. Only one side said so."""
    src = pathlib.Path(en.__file__).parent.joinpath("bind_materials.py").read_text(encoding="utf-8")
    assert src.count("DELIBERATELY DUPLICATED") == 2
    assert "enrich_news_articles" in src


# ---------------------------------------------------------------------------
# Search budget. Brave bills the saved card past its $5 monthly credit and
# publishes no spending cap of its own, so this is the only thing standing
# between a runaway loop and a real charge. Tested at the seam it is enforced
# at -- the client -- not at the provider functions, because the point of
# putting it in the client is that a NEW provider cannot forget it.
# ---------------------------------------------------------------------------

def _budget(tmp_path, limit, spent=None, provider="brave"):
    path = tmp_path / "search-budget.json"
    budget = ns.SearchBudget(limit, provider, path=path)
    if spent is not None:
        budget.spent = spent
        budget._save()
        budget = ns.SearchBudget(limit, provider, path=path)
    return budget


def test_the_budget_refuses_the_query_that_would_breach_it(tmp_path):
    budget = _budget(tmp_path, limit=3)
    for _ in range(3):
        budget.spend()
    with pytest.raises(ns.SearchBudgetExceeded) as exc:
        budget.spend()
    assert budget.spent == 3, "the refused query must not be charged"
    assert "Nothing was sent" in str(exc.value)


def test_budget_exhaustion_aborts_the_whole_run_not_one_case():
    """`SearchBudgetExceeded` must be a `SearchUnavailable`: the enricher already
    treats that as abort-the-run, and continuing would write one 'found nothing'
    row per remaining case -- a review file that looks complete and is not."""
    assert issubclass(ns.SearchBudgetExceeded, ns.SearchUnavailable)


def test_the_ledger_survives_across_runs(tmp_path):
    """A per-run cap is no protection: three re-runs of a 300-query batch spend
    the month. The count has to persist."""
    first = _budget(tmp_path, limit=10)
    for _ in range(4):
        first.spend()
    second = ns.SearchBudget(10, "brave", path=tmp_path / "search-budget.json")
    assert second.spent == 4, "a fresh process must see what the last one spent"
    assert second.remaining() == 6


def test_a_new_month_restores_the_allowance(tmp_path):
    """The credit is granted monthly, so the ledger resets with it -- otherwise
    the cap silently becomes permanent."""
    path = tmp_path / "search-budget.json"
    path.write_text(json.dumps({"spent": {"brave": {"bucket": "1999-01",
                                                    "spent": 900}}}))
    assert ns.SearchBudget(900, "brave", path=path).spent == 0


def test_a_corrupt_or_missing_ledger_does_not_kill_the_run(tmp_path):
    path = tmp_path / "search-budget.json"
    assert ns.SearchBudget(10, "brave", path=path).spent == 0        # missing
    path.write_text("{not json")
    assert ns.SearchBudget(10, "brave", path=path).spent == 0        # corrupt
    path.write_text(json.dumps({"spent": {"brave": "many"}}))
    assert ns.SearchBudget(10, "brave", path=path).spent == 0        # wrong type
    path.write_text(json.dumps({"month": "2026-08", "spent": 5}))
    assert ns.SearchBudget(10, "brave", path=path).spent == 0        # old format


def test_an_unwritable_ledger_warns_rather_than_failing(tmp_path, caplog):
    """Losing the ledger must not sink an otherwise fine run, but it removes the
    protection silently -- so it has to say so."""
    budget = ns.SearchBudget(10, "brave", path=tmp_path / "nodir" / "x" / "b.json")
    budget.path = tmp_path            # a directory: write_text always fails
    caplog.set_level(logging.WARNING, logger="casework.news_search")
    budget.spend()
    assert any("search budget ledger" in r.getMessage() for r in caplog.records)
    assert budget.spent == 1, "the run continues; only the persistence is lost"


def test_only_search_is_charged_not_fetching_or_archiving(tmp_path, monkeypatch):
    """Fetching an article hits the publisher and archiving hits Wayback. Neither
    is billed by the search provider, and charging them would exhaust the cap on
    requests nobody invoices us for."""
    budget = _budget(tmp_path, limit=100)
    client = ns.WebClient(search_delay=0, fetch_delay=0, save_delay=0,
                          proxy="", budget=budget)
    monkeypatch.setattr(client, "_opener", _OpenerReturning("<html></html>"))
    for kind in ("fetch", "archive", "save"):
        client.get(f"https://example.test/{kind}", kind)
    assert budget.spent == 0
    client.get("https://example.test/q", "search")
    assert budget.spent == 1


def test_a_cached_search_is_not_charged_twice(tmp_path, monkeypatch):
    """A cache hit sends nothing, so charging it would bill us for a request that
    never left the process and make the ledger disagree with the provider."""
    budget = _budget(tmp_path, limit=100)
    client = ns.WebClient(search_delay=0, fetch_delay=0, proxy="", budget=budget)
    monkeypatch.setattr(client, "_opener", _OpenerReturning("<html></html>"))
    client.get("https://example.test/same", "search")
    client.get("https://example.test/same", "search")
    assert budget.spent == 1


def test_the_post_providers_cannot_bypass_the_budget(tmp_path, monkeypatch):
    """Serper and Tavily are POST-only. A cap enforced on `get` alone would be
    silently absent for two of the five providers."""
    budget = _budget(tmp_path, limit=100)
    client = ns.WebClient(search_delay=0, proxy="", budget=budget)
    monkeypatch.setattr(client, "_opener", _OpenerReturning('{"organic": []}'))
    client.post_json("https://example.test/s", "search", {"q": "क"})
    assert budget.spent == 1


def test_an_exhausted_budget_stops_the_request_reaching_the_network(tmp_path):
    budget = _budget(tmp_path, limit=1, spent=1)
    client = ns.WebClient(search_delay=0, proxy="", budget=budget)

    class _Explode:
        def open(self, *a, **k):
            raise AssertionError("the request must never be sent")

    client._opener = _Explode()
    with pytest.raises(ns.SearchBudgetExceeded):
        client.get("https://example.test/q", "search")


def test_duckduckgo_is_uncapped_but_keyed_providers_are_not():
    """The cap exists because a keyed provider bills a card. DuckDuckGo cannot,
    and a 238-case pass is ~2,900 queries -- any credit-sized limit would abort
    a legitimate free run."""
    assert ns.default_budget_for("duckduckgo") == 0
    for keyed in ("brave", "serper", "tavily", "google_cse"):
        assert ns.default_budget_for(keyed) > 0


class _OpenerReturning:
    """Minimal stand-in for the client's urllib opener. Never touches a socket."""

    def __init__(self, body):
        self.body = body

    def open(self, request, timeout=None):
        return _Response(self.body)


class _Response:
    def __init__(self, body):
        self.body = body.encode()
        self.status = 200
        self.headers = _Headers()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Headers:
    def get(self, name, default=None):
        return "text/html" if name.lower() == "content-type" else default

    def get_content_charset(self):
        return "utf-8"


class ExhaustedWeb(FakeWeb):
    """A client whose budget ran out mid-run, exactly as the real one behaves."""

    def get(self, url, kind, headers=None, expect_html=False, error_body=False):
        if kind == "search":
            raise ns.SearchBudgetExceeded(
                "search budget exhausted: 900 of 900 queries already spent this "
                "month (2026-08). Nothing was sent.")
        return super().get(url, kind, headers=headers, expect_html=expect_html,
                           error_body=error_body)


def test_an_exhausted_budget_aborts_the_run_and_writes_nothing(monkeypatch,
                                                               tmp_path):
    """End-to-end proof the cap actually stops the enricher. A budget that only
    raised deep in the client, and got swallowed by a per-case handler, would
    keep spending on every remaining case -- which is the failure it exists to
    prevent."""
    api = FakeApi(case_payload())
    review = tmp_path / "review.md"
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, api, ExhaustedWeb(), stub_invoke_json(),
                 ["--review-file", str(review)])
    assert exc.value.code not in (0, None), (
        "a batch stopped by the cap did not complete, and must say so")
    assert "budget exhausted" in review.read_text(encoding="utf-8")
    assert api.materials == [] and api.replaced == [], (
        "an aborted run must not have written anything")


def test_search_budget_resolution_prefers_the_flag_then_the_env(monkeypatch,
                                                                tmp_path):
    monkeypatch.setenv(ns.SEARCH_LEDGER_ENV, str(tmp_path / "b.json"))
    monkeypatch.setenv(ns.SEARCH_BUDGET_ENV, "50")
    flag = en._build_budget(types.SimpleNamespace(search_budget=7), "brave")
    assert flag.limit == 7, "--search-budget must win over the env var"
    env = en._build_budget(types.SimpleNamespace(search_budget=None), "brave")
    assert env.limit == 50
    monkeypatch.delenv(ns.SEARCH_BUDGET_ENV)
    fallback = en._build_budget(types.SimpleNamespace(search_budget=None), "brave")
    assert fallback.limit == ns.DEFAULT_KEYED_BUDGET


def test_an_explicit_zero_disables_the_cap_but_absent_does_not(monkeypatch,
                                                               tmp_path):
    """`0` and "not given" have to stay distinguishable, or an operator who wants
    an uncapped free-provider run silently gets the keyed default instead."""
    monkeypatch.setenv(ns.SEARCH_LEDGER_ENV, str(tmp_path / "b.json"))
    monkeypatch.delenv(ns.SEARCH_BUDGET_ENV, raising=False)
    assert en._build_budget(types.SimpleNamespace(search_budget=0), "brave") is None
    assert en._build_budget(types.SimpleNamespace(search_budget=None),
                            "duckduckgo") is None
    assert en._build_budget(types.SimpleNamespace(search_budget=None),
                            "brave") is not None


def test_a_bad_search_budget_is_rejected_not_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv(ns.SEARCH_LEDGER_ENV, str(tmp_path / "b.json"))
    monkeypatch.setenv(ns.SEARCH_BUDGET_ENV, "lots")
    with pytest.raises(SystemExit):
        en._build_budget(types.SimpleNamespace(search_budget=None), "brave")
    monkeypatch.delenv(ns.SEARCH_BUDGET_ENV)
    with pytest.raises(SystemExit):
        en._build_budget(types.SimpleNamespace(search_budget=-1), "brave")


def test_the_preflight_warns_when_the_batch_cannot_fit(tmp_path, capsys):
    """Aborting at case 19 of 25 leaves a review file that is silently partial.
    The operator would rather cut the batch than find out afterwards."""
    budget = ns.SearchBudget(100, "brave", path=tmp_path / "b.json")
    en._check_budget_fits(budget, n_cases=25)
    out = capsys.readouterr().out
    # Derived, not hard-coded: `affordable = remaining // QUERY_LIMIT`, so a
    # change to QUERY_LIMIT should fail on the constant that moved rather than
    # on a warning string.
    affordable = 100 // ns.QUERY_LIMIT
    assert "WARNING" in out and f"--limit {affordable}" in out, out
    en._check_budget_fits(ns.SearchBudget(1000, "brave", path=tmp_path / "c.json"),
                          25)
    assert "WARNING" not in capsys.readouterr().out


def test_each_provider_gets_its_own_count(tmp_path):
    """The original bug: one shared counter. Serper's one-time 2,500 and Brave's
    monthly ~1,000 are separate allowances, so spending one must not consume the
    other."""
    path = tmp_path / "b.json"
    brave = ns.SearchBudget(900, "brave", path=path)
    serper = ns.SearchBudget(2200, "serper", path=path)
    for _ in range(50):
        serper.spend()
    assert ns.SearchBudget(900, "brave", path=path).spent == 0
    assert ns.SearchBudget(2200, "serper", path=path).spent == 50
    brave.spend()
    assert ns.SearchBudget(2200, "serper", path=path).spent == 50, (
        "writing one provider's row must not erase the other's")


def test_a_one_time_allowance_never_resets(tmp_path):
    """Serper's 2,500 credits are granted ONCE. A month-keyed counter would hand
    the whole allowance back every 1st and quietly overspend it."""
    path = tmp_path / "b.json"
    jan = ns.SearchBudget(2200, "serper", path=path,
                          now=lambda: ns.datetime(2026, 1, 5,
                                                  tzinfo=ns.timezone.utc))
    jan.spend()
    later = ns.SearchBudget(2200, "serper", path=path,
                            now=lambda: ns.datetime(2027, 9, 5,
                                                    tzinfo=ns.timezone.utc))
    assert later.spent == 1, "a one-time quota must not refresh with the calendar"


def test_a_daily_allowance_resets_daily(tmp_path):
    """Google's 100 is per DAY. A monthly counter would refuse for the rest of
    the month after one busy afternoon."""
    path = tmp_path / "b.json"
    day1 = ns.SearchBudget(90, "google_cse", path=path,
                           now=lambda: ns.datetime(2026, 8, 6,
                                                   tzinfo=ns.timezone.utc))
    day1.spend()
    day2 = ns.SearchBudget(90, "google_cse", path=path,
                           now=lambda: ns.datetime(2026, 8, 7,
                                                   tzinfo=ns.timezone.utc))
    assert day2.spent == 0


def test_the_ledger_survives_two_providers_writing_alternately(tmp_path):
    """Running a Brave batch and a Serper batch at once is a thing an operator
    would reasonably do, and it is exactly when an unlocked read-modify-write
    drops one of the two rows. Committed with `os.replace`, so a crash
    mid-write also cannot leave a truncated file that `_read_all` would read as
    zero spent -- handing back an allowance that is gone."""
    ledger = tmp_path / "b.json"
    brave = ns.SearchBudget(900, "brave", path=ledger)
    serper = ns.SearchBudget(2200, "serper", path=ledger)
    for _ in range(2):
        brave.spend()
    for _ in range(3):
        serper.spend()

    assert ns.SearchBudget(900, "brave", path=ledger).spent == 2
    assert ns.SearchBudget(2200, "serper", path=ledger).spent == 3
    assert not list(tmp_path.glob("*.tmp")), (
        "the atomic write must not leave a temp file behind")


def test_the_quota_table_matches_each_vendors_real_allowance():
    """These caps are the only thing standing between a run and a real charge or
    a burnt one-time allowance, so both halves are pinned to the vendor's
    published free tier as measured on 2026-08-06 -- each a little under it, so
    a rounding error cannot overspend. `cap > 0` is not enough: a `brave` cap
    typo'd to 9000 passes that and bills the card ten times over."""
    assert ns.PROVIDER_QUOTAS == {
        "brave": (900, "month"),        # $5 credit ~= 1,000 at $5/1,000
        "serper": (2200, "once"),       # 2,500 free credits, NOT recurring
        "tavily": (900, "month"),       # 1,000 credits/month
        "google_cse": (90, "day"),      # 100/day
    }
    for provider, (cap, _period) in ns.PROVIDER_QUOTAS.items():
        assert cap == ns.default_budget_for(provider), provider


def test_an_unreadable_ledger_says_so_instead_of_resetting_quietly(tmp_path,
                                                                   caplog):
    """A ledger we cannot parse resets the counter to zero, which hands back an
    allowance that may already be spent. On Brave that is the difference
    between the free credit and a charged card, so it must not be silent."""
    ledger = tmp_path / "b.json"
    ledger.write_text(json.dumps({"month": "2026-08", "spent": 870}))
    caplog.set_level(logging.WARNING, logger="casework.news_search")
    budget = ns.SearchBudget(900, "brave", path=ledger)
    assert budget.spent == 0
    assert any("not in the expected format" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_the_exhaustion_message_names_the_provider_and_the_window(tmp_path):
    """Two providers share the ledger, so 'budget exhausted' without a name
    sends the operator to the wrong dashboard."""
    budget = ns.SearchBudget(1, "serper", path=tmp_path / "b.json")
    budget.spend()
    with pytest.raises(ns.SearchBudgetExceeded) as exc:
        budget.spend()
    assert "serper" in str(exc.value)
    assert "does not refresh" in str(exc.value)


# ---------------------------------------------------------------------------
# Timeline-aware queries. Every constant below is measured against the 138
# deduplicated news articles bound to PUBLISHED cases, so the tests assert the
# MEASUREMENT held, not that the code does what it happens to do.
# ---------------------------------------------------------------------------

def _timeline_case(entries, accused="Bikal Poudel"):
    return {"title": "test case", "key_allegations": [], "court_cases": [],
            "entities": [{"display_name": accused, "type": "accused"}],
            "timeline": entries}


def test_the_timeline_says_which_stages_a_case_reached():
    case = _timeline_case([
        {"date": "2023-08-29", "date_bs": "2080-05-12",
         "title": "अनुसन्धान प्रतिवेदन पेश", "description": ""},
        {"date": "2025-05-13", "date_bs": "2082-01-30",
         "title": "अख्तियारद्वारा अभियोगपत्र दायर", "description": ""},
        {"date": "2026-02-05", "date_bs": "2082-10-22",
         "title": "विशेष अदालतको आंशिक ठहर फैसला", "description": ""},
    ])
    reached = ns.timeline_events(case)
    assert set(reached) == {"investigation", "filing", "verdict"}
    assert reached["verdict"] == ("2026", "2082")
    assert "appeal" not in reached, (
        "a case that never went to appeal must not spend a slot asking for one")


def test_the_verdict_is_asked_about_before_the_investigation():
    """19 of 61 published cases carry a verdict no article covers, so on a
    decided case the verdict query must not queue behind an investigation one.

    Asserted on `_event_queries`, and on the DEVANAGARI block of the final
    list, because `normalize_search_queries` promotes the first four English
    queries to the front regardless of stage (donor:523). Stage ordering
    governs everything after that block, and asserting otherwise would be
    asserting against the donor's pinned interleave.
    """
    case = _timeline_case([
        {"date": "2023-01-01", "date_bs": "2079-09-17",
         "title": "अनुसन्धान सुरु", "description": ""},
        {"date": "2026-02-05", "date_bs": "2082-10-22",
         "title": "विशेष अदालतको ठहर", "description": ""},
    ])
    raw = ns._event_queries("Bikal Poudel", case)
    assert "ठहर" in raw[0], raw[:3]

    devanagari = [q for q in ns.build_queries(case)
                  if not ns.is_english_query(q)]
    verdict_at = next(i for i, q in enumerate(devanagari) if "ठहर" in q)
    invest_at = next((i for i, q in enumerate(devanagari)
                      if "अनुसन्धान" in q), 99)
    assert verdict_at < invest_at, devanagari


def test_the_latest_entry_wins_on_the_whole_date_not_the_year():
    """BS rolls over in mid-April, so two verdicts in one AD year can sit in
    different BS years. Comparing only `[:4]` left the winner to list order and
    could narrow the query onto the year the story was NOT filed under."""
    case = _timeline_case([
        {"date": "2026-11-20", "date_bs": "2083-08-04",
         "title": "विशेष अदालतको ठहर", "description": ""},
        {"date": "2026-01-10", "date_bs": "2082-09-26",
         "title": "विशेष अदालतको ठहर", "description": ""},
    ])
    assert ns.timeline_events(case)["verdict"] == ("2026", "2083")


def test_an_undated_entry_still_marks_the_stage_as_reached():
    """The date is needed for the year term and nothing else. Requiring one
    dropped the stage ORDERING too, so an undated `ठहर` entry left the verdict
    queries at the tail, where truncation removes them."""
    case = _timeline_case([{"title": "विशेष अदालतको ठहर फैसला",
                            "description": ""}])
    assert ns.timeline_events(case) == {"verdict": ("", "")}
    assert "ठहर" in ns._event_queries("Bikal Poudel", case)[0]


def test_the_models_english_queries_are_stage_ordered_too():
    """`generate_english_queries` asks for one query per event type in
    lifecycle order and only `QUERY_RESERVED_ENGLISH_SLOTS` are sent, so
    without a re-sort the verdict and appeal queries are always the ones cut --
    on exactly the cases whose timeline says the verdict is what to look for."""
    case = _timeline_case([{"date": "2026-02-05", "date_bs": "2082-10-22",
                            "title": "विशेष अदालतको ठहर", "description": ""}])
    llm = ["Bikal Poudel CIAA investigation Nepal",
           "Bikal Poudel charge sheet filed special court",
           "Bikal Poudel hearing custody Nepal",
           "Bikal Poudel verdict convicted special court",
           "Bikal Poudel supreme court appeal"]
    ordered = ns._order_by_stage(llm, case, ns.EVENT_WORDS_EN)
    assert "verdict" in ordered[0], ordered
    assert set(ordered) == set(llm), "re-sorting must not drop a query"


def test_a_query_matching_no_stage_keeps_its_place_at_the_back():
    """The English map is a keyword heuristic, not an authority. A query it
    does not recognise is still a query."""
    case = _timeline_case([])
    llm = ["Melamchi drinking water project contractor scandal",
           "Bikal Poudel CIAA investigation Nepal"]
    ordered = ns._order_by_stage(llm, case, ns.EVENT_WORDS_EN)
    assert ordered == ["Bikal Poudel CIAA investigation Nepal",
                       "Melamchi drinking water project contractor scandal"]


def test_the_year_is_devanagari_and_bs_never_ad():
    """Measured over 135 published-case bodies: the BS year appears as २०८२ in
    33% and 2082 in 9%. The AD year matched 100% -- page furniture, so it
    discriminates nothing while still costing a slot."""
    case = _timeline_case([{"date": "2026-02-05", "date_bs": "2082-10-22",
                            "title": "विशेष अदालतको ठहर", "description": ""}])
    queries = ns.build_queries(case)
    dated = [q for q in queries if "२०८२" in q]
    assert dated, queries
    assert not any("2082" in q or "2026" in q for q in queries), (
        "Latin digits and the AD year must not reach a query")


def test_the_year_never_replaces_the_bare_query():
    """The BS year is in only a third of articles, so a year-only query would
    lose the other two thirds."""
    case = _timeline_case([{"date": "2026-02-05", "date_bs": "2082-10-22",
                            "title": "विशेष अदालतको ठहर", "description": ""}])
    queries = ns.build_queries(case)
    bare = [q for q in queries if "ठहर" in q and "२०८२" not in q]
    assert bare, "the unqualified verdict query must still be searched"


def test_a_case_with_no_timeline_still_asks_about_every_stage():
    """The DRAFT backlog has no timeline at all, so an empty one must keep the
    full lifecycle spread rather than collapsing onto a default stage."""
    case = _timeline_case([])
    raw = ns._event_queries("Bikal Poudel", case)
    for event_type in ns.ALL_EVENT_TYPES:
        words = ns.TIMELINE_EVENT_WORDS[event_type]
        assert any(any(w in q for w in words) for q in raw), (
            f"{event_type} is not asked about at all")
    assert not any("२०" in q for q in ns.build_queries(case)), (
        "no timeline means no year is known -- none may be invented")


def test_the_reserved_event_slots_cover_four_different_stages():
    """The load-bearing property of the round-robin, and the regression that
    review caught: emitting all of a stage's templates before moving on spent
    5 of the 12 sent slots on `verdict` and dropped `filing` and `hearing`
    entirely -- while `मुद्दा दायर` is the most common phrase in the corpus
    (57% of bodies). One query per stage before any stage gets two.
    """
    case = _timeline_case([{"date": "2026-02-05", "date_bs": "2082-10-22",
                            "title": "विशेष अदालतको ठहर", "description": ""}])
    lead = ns._event_queries("Bikal Poudel", case)[:ns.QUERY_RESERVED_EVENT_SLOTS]
    hit = [event for event, words in ns.TIMELINE_EVENT_WORDS.items()
           if any(any(w in q for w in words) for q in lead)]
    assert len(hit) == ns.QUERY_RESERVED_EVENT_SLOTS, (
        f"the {ns.QUERY_RESERVED_EVENT_SLOTS} reserved slots cover only "
        f"{sorted(hit)}")
    assert "ठहर" in lead[0], "the stage the timeline points at must lead"


def test_the_measured_templates_use_the_words_that_actually_occur():
    """Guards the corpus measurement. Share of the 135 article bodies:
    अख्तियार 64%, भ्रष्टाचार 63%, विशेष अदालत 57%, बिगो 41%, ठहर 20% --
    against फैसला 10%, सुनुवाइ 5%, पुनरावेदन 3%."""
    joined = " ".join(t for ts in ns.EVENT_QUERY_TEMPLATES_MEASURED.values()
                      for t in ts)
    for common in ("अख्तियार", "भ्रष्टाचार", "विशेष अदालत", "बिगो", "ठहर"):
        assert common in joined, f"{common} is frequent and must be searched"
    assert "सुनुवाइ" not in joined, (
        "सुनुवाइ appears in 0 of 138 headlines and 5% of bodies; the donor "
        "template keeps it, the measured set must not add it back")


def test_the_donor_templates_are_still_candidates_behind_the_measured_ones():
    """Both sets are offered for every stage, measured first.

    NOT "reach can only grow" -- that claim was wrong and this test used to
    assert it. `build_queries` truncates to `QUERY_LIMIT`, so adding templates
    necessarily pushes others off the wire; on a rich title the donor filing
    query is one of them. What is guaranteed is that no donor template is
    DELETED: it stays a candidate, ranked behind the corpus-measured one, and
    the retry ladder in `fallback_queries` can still reach it.
    """
    case = _timeline_case([])
    offered = " ".join(ns._event_queries("Bikal Poudel", case))
    assert "अख्तियार मुद्दा दायर" in offered            # donor filing
    assert "विरुद्ध भ्रष्टाचार मुद्दा दायर" in offered   # measured filing
    for event_type, templates in ns.EVENT_QUERY_TEMPLATES.items():
        for template in templates:
            assert template.format(name="Bikal Poudel") in offered, (
                f"donor template for {event_type} was deleted, not deprioritised")


# ---------------------------------------------------------------------------
# Devanagari name recovery. NES stores Latin names; Nepali newsrooms write
# Devanagari. Measured on the labelled set: 3 of the 4 articles we could not
# find ARE in Google's index and their headlines carry the exact keywords we
# search for -- only the name was in the wrong script.
# ---------------------------------------------------------------------------

def test_the_skeleton_guard_accepts_real_spellings_and_rejects_wrong_names():
    for latin, dev in [("Bikal Poudel", "विकल पौडेल"),
                       ("Gajendra Maharjan", "गजेन्द्र महर्जन"),
                       ("Surendra Bahadur Bisht", "सुरेन्द्र बहादुर विष्ट"),
                       ("Dinesh Prasad Yadav", "दिनेश प्रसाद यादव")]:
        assert ns.devanagari_names_match(latin, dev), (latin, dev)
    assert not ns.devanagari_names_match("Bikal Poudel", "रामप्रसाद शर्मा")
    assert not ns.devanagari_names_match("Bikal Poudel", "")
    assert not ns.devanagari_names_match("", "विकल पौडेल")


def test_a_wrong_name_from_the_model_is_dropped_not_searched():
    """A hallucinated name would send the whole search after another person and
    report the result as 'no coverage exists' -- the silent zero this module
    refuses everywhere else."""
    case = {"title": "t", "description": "", "key_allegations": [],
            "entities": [{"display_name": "Bikal Poudel", "type": "accused"}]}

    def fake_invoke(**kwargs):
        return {"names": {"Bikal Poudel": "रामप्रसाद शर्मा"}}

    got = ns.generate_devanagari_names(case, fake_invoke, FakeUsage())
    assert got == {}, "a name that fails the skeleton check must not be used"


def test_the_guard_rejects_a_swapped_syllable_name():
    """`romanize_devanagari` writes च as "ch" and ख as "kh", so folding `c`->`k`
    made चन्द्र and खन्द्र identical and a swapped-syllable name passed the only
    guard against a hallucinated one."""
    assert not ns.devanagari_names_match("Chandra Khadka", "खन्द्र छड्का")
    assert ns.devanagari_names_match("Chandra Khadka", "चन्द्र खड्का")


def test_the_guard_rejects_a_second_name_appended():
    """Containment on its own accepts any superset, so a model that answered
    with two people's names passed. The slack is bounded to the one consonant a
    conjunct romanisation actually explains."""
    assert not ns.devanagari_names_match("Bikal Poudel", "विकल पौडेल पण्डित शर्मा")
    assert ns.devanagari_names_match("Bikal Poudel", "विकल पौडेल")


def test_an_honorific_does_not_break_the_guard():
    """NES carries `Dr.` in `display_name`; a news report does not print it and
    the prompt tells the model to drop it. The comparison has to drop it too,
    or the model's correct answer reads two consonants short and is rejected."""
    assert ns.devanagari_names_match("Dr. Bikal Poudel", "विकल पौडेल")
    assert ns.devanagari_names_match("Prof. Gajendra Maharjan", "गजेन्द्र महर्जन")
    assert not ns.devanagari_names_match("Dr. Bikal Poudel", "रामप्रसाद शर्मा")


def test_a_good_name_from_the_model_is_kept():
    case = {"title": "t", "description": "", "key_allegations": [],
            "entities": [{"display_name": "Bikal Poudel", "type": "accused"}]}

    def fake_invoke(**kwargs):
        return {"names": {"Bikal Poudel": "विकल पौडेल"}}

    assert ns.generate_devanagari_names(case, fake_invoke, FakeUsage()) == {
        "Bikal Poudel": "विकल पौडेल"}


def test_a_dead_model_costs_query_quality_not_correctness():
    case = {"title": "t", "description": "", "key_allegations": [],
            "entities": [{"display_name": "Bikal Poudel", "type": "accused"}]}

    def dead(**kwargs):
        raise RuntimeError("529 Overloaded")

    assert ns.generate_devanagari_names(case, dead, FakeUsage()) == {}
    # The run must still produce queries, in Latin.
    queries = ns.build_queries(case, devanagari_names={})
    assert queries and any("Bikal Poudel" in q for q in queries)


def test_devanagari_templates_get_the_devanagari_name_english_ones_do_not():
    """Mixing scripts inside one query is the bug: 'Bikal Poudel विशेष अदालत
    ठहर' against an article that says 'विकल पौडेल'."""
    case = {"title": "t", "key_allegations": [], "court_cases": [],
            "entities": [{"display_name": "Bikal Poudel", "type": "accused"}],
            "timeline": [{"date": "2026-02-05", "date_bs": "2082-10-22",
                          "title": "विशेष अदालतको ठहर", "description": ""}]}
    queries = ns.build_queries(case,
                               devanagari_names={"Bikal Poudel": "विकल पौडेल"})
    devanagari = [q for q in queries if "ठहर" in q or "सफाइ" in q]
    assert devanagari
    for q in devanagari:
        assert "विकल पौडेल" in q, q
        assert "Bikal Poudel" not in q, f"mixed-script query survived: {q}"
    english = [q for q in queries if ns.is_english_query(q)]
    assert english and all("Bikal Poudel" in q or "Nepal" in q for q in english)


def test_no_devanagari_name_falls_back_to_the_latin_one():
    """The recovery is an improvement, not a dependency. When the model returns
    nothing, the Devanagari templates keep the Latin name -- the donor's
    behaviour -- rather than emitting a nameless or empty query."""
    case = {"title": "t", "key_allegations": [], "court_cases": [],
            "entities": [{"display_name": "Bikal Poudel", "type": "accused"}],
            "timeline": []}
    assert ns.build_queries(case) == ns.build_queries(case, devanagari_names={})
    # Only the name-bearing templates -- a title-keyword query carries no name
    # by design.
    devanagari = [q for q in ns._event_queries("Bikal Poudel", case, "")
                  if ns.DEVANAGARI_RE.search(q)]
    assert devanagari
    for q in devanagari:
        assert "Bikal Poudel" in q, f"a Devanagari template lost its name: {q}"


def test_the_devanagari_name_is_keyed_by_the_nes_name_not_the_models_key():
    """The prompt tells the model to drop `Dr.`/`Mr.`, so for a NES
    `display_name` of "Dr. Bikal Poudel" it correctly answers under the key
    "Bikal Poudel". `build_queries` looks up the NES name, so keying the table
    on the model's echo made the lookup miss -- silently, because the table was
    non-empty and nothing warned -- and re-sent the mixed-script query this
    whole feature exists to prevent."""
    case = {"title": "t", "key_allegations": [], "court_cases": [],
            "description": "विकल पौडेल विरुद्ध भ्रष्टाचार", "timeline": [],
            "entities": [{"display_name": "Dr. Bikal Poudel", "type": "accused"}]}
    def fake_invoke(**kwargs):
        return {"names": {"Bikal Poudel": "विकल पौडेल"}}

    table = ns.generate_devanagari_names(case, fake_invoke, FakeUsage())
    assert table == {"Dr. Bikal Poudel": "विकल पौडेल"}
    for q in ns.build_queries(case, devanagari_names=table):
        assert not (ns.DEVANAGARI_RE.search(q) and "Bikal" in q), (
            f"mixed-script query survived: {q}")


def test_a_devanagari_nes_name_needs_no_model_call():
    """`name_skeleton` keeps only `[a-z]`, so a Devanagari name on the Latin
    side reduces to "" and every answer fails the guard. Asking anyway burnt a
    cheap-tier call per case and logged a warning blaming the model for a
    question it should never have been asked."""
    calls = []

    def spy(*args, **kwargs):
        calls.append(1)
        return {"names": {}}

    case = {"title": "t", "key_allegations": [], "court_cases": [], "timeline": [],
            "entities": [{"display_name": "जीवन बहादुर शाही", "type": "accused"}]}
    assert ns.generate_devanagari_names(case, spy, None) == {}
    assert not calls, "no transcription is needed, so no call may be made"


def test_the_general_block_is_in_devanagari_too():
    """`_event_queries` was not the only place interpolating a name into a
    Devanagari template -- `general` holds three more, and they get four
    reserved slots at the front of the sent list."""
    case = {"title": "काठमाडौं महानगरपालिका भ्रष्टाचार मुद्दा",
            "key_allegations": ["घुस लिएको"], "court_cases": [], "timeline": [],
            "entities": [{"display_name": "Bikal Poudel", "type": "accused"}]}
    queries = ns.build_queries(case,
                               devanagari_names={"Bikal Poudel": "विकल पौडेल"})
    for q in queries:
        assert not (ns.DEVANAGARI_RE.search(q) and "Bikal" in q), (
            f"mixed-script query survived: {q}")


def test_a_devanagari_nes_name_keeps_the_lossy_table_out_of_the_front_slots():
    """`romanize_devanagari` is a lossy hand table: "जीवन बहादुर शाही" comes
    back "jiwn bhadur shahi" -- the exact defect the model's own prompt calls
    out. Promoting it would spend 2 of the 4 English slots on a misspelling and
    displace the model's correct romanisation, so on a Devanagari NES name it
    goes back to being the donor's last resort."""
    case = {"title": "t", "key_allegations": [], "court_cases": [], "timeline": [],
            "entities": [{"display_name": "जीवन बहादुर शाही", "type": "accused"}]}
    good = ["Jiban Bahadur Shahi CIAA corruption Nepal",
            "Jiban Bahadur Shahi special court verdict"]
    queries = ns.build_queries(case, llm_english_queries=good)
    english = [q for q in queries if ns.is_english_query(q)]
    lossy = [i for i, q in enumerate(english) if "jiwn" in q]
    model = [i for i, q in enumerate(english) if "Jiban Bahadur Shahi" in q]
    assert model and lossy, english
    assert max(model) < min(lossy), (
        f"the lossy hand table outranked the model's romanisation: {english}")


def test_the_enricher_asks_for_the_devanagari_name():
    """Driven through `collect_for_case`, so it pins that the call HAPPENS on
    the production path -- a source grep would keep passing if the result were
    computed and then never passed to `build_queries`."""
    invoke_json = stub_invoke_json()
    en.collect_for_case(case_payload(), FakeWeb(search_results=[]), invoke_json,
                        FakeUsage(), max_articles=3)
    assert ns.DEVANAGARI_NAME_SYSTEM_PROMPT[:40] in invoke_json.seen["systems"], (
        invoke_json.seen["systems"])


def test_the_short_name_queries_survive_the_models_queries():
    """Measured on the labelled set with Serper, 2026-08-06: the model's queries
    ALONE scored 5/10 against the short queries' 6/10, because it over-specifies
    and the plain name query that had been finding the article stopped being
    sent. The model still earns its place -- it found the one article no name
    query reaches, via the project name -- so both are searched, short first."""
    case = {"title": "t", "key_allegations": [], "court_cases": [], "timeline": [],
            "entities": [{"display_name": "Jiban Bahadur Shahi", "type": "accused"}]}
    llm = ["Nepal CIAA investigation Nepal Airlines A330-200 widebody aircraft "
           "purchase Jiban Bahadur Shahi corruption"]
    queries = ns.build_queries(case, llm_english_queries=llm)
    assert any(q.startswith("Jiban Bahadur Shahi CIAA Nepal corruption")
               for q in queries), queries
    assert any("A330-200" in q for q in queries), (
        "the model's query must still be searched, not dropped")
    short_at = next(i for i, q in enumerate(queries) if "CIAA Nepal corruption" in q)
    long_at = next(i for i, q in enumerate(queries) if "A330-200" in q)
    assert short_at < long_at, "the short query must be spent first"
