"""Tests for the DB-free news-article enricher (casework/enrich_news_articles.py).

These cover the pure search/parse/query logic and the HTTP save path against a
fake CaseworkApi. No database and no network are touched.
"""

from datetime import date

from casework import enrich_news_articles as nea

# ── accused-name extraction ────────────────────────────────────────────────────


def test_get_accused_names_from_entities():
    case = {
        "entities": [
            {"display_name": "राम बहादुर", "type": "accused"},
            {"display_name": "श्याम कम्पनी", "type": "related"},
            {"display_name": "सीता देवी", "type": "accused"},
        ]
    }
    assert nea._get_accused_names(case) == ["राम बहादुर", "सीता देवी"]


def test_get_accused_names_falls_back_to_nes_id():
    case = {"entities": [{"nes_id": "entity:person/x", "type": "accused"}]}
    assert nea._get_accused_names(case) == ["entity:person/x"]


def test_get_accused_names_title_fallback():
    # The title-regex fallback captures the text after "विरुद्ध" up to end-of-string;
    # it does not strip a trailing "मुद्दा" (ported behavior).
    case = {"entities": [], "title": "नेपाल सरकार विरुद्ध राम बहादुर"}
    names = nea._get_accused_names(case)
    assert names == ["राम बहादुर"]


def test_get_accused_names_empty():
    assert nea._get_accused_names({}) == []


# ── case number / court refs ────────────────────────────────────────────────────


def test_resolve_case_number():
    assert (
        nea._resolve_case_number({"court_cases": ["special:081-CR-0121"]})
        == "081-CR-0121"
    )


def test_resolve_case_number_none():
    assert nea._resolve_case_number({"court_cases": []}) is None


def test_court_number_normalizes():
    assert nea._court_number("special:081-CR-0121") == "081-CR-0121"
    assert nea._court_number("081-cr-0121") == "081-CR-0121"
    assert nea._court_number(None) == ""


# ── query generation ────────────────────────────────────────────────────────────


def test_is_english_query():
    assert nea._is_english_query("Bahadur Nepal corruption")
    assert not nea._is_english_query("राम बहादुर भ्रष्टाचार")
    assert not nea._is_english_query("12345")


def test_with_nepal_keyword_adds_once():
    assert nea._with_nepal_keyword("Bahadur corruption").endswith(" Nepal")
    assert nea._with_nepal_keyword("Bahadur Nepal") == "Bahadur Nepal"
    assert "नेपाल" in nea._with_nepal_keyword("राम भ्रष्टाचार")


def test_romanize_devanagari():
    out = nea._romanize_devanagari("राम")
    assert out and all(c.isascii() for c in out)


def test_generate_query_variations_basic():
    case = {
        "title": "काठमाडौं महानगरपालिका कार्यालय भ्रष्टाचार मुद्दा",
        "entities": [{"display_name": "राम बहादुर", "type": "accused"}],
        "court_cases": ["special:081-CR-0121"],
        "key_allegations": ["घुस लिएको आरोप"],
    }
    queries = nea._generate_query_variations(case)
    assert 0 < len(queries) <= nea._QUERY_LIMIT
    assert len(queries) == len(set(queries))  # deduped
    assert all(nea._query_has_nepal_keyword(q) for q in queries)


def test_generate_query_variations_uses_llm_english():
    case = {"title": "x", "entities": [{"display_name": "राम", "type": "accused"}]}
    queries = nea._generate_query_variations(
        case, llm_english_queries=["Ram Bahadur Nepal corruption"]
    )
    assert any("Ram Bahadur" in q for q in queries)


# ── HTML parsing ────────────────────────────────────────────────────────────────


def test_extract_text_from_html_skips_script():
    html = "<html><body><p>Hello world</p><script>var x=1;</script></body></html>"
    text = nea._extract_text_from_html(html)
    assert "Hello world" in text
    assert "var x" not in text


def test_extract_title_from_html():
    assert nea._extract_title_from_html("<title>  My  Article </title>") == "My Article"
    assert nea._extract_title_from_html("<html></html>") == ""


