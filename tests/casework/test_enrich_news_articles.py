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

import json
import logging
import sys
import types
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

    Implements exactly the surface the module uses -- `get(url, kind, headers=,
    expect_html=)`, `calls` -- so it can be swapped in without touching the code
    under test. A URL with no canned page returns `(404, None)`, which is what a
    dead news host looks like; nothing here can reach the network.
    """

    def __init__(self, pages=None, search_results=None, snapshot=None):
        self.pages = dict(pages or {})
        self.search_results = list(search_results or [])
        self.snapshot = snapshot
        self.calls = {"search": 0, "fetch": 0, "archive": 0, "save": 0}
        self.requested = []

    def get(self, url, kind, headers=None, expect_html=False):
        self.calls[kind] = self.calls.get(kind, 0) + 1
        self.requested.append((kind, url))
        if kind == "search":
            return 200, ddg_html(self.search_results)
        if kind == "archive":
            if self.snapshot is None:
                return 429, None            # Wayback rate-limited, as seen in prod
            return 200, json.dumps(
                {"archived_snapshots": {"closest": {"available": True,
                                                    "url": self.snapshot}}})
        if kind == "save":
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

    def iter_cases(self, params=None, timeout=60, progress=None):
        return iter([{"slug": self.case["slug"], "state": self.case.get("state")}])

    def get_case_with_etag(self, slug, timeout=60):
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
    report = run_main(monkeypatch, api, web, stub_invoke_json(),
                      ["--review-file", str(review)])
    assert report.summary() == {"error": 1}
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


@pytest.fixture(autouse=True)
def _quiet(caplog):
    caplog.set_level(logging.CRITICAL, logger="casework.news_search")