def test_extract_publication_date_meta():
    html = '<meta property="article:published_time" content="2024-06-15T10:00:00">'
    assert nea._extract_publication_date(html) == date(2024, 6, 15)


def test_extract_publication_date_none():
    assert nea._extract_publication_date("<html></html>") is None


def test_parse_date_string():
    assert nea._parse_date_string("2024-06-15") == date(2024, 6, 15)
    assert nea._parse_date_string("not-a-date") is None


def test_fix_mojibake_roundtrip():
    original = "अख्तियार"
    mojibake = original.encode("utf-8").decode("latin-1")
    assert nea._fix_mojibake(mojibake) == original


# ── url classification ──────────────────────────────────────────────────────────


def test_is_official_press_release():
    assert nea._is_official_press_release("https://ciaa.gov.np/pressrelease/123")
    assert not nea._is_official_press_release("https://example.com/news/123")


def test_is_url_blocklisted():
    assert nea._is_url_blocklisted("https://site.com/tag/corruption")
    assert nea._is_url_blocklisted("https://en.wikipedia.org/wiki/X")
    assert nea._is_url_blocklisted("https://example.com/news/123") is None


def test_extract_ddg_redirect():
    redirect = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x"
    assert nea._extract_ddg_redirect(redirect) == "https://example.com/a"
    assert nea._extract_ddg_redirect("https://example.com/a") == "https://example.com/a"


def test_guess_outlet():
    assert nea._guess_outlet("https://www.kantipur.com/news/1") == "Kantipur"
    assert nea._guess_outlet("https://onlinekhabar.com/x") == "Onlinekhabar"


# ── event-type inference ────────────────────────────────────────────────────────


def test_infer_event_type_uses_fallback_when_valid():
    assert nea._infer_event_type_from_reason("", "", "verdict") == "verdict"


def test_infer_event_type_from_reason_keywords():
    assert (
        nea._infer_event_type_from_reason("the court issued a verdict", "", "")
        == "verdict"
    )
    assert (
        nea._infer_event_type_from_reason("CIAA filed a charge sheet", "", "")
        == "filing"
    )


def test_infer_event_type_from_title_devanagari():
    assert (
        nea._infer_event_type_from_reason(
            "fallback: extracted", "सर्वोच्च अदालतमा पुनरावेदन", ""
        )
        == "appeal"
    )


def test_infer_event_type_empty():
    assert nea._infer_event_type_from_reason("unrelated text", "headline", "") == ""


# ── existing-evidence bookkeeping ────────────────────────────────────────────────


def _news_case(*event_types, extra_urls=None):
    evidence = []
    for i, et in enumerate(event_types):
        evidence.append(
            {
                "source_id": f"source:{i}",
                "description": "d",
                "event_type": et,
                "source": {
                    "source_type": "NEWS",
                    "urls": [{"link": f"https://news.example/{i}", "role": "RAW"}],
                },
            }
        )
    # a non-news evidence entry that should be ignored
    evidence.append(
        {
            "source_id": "source:pr",
            "description": "pr",
            "source": {
                "source_type": "CIAA_PRESS_RELEASE",
                "urls": [{"link": "https://ciaa.gov.np/x", "role": "RAW"}],
            },
        }
    )
    return {"evidence": evidence}


def test_count_news_evidence_ignores_non_news():
    case = _news_case("filing", "verdict")
    assert nea._count_news_evidence(case) == 2


def test_case_linked_news_urls():
    case = _news_case("filing", "verdict")
    assert nea._case_linked_news_urls(case) == {
        "https://news.example/0",
        "https://news.example/1",
    }
    assert "https://ciaa.gov.np/x" not in nea._case_linked_news_urls(case)


def test_existing_event_type_counts():
    case = _news_case("filing", "filing", "verdict")
    assert nea._existing_event_type_counts(case) == {"filing": 2, "verdict": 1}


# ── descriptions / publication date ──────────────────────────────────────────────


def test_publication_date_prefers_article_date():
    article = {"publication_date": date(2024, 1, 2)}
    assert nea._publication_date(article, {}) == "2024-01-02"


def test_publication_date_falls_back_to_case_start():
    assert nea._publication_date({}, {"case_start_date": "2023-05-06"}) == "2023-05-06"


def test_publication_date_defaults_today():
    assert nea._publication_date({}, {}) == date.today().isoformat()


def test_build_source_description_uses_summary():
    assert nea._build_source_description({"summary": "सारांश"}) == "सारांश"


def test_build_source_description_fallback():
    desc = nea._build_source_description(
        {"url": "https://kantipur.com/x", "publication_date": date(2024, 1, 2)}
    )
    assert "Kantipur" in desc and "2024-01-02" in desc


# ── fake API + save path ──────────────────────────────────────────────────────


class FakeApi:
    def __init__(self):
        self.created_sources = []
        self.added_evidence = []
        self.attached_markdown = []
        self._counter = 0

    def create_source(
        self, title, description, source_type, url, publication_date=None
    ):
        self._counter += 1
        sid = f"source:test:{self._counter}"
        self.created_sources.append(
            {
                "title": title,
                "description": description,
                "source_type": source_type,
                "url": url,
                "publication_date": publication_date,
            }
        )
        return {"source_id": sid}

    def add_evidence(self, slug, source_id, description, event_type=None):
        self.added_evidence.append(
            {
                "slug": slug,
                "source_id": source_id,
                "description": description,
                "event_type": event_type,
            }
        )

    def attach_markdown(self, source_id, markdown, overwrite=False):
        self.attached_markdown.append(
            {"source_id": source_id, "markdown": markdown, "overwrite": overwrite}
        )
        return {"created": True}


def _make_enricher(api, max_articles=5, transcribe=False):
    def _never_called(**kwargs):
        raise AssertionError("invoke_json should not be called in this test")

    return nea.NewsEnricher(
        api=api,
        invoke_json=_never_called,
        usage=None,
        max_articles_per_case=max_articles,
        transcribe=transcribe,
    )


def test_save_articles_creates_news_source_and_evidence():
    api = FakeApi()
    enricher = _make_enricher(api)
    case = {"slug": "case-1", "case_start_date": "2023-01-01"}
    accepted = [
        {
            "title": "T1",
            "url": "https://news.example/a",
            "event_type": "filing",
            "summary": "स",
            "publication_date": date(2024, 2, 3),
        },
    ]
    n = enricher._save_articles(case, accepted, dry_run=False, stats=nea._make_stats())
    assert n == 1
    src = api.created_sources[0]
    assert src["source_type"] == "NEWS"
    assert src["url"] == [{"link": "https://news.example/a", "role": "RAW"}]
    assert src["publication_date"] == "2024-02-03"
    ev = api.added_evidence[0]
    assert ev["slug"] == "case-1"
    assert ev["source_id"] == "source:test:1"
    assert ev["event_type"] == "filing"


def test_save_articles_orders_by_lifecycle():
    api = FakeApi()
    enricher = _make_enricher(api)
    accepted = [
        {"title": "V", "url": "https://x/v", "event_type": "verdict", "summary": "s"},
        {
            "title": "I",
            "url": "https://x/i",
            "event_type": "investigation",
            "summary": "s",
        },
    ]
    enricher._save_articles(
        {"slug": "c"}, accepted, dry_run=False, stats=nea._make_stats()
    )
    saved_events = [e["event_type"] for e in api.added_evidence]
    assert saved_events == ["investigation", "verdict"]


def test_save_articles_dry_run_makes_no_calls():
    api = FakeApi()
    enricher = _make_enricher(api)
    accepted = [
        {"title": "T", "url": "https://x/a", "event_type": "filing", "summary": "s"}
    ]
    n = enricher._save_articles(
        {"slug": "c"}, accepted, dry_run=True, stats=nea._make_stats()
    )
    assert n == 1
    assert api.created_sources == []
    assert api.added_evidence == []


# ── transcription ─────────────────────────────────────────────────────────────


def test_save_articles_transcribes_via_converter(monkeypatch):
    api = FakeApi()
    enricher = _make_enricher(api, transcribe=True)

    captured = {}

    def fake_convert(synthetic_case, *, overwrite=False):
        # Reuses the reprocess pipeline shape: returns (converted, candidates).
        captured["case"] = synthetic_case
        captured["overwrite"] = overwrite
        sids = [e["source_id"] for e in synthetic_case["evidence"]]
        return [], [{"source_id": sid, "markdown": f"# md {sid}"} for sid in sids]

    monkeypatch.setattr(
        "sourcing.converter.convert_case_to_attach_candidates", fake_convert
    )

    accepted = [
        {
            "title": "T",
            "url": "https://news.example/a",
            "event_type": "filing",
            "summary": "s",
        }
    ]
    stats = nea._make_stats()
    enricher._save_articles({"slug": "case-1"}, accepted, dry_run=False, stats=stats)

    # The synthetic case fed to the converter carries the new NEWS source as RAW.
    src = captured["case"]["evidence"][0]["source"]
    assert src["source_type"] == "NEWS"
    assert src["urls"] == [{"link": "https://news.example/a", "role": "RAW"}]
    # Markdown attached to the created source_id, transcribed counter bumped.
    assert api.attached_markdown[0]["source_id"] == "source:test:1"
    assert api.attached_markdown[0]["markdown"] == "# md source:test:1"
    assert stats["transcribed"] == 1


def test_save_articles_no_transcribe_when_disabled(monkeypatch):
    api = FakeApi()
    enricher = _make_enricher(api, transcribe=False)

    def boom(*args, **kwargs):
        raise AssertionError("converter must not run when --no-transcribe")

    monkeypatch.setattr("sourcing.converter.convert_case_to_attach_candidates", boom)

    accepted = [
        {"title": "T", "url": "https://x/a", "event_type": "filing", "summary": "s"}
    ]
    stats = nea._make_stats()
    enricher._save_articles({"slug": "c"}, accepted, dry_run=False, stats=stats)
    assert api.attached_markdown == []
    assert stats["transcribed"] == 0


def test_enrich_case_skips_saturated():
    api = FakeApi()
    enricher = _make_enricher(api, max_articles=2)
    case = _news_case("filing", "verdict")
    case["case_id"] = "case-x"
    result = enricher.enrich_case(case, dry_run=True, force=False, case_num=1, total=1)
    assert result["status"] == "skipped"
    assert result["reason"] == "already_saturated"
    assert api.created_sources == []


# ── candidate filtering ──────────────────────────────────────────────────────


def test_filter_new_candidates_excludes_linked():
    api = FakeApi()
    enricher = _make_enricher(api)
    candidates = [
        {"url": "https://x/a"},
        {"url": "https://x/b"},
        {"url": "https://x/linked"},
    ]
    new, linked = enricher._filter_new_candidates(
        candidates, {"https://x/linked"}, force=False
    )
    assert {c["url"] for c in new} == {"https://x/a", "https://x/b"}
    assert linked == 1


def test_filter_new_candidates_force_keeps_all():
    api = FakeApi()
    enricher = _make_enricher(api)
    candidates = [{"url": "https://x/a"}, {"url": "https://x/linked"}]
    new, linked = enricher._filter_new_candidates(
        candidates, {"https://x/linked"}, force=True
    )
    assert len(new) == 2
    assert linked == 1


# ── batched two-tier verification ─────────────────────────────────────────────


def _fetch_result(url, title="Ram Bahadur corruption"):
    # A body that clears _prefilter: >500 chars, contains the title keywords
    # beyond the first 200 chars, and a corruption keyword.
    body = "Ram Bahadur corruption case investigation details. " * 30
    return {
        "candidate": {"url": url},
        "article_text": body,
        "article_title": title,
        "article_date": None,
    }


def _enricher_with_llm(fake_invoke_json):
    return nea.NewsEnricher(
        api=FakeApi(), invoke_json=fake_invoke_json, usage=None, verbose=True
    )


def test_verify_batch_two_tier_accepts_premium_relevant():
    calls = []

    def fake(system, content, max_tokens, tier, usage):
        calls.append(tier)
        if tier == "cheap":
            return {
                "results": [
                    {"index": 0, "relevant": True},
                    {"index": 1, "relevant": True},
                ]
            }
        return {
            "results": [
                {
                    "index": 0,
                    "relevant": True,
                    "confidence": "high",
                    "reason": "matches",
                    "event_type": "filing",
                    "summary": "सारांश",
                },
                {"index": 1, "relevant": False, "reason": "different case"},
            ]
        }

    enricher = _enricher_with_llm(fake)
    stats = nea._make_stats()
    fetched = [_fetch_result("https://x/0"), _fetch_result("https://x/1")]
    out = enricher._verify_batch(fetched, {"title": "t"}, None, stats)

    assert calls == ["cheap", "premium"]  # two-tier: one call each
    assert len(out) == 1
    assert out[0]["url"] == "https://x/0"
    assert out[0]["event_type"] == "filing"
    assert out[0]["summary"] == "सारांश"
    assert stats["rejected"] == 1  # the premium-rejected candidate


def test_verify_batch_cheap_gate_rejects_all_skips_premium():
    calls = []

    def fake(system, content, max_tokens, tier, usage):
        calls.append(tier)
        return {
            "results": [
                {"index": 0, "relevant": False},
                {"index": 1, "relevant": False},
            ]
        }

    enricher = _enricher_with_llm(fake)
    stats = nea._make_stats()
    fetched = [_fetch_result("https://x/0"), _fetch_result("https://x/1")]
    out = enricher._verify_batch(fetched, {"title": "t"}, None, stats)

    assert calls == ["cheap"]  # premium never called when gate rejects everything
    assert out == []
    assert stats["rejected"] == 2


def test_verify_batch_gate_failure_escalates_to_premium():
    calls = []

    def fake(system, content, max_tokens, tier, usage):
        calls.append(tier)
        if tier == "cheap":
            return None  # gate call/parse failed
        return {
            "results": [
                {
                    "index": 0,
                    "relevant": True,
                    "confidence": "medium",
                    "reason": "ok",
                    "event_type": "verdict",
                    "summary": "स",
                }
            ]
        }

    enricher = _enricher_with_llm(fake)
    stats = nea._make_stats()
    out = enricher._verify_batch(
        [_fetch_result("https://x/0")], {"title": "t"}, None, stats
    )

    assert calls == ["cheap", "premium"]  # escalated despite gate failure
    assert len(out) == 1
    assert out[0]["event_type"] == "verdict"


def test_verify_batch_prefilters_thin_without_llm():
    def fake(*args, **kwargs):
        raise AssertionError("LLM must not be called when all candidates are thin")

    enricher = _enricher_with_llm(fake)
    stats = nea._make_stats()
    thin = {
        "candidate": {"url": "https://x/0"},
        "article_text": "short",
        "article_title": "Headline",
        "article_date": None,
    }
    out = enricher._verify_batch([thin], {"title": "t"}, None, stats)
    assert out == []
    assert stats["rejected"] == 1


def test_verify_batch_infers_missing_event_type():
    def fake(system, content, max_tokens, tier, usage):
        if tier == "cheap":
            return {"results": [{"index": 0, "relevant": True}]}
        return {
            "results": [
                {
                    "index": 0,
                    "relevant": True,
                    "confidence": "low",
                    "reason": "the court issued a verdict",
                    "event_type": "",
                    "summary": "स",
                }
            ]
        }

    enricher = _enricher_with_llm(fake)
    stats = nea._make_stats()
    out = enricher._verify_batch(
        [_fetch_result("https://x/0")], {"title": "t"}, None, stats
    )
    assert len(out) == 1
    assert out[0]["event_type"] == "verdict"  # inferred from reason text
