"""Tests for the DB-free standalone BIGO enricher (casework/enrich_missing_bigo.py).

Focus on `coerce_bigo_int`: CIAA writes paisa after a danda '।', slash '/', or
dot '.', and blindly stripping non-digits used to fold the paisa digits into the
rupee figure (a 10-100x inflation that reached production, e.g. 080-CR-0158).
No database and no network are touched.
"""
import json
import logging
import sys
import types
from pathlib import Path

import pytest

from casework import enrich_missing_bigo as emb
from casework.enrich_missing_bigo import (
    coerce_bigo_int,
    is_explicit_bigo_context,
    parse_bigo_response,
)

# --------------------------------------------------------------------------
# coerce_bigo_int -- donor pins (donor commit 0321a85, 080-CR-0158/0181)
# --------------------------------------------------------------------------


class TestCoerceBigoInt:
    def test_danda_paisa_dropped(self):
        # 080-CR-0158: २३,७५,४६,३२४।५७ must be 237546324, NOT 2375463245.
        assert coerce_bigo_int("२३,७५,४६,३२४।५७") == 237546324

    def test_danda_paisa_with_currency_prefix(self):
        # 080-CR-0181
        assert coerce_bigo_int("रु.२६,६३,३७,३९८।१२") == 266337398

    def test_pipe_paisa_dropped(self):
        # OCR frequently misreads the danda '।' as a vertical pipe '|'.
        assert coerce_bigo_int("२३,७५,४६,३२४|५७") == 237546324

    def test_slash_paisa_dropped(self):
        assert coerce_bigo_int("१,४६,८१,२२५/९०") == 14681225

    def test_trailing_slash_dash(self):
        assert coerce_bigo_int("40,85,74,740/-") == 408574740

    def test_ascii_decimal_paisa_dropped(self):
        assert coerce_bigo_int("237546324.57") == 237546324

    def test_clean_integer_string(self):
        assert coerce_bigo_int("237546324") == 237546324

    def test_plain_int_passthrough(self):
        assert coerce_bigo_int(237546324) == 237546324

    def test_float_truncates(self):
        assert coerce_bigo_int(237546324.57) == 237546324

    def test_zero_is_none(self):
        assert coerce_bigo_int(0) is None

    def test_none_is_none(self):
        assert coerce_bigo_int(None) is None

    def test_empty_string_is_none(self):
        assert coerce_bigo_int("रु.") is None


# --------------------------------------------------------------------------
# coerce_bigo_int -- brief pins, plus a deliberate enumeration of the paisa
# input space (whole rupees / paisa-bearing / the boundary / absent /
# malformed), per the task's caveat that mutating what you already wrote is
# not the same as constructing the case that breaks it.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("237546324।57", 237546324),   # danda paisa separator
    ("237546324|57", 237546324),   # OCR misreads danda as pipe
    ("237546324/57", 237546324),   # slash
    ("237546324.57", 237546324),   # dot
    ("रु. 237546324।57", 237546324),  # leading currency prefix
    ("237546324", 237546324),      # no paisa at all
])
def test_paisa_separator_does_not_inflate_bigo(raw, expected):
    # Stripping non-digits would fold paisa into rupees and inflate 10-100x.
    assert coerce_bigo_int(raw) == expected


def test_leading_currency_prefix_is_not_a_paisa_separator():
    # Anchoring at the first digit is what stops 'रु.' being read as a separator.
    assert coerce_bigo_int("रु.1000") == 1000


class TestCoerceBigoIntInputSpaceEnumeration:
    """Deliberate enumeration, not incidental mutation-driven coverage."""

    # -- whole rupees (no paisa marker present at all) --
    def test_whole_rupee_devanagari_digits(self):
        assert coerce_bigo_int("१०,४०,३९,४१") == 10403941

    def test_whole_rupee_ascii_digits(self):
        assert coerce_bigo_int("10403941") == 10403941

    # -- paisa-bearing amounts, one per separator the donor recognizes --
    def test_paisa_bearing_danda(self):
        assert coerce_bigo_int("१,००,००,०००।५०") == 10000000

    def test_paisa_bearing_pipe(self):
        assert coerce_bigo_int("१,००,००,०००|५०") == 10000000

    def test_paisa_bearing_slash(self):
        assert coerce_bigo_int("१,००,००,०००/५०") == 10000000

    def test_paisa_bearing_dot(self):
        assert coerce_bigo_int("१,००,००,०००.५०") == 10000000

    # -- the boundary: a single-digit rupee amount right before a separator,
    # and paisa digits that themselves look like a plausible rupee amount if
    # merged (guards the exact 10-100x failure mode, not just a big number).
    def test_boundary_single_digit_rupee_before_separator(self):
        assert coerce_bigo_int("५।९०") == 5

    def test_boundary_paisa_digits_would_look_valid_if_merged(self):
        # If paisa merged in, this would read as 23754632457 (100x) instead
        # of 237546324 -- the exact shape of the 080-CR-0158 regression.
        assert coerce_bigo_int("२३,७५,४६,३२४।५७") == 237546324
        assert coerce_bigo_int("२३,७५,४६,३२४।५७") != 2375463245
        assert coerce_bigo_int("२३,७५,४६,३२४।५७") != 23754632457

    # -- absent amounts --
    def test_absent_none(self):
        assert coerce_bigo_int(None) is None

    def test_absent_zero_int(self):
        assert coerce_bigo_int(0) is None

    def test_absent_zero_float(self):
        assert coerce_bigo_int(0.0) is None

    def test_absent_negative_int(self):
        assert coerce_bigo_int(-5) is None

    def test_negative_sign_in_a_string_is_not_special_cased(self):
        # The donor's string path anchors at the first DIGIT and never
        # inspects sign characters -- CIAA amounts are never negative, so
        # this is not a guard the donor implements. Pinning the actual
        # behavior (not an invented "should be None") per this task's
        # instruction not to assert brief-derived values the donor doesn't
        # implement.
        assert coerce_bigo_int("-5") == 5

    # -- malformed amounts --
    def test_malformed_no_digits_at_all(self):
        assert coerce_bigo_int("रु.") is None

    def test_malformed_prose_only(self):
        assert coerce_bigo_int("करोडौं") is None

    def test_malformed_non_string_non_numeric_type(self):
        assert coerce_bigo_int(["not", "a", "number"]) is None

    def test_malformed_dict_type(self):
        assert coerce_bigo_int({"bigo": 5}) is None

    # -- the STRING-path zero guard specifically. `test_zero_is_none` only
    # exercises the int-literal branch (`isinstance(value, int)`), and
    # `test_empty_string_is_none` short-circuits earlier at the
    # "not digits_only" check -- neither reaches the final
    # `bigo if bigo > 0 else None` on the parsed-from-string path. A mutant
    # that drops that final guard passes both of those tests untouched.
    def test_string_parsing_to_zero_is_none(self):
        assert coerce_bigo_int("0") is None

    def test_devanagari_string_parsing_to_zero_is_none(self):
        assert coerce_bigo_int("०") is None

    def test_string_parsing_to_zero_with_paisa_suffix_is_none(self):
        assert coerce_bigo_int("०।५०") is None


# --------------------------------------------------------------------------
# is_explicit_bigo_context
# --------------------------------------------------------------------------


def test_explicit_bigo_context_required():
    assert is_explicit_bigo_context("बिगो रु. १०,००० कायम भएको")
    assert is_explicit_bigo_context("मागदाबी रकम")
    assert is_explicit_bigo_context("नोक्सानी भएको")
    assert not is_explicit_bigo_context("कुल आय रु. ५०,००,०००")


def test_context_check_rejects_non_string_quote():
    assert not is_explicit_bigo_context(None)
    assert not is_explicit_bigo_context(12345)


def test_context_check_rejects_blank_quote():
    assert not is_explicit_bigo_context("   ")


def test_context_check_is_case_insensitive_on_english_keywords():
    assert is_explicit_bigo_context("Corruption Loss estimated at NPR 5,000,000")


# --------------------------------------------------------------------------
# parse_bigo_response
# --------------------------------------------------------------------------


def test_low_confidence_yields_none():
    body = '{"bigo": 500000, "confidence": "low", "evidence_quote": "बिगो रु ५ लाख"}'
    assert parse_bigo_response(body) is None


def test_non_bigo_quote_yields_none_even_at_high_confidence():
    body = '{"bigo": 5000000, "confidence": "high", "evidence_quote": "जम्मा आय रु ५० लाख"}'
    assert parse_bigo_response(body) is None


def test_valid_high_confidence_bigo_parses():
    body = '{"bigo": 10403941, "confidence": "high", "evidence_quote": "बिगो रु. १,०४,०३,९४१"}'
    assert parse_bigo_response(body) == 10403941


def test_null_bigo_with_high_confidence_and_bigo_quote_is_none():
    # coerce_bigo_int(None) is None even after passing the context gate.
    body = '{"bigo": null, "confidence": "high", "evidence_quote": "बिगो निर्धारण भएको छैन"}'
    assert parse_bigo_response(body) is None


def test_fenced_json_is_parsed():
    body = (
        "Here is the extraction:\n```json\n"
        '{"bigo": 237546324, "confidence": "high", "evidence_quote": "बिगो रु. २,३७,५४,६,३२४"}'
        "\n```\n"
    )
    assert parse_bigo_response(body) == 237546324


def test_balanced_object_scan_finds_bigo_amid_prose_and_nested_braces():
    # A brace-only regex would break on a nested/quoted brace in the
    # evidence_quote; balanced_object is string-aware and must still find it.
    body = (
        'Some preamble text with a stray { that is not JSON. '
        '{"bigo": 999000, "confidence": "high", '
        '"evidence_quote": "बिगो {कायम} रु. ९,९९,०००", "note": "trailing"}'
    )
    assert parse_bigo_response(body) == 999000


def test_missing_confidence_key_defaults_to_empty_string_not_low():
    # str(obj.get("confidence", "")).strip().lower() on a genuinely absent key
    # yields "" != "low", so this must NOT be treated as low-confidence --
    # only an explicit "low" string should gate the result.
    body = '{"bigo": 100000, "evidence_quote": "बिगो रु. १,००,०००"}'
    assert parse_bigo_response(body) == 100000


def test_unparseable_text_yields_none():
    assert parse_bigo_response("not json at all, just prose about a case") is None


def test_confidence_is_case_and_whitespace_insensitive():
    body = '{"bigo": 500000, "confidence": " LOW ", "evidence_quote": "बिगो रु ५ लाख"}'
    assert parse_bigo_response(body) is None


# --------------------------------------------------------------------------
# parse_bigo_response -- confidence/context gate coverage per parse branch
# (review finding: the gate is triplicated across the direct-JSON, fenced-JSON,
# and balanced-object-scan branches, but only the direct-JSON branch had test
# coverage. Mutation testing proved deleting the gate from the fenced branch
# alone left all other tests passing.)
# --------------------------------------------------------------------------


def test_fenced_json_low_confidence_yields_none():
    body = (
        "Here is the extraction:\n```json\n"
        '{"bigo": 500000, "confidence": "low", "evidence_quote": "बिगो रु ५ लाख"}'
        "\n```\n"
    )
    assert parse_bigo_response(body) is None


def test_fenced_json_non_bigo_quote_yields_none_even_at_high_confidence():
    body = (
        "Here is the extraction:\n```json\n"
        '{"bigo": 5000000, "confidence": "high", "evidence_quote": "जम्मा आय रु ५० लाख"}'
        "\n```\n"
    )
    assert parse_bigo_response(body) is None


def test_balanced_object_scan_low_confidence_yields_none():
    body = (
        'Some preamble text with a stray { that is not JSON. '
        '{"bigo": 500000, "confidence": "low", '
        '"evidence_quote": "बिगो रु ५ लाख", "note": "trailing"}'
    )
    assert parse_bigo_response(body) is None


def test_balanced_object_scan_non_bigo_quote_yields_none_even_at_high_confidence():
    body = (
        'Some preamble text with a stray { that is not JSON. '
        '{"bigo": 5000000, "confidence": "high", '
        '"evidence_quote": "जम्मा आय रु ५० लाख", "note": "trailing"}'
    )
    assert parse_bigo_response(body) is None


# --------------------------------------------------------------------------
# _source_metadata -- prompt source-context block (review finding: this must
# surface material.display_name, the schema's analog to the donor's
# source.title, since ~10% of press-release display_names state the बिगो
# amount directly, e.g. "... उपर बिगो रु.९०,३९,६२०।३९ कायम")
# --------------------------------------------------------------------------


def test_source_metadata_includes_material_display_name():
    case = {
        "title": "अख्तियारले थुनामा राखेको",
        "evidence": [
            {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/1",
             "material": {
                 "material_type": "press_release",
                 "display_name": (
                     "चापाकोट नगरकार्यपालिकाको कार्यालय ... सिनियर अहेव बिन्दु "
                     "कोईराला उपर बिगो रु.९०,३९,६२०।३९ कायम"
                 ),
                 "urls": [{"link": "https://x/1.md", "role": "MARKDOWN"}],
             }},
        ],
    }
    rendered = emb._source_metadata(case, ("press_release",))
    assert "बिगो रु.९०,३९,६२०।३९ कायम" in rendered


def test_source_metadata_material_without_display_name_renders_without_error():
    case = {
        "title": "अख्तियारले थुनामा राखेको",
        "evidence": [
            {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/1",
             "material": {
                 "material_type": "press_release",
                 "urls": [{"link": "https://x/1.md", "role": "MARKDOWN"}],
             }},
        ],
    }
    rendered = emb._source_metadata(case, ("press_release",))
    assert "display_name: " in rendered
    assert "material_type: press_release" in rendered
    assert "https://x/1.md" in rendered


# --------------------------------------------------------------------------
# main() -- integration over a stubbed API + LLM
# --------------------------------------------------------------------------

PRESS_CASE_UNCONVERTED = {
    "slug": "case-unconverted",
    "title": "अख्तियारले थुनामा राखेको",
    "state": "DRAFT",
    "bigo": None,
    "court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0001"],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/1",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/1.pdf", "role": "RAW"}]}},
    ],
}

PRESS_CASE_READY = {
    "slug": "case-ready",
    "title": "बिगो रु. १,०४,०३,९४१ कायम",
    "state": "DRAFT",
    "bigo": None,
    "court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0002"],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/2",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/2.md", "role": "MARKDOWN"}]}},
    ],
}

PRESS_CASE_ALREADY_POPULATED = {
    "slug": "case-populated",
    "title": "पहिल्यै बिगो तोकिएको",
    "state": "DRAFT",
    "bigo": 5000000,
    "court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0003"],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/3",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/3.md", "role": "MARKDOWN"}]}},
    ],
}

PRESS_CASE_LLM_DECLINES = {
    "slug": "case-declines",
    "title": "रंगेहात पक्राउ",
    "state": "DRAFT",
    "bigo": None,
    "court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0004"],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/4",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/4.md", "role": "MARKDOWN"}]}},
    ],
}


class _StubApi:
    def __init__(self, cases, markdown_by_link=None):
        # Shallow-copy each case dict: `patch_field` mutates in place to
        # emulate a real PATCH, and several tests reuse the same
        # module-level fixture dict across test functions. Without the
        # copy, a --apply test that patches PRESS_CASE_READY's `bigo`
        # leaks that mutation into every later test that reuses the same
        # object -- silently turning "ready" cases into "already
        # populated" ones and starving the LLM stub of calls.
        self._cases = {c["slug"]: dict(c) for c in cases}
        self._markdown_by_link = markdown_by_link or {}
        self.patched = []

    def iter_cases(self, params=None, timeout=60):
        yield from self._cases.values()

    def get_case(self, slug, timeout=60):
        return self._cases[slug]

    def patch_field(self, slug, field, value, timeout=60):
        self.patched.append((slug, field, value))
        self._cases[slug][field] = value
        return {}


@pytest.fixture
def patched_fetch_markdown(monkeypatch):
    import casework.common.materials as m

    def fake_fetch(link, timeout=60):
        return {
            "https://x/2.md": "बिगो रु. १,०४,०३,९४१ कायम भएको छ ।",
            "https://x/3.md": "बिगो रु. ५,००,००,००० कायम भएको छ ।",
            "https://x/4.md": "रंगेहात पक्राउ परेको घुस रकम रु. ५,००,०००",
        }.get(link, "")

    monkeypatch.setattr(m, "fetch_markdown", fake_fetch)


class _FakeUsage:
    def __init__(self):
        self.calls = 0

    def as_dict(self):
        return {"by_provider": []}


def _run_main(monkeypatch, cases, invoke_text_stub, argv):
    """Drive `main()` end to end with a stubbed API and a stubbed LLM call.

    `invoke_text` and `UsageAccumulator` are imported INSIDE `main()` (after
    bootstrap), so they're faked out via `sys.modules` rather than
    `monkeypatch.setattr(emb, ...)` -- there is no module-level `emb.invoke_text`
    name to patch.
    """
    api = _StubApi(cases)
    monkeypatch.setattr(emb, "build_api", lambda args: api)
    monkeypatch.setattr(emb, "bootstrap", lambda *a, **k: None)

    fake_llm_invoke = types.ModuleType("llm.invoke")
    fake_llm_invoke.invoke_text = invoke_text_stub

    fake_llm_usage = types.ModuleType("llm.usage")
    fake_llm_usage.UsageAccumulator = _FakeUsage
    fake_llm_usage.render_usage_table = lambda by_provider, title=None: ""

    monkeypatch.setitem(sys.modules, "llm.invoke", fake_llm_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_llm_usage)

    report = emb.main(argv)
    return api, report


def test_unmet_prerequisite_is_recorded_not_silently_skipped(
    monkeypatch, patched_fetch_markdown
):
    api, report = _run_main(
        monkeypatch,
        [PRESS_CASE_UNCONVERTED],
        invoke_text_stub=lambda **kw: (_ for _ in ()).throw(
            AssertionError("LLM must not be called: no converted material")),
        argv=["--dry-run"],
    )
    statuses = {r["status"] for r in report.rows}
    assert "unmet" in statuses
    assert report.rows[0]["status"] == "unmet"
    assert report.rows[0]["reason"]  # a real reason string, never blank


def test_already_populated_case_is_skipped_without_calling_llm(
    monkeypatch, patched_fetch_markdown
):
    api, report = _run_main(
        monkeypatch,
        [PRESS_CASE_ALREADY_POPULATED],
        invoke_text_stub=lambda **kw: (_ for _ in ()).throw(
            AssertionError("LLM must not be called for an already-populated case")),
        argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "already"
    assert api.patched == []


def test_force_reruns_an_already_populated_case(monkeypatch, patched_fetch_markdown):
    response = json.dumps({
        "bigo": 50000000, "confidence": "high",
        "evidence_quote": "बिगो रु. ५,००,००,००० कायम भएको छ",
    })
    api, report = _run_main(
        monkeypatch,
        [PRESS_CASE_ALREADY_POPULATED],
        invoke_text_stub=lambda **kw: response,
        argv=["--force", "--apply"],
    )
    assert report.rows[0]["status"] == "enriched"
    assert api.patched == [("case-populated", "bigo", 50000000)]


def test_dry_run_extracts_but_does_not_patch(monkeypatch, patched_fetch_markdown):
    response = json.dumps({
        "bigo": 10403941, "confidence": "high",
        "evidence_quote": "बिगो रु. १,०४,०३,९४१ कायम भएको छ",
    })
    api, report = _run_main(
        monkeypatch,
        [PRESS_CASE_READY],
        invoke_text_stub=lambda **kw: response,
        argv=["--dry-run"],
    )
    assert report.rows[0]["status"] == "would-enrich"
    assert api.patched == []


def test_apply_patches_the_extracted_bigo(monkeypatch, patched_fetch_markdown):
    response = json.dumps({
        "bigo": 10403941, "confidence": "high",
        "evidence_quote": "बिगो रु. १,०४,०३,९४१ कायम भएको छ",
    })
    api, report = _run_main(
        monkeypatch,
        [PRESS_CASE_READY],
        invoke_text_stub=lambda **kw: response,
        argv=["--apply"],
    )
    assert report.rows[0]["status"] == "enriched"
    assert api.patched == [("case-ready", "bigo", 10403941)]


def test_llm_decline_is_recorded_as_skipped_not_enriched(
    monkeypatch, patched_fetch_markdown
):
    response = json.dumps({
        "bigo": None, "confidence": "high",
        "evidence_quote": "रंगेहात पक्राउ - सोझै फिर्ता", "press_release_type": "sting_operation",
    })
    api, report = _run_main(
        monkeypatch,
        [PRESS_CASE_LLM_DECLINES],
        invoke_text_stub=lambda **kw: response,
        argv=["--apply"],
    )
    assert report.rows[0]["status"] == "skipped"
    assert api.patched == []


def test_llm_invoked_with_premium_tier(monkeypatch, patched_fetch_markdown):
    """Pins the donor's `tier="premium"` argument (enrich_missing_bigo.py:446)."""
    seen_tiers = []

    def stub(**kw):
        seen_tiers.append(kw.get("tier"))
        return json.dumps({
            "bigo": 10403941, "confidence": "high",
            "evidence_quote": "बिगो रु. १,०४,०३,९४१ कायम भएको छ",
        })

    _run_main(monkeypatch, [PRESS_CASE_READY], invoke_text_stub=stub, argv=["--apply"])
    assert seen_tiers == ["premium"]


# --------------------------------------------------------------------------
# Task PP2 -- run-logging events file. `conftest.py`'s autouse
# `_isolate_casework_run_logs` fixture points `CASEWORK_RUN_LOG_DIR` at
# `tmp_path`, so the events file `main()` produces lands there, not in the
# real repo `work/enricher-runs/`.
# --------------------------------------------------------------------------


def _events_path():
    logger = logging.getLogger("casework.bigo")
    return logger._casework_run_paths["events"]


def _read_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_events_file_covers_start_extract_write_on_apply_happy_path(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    response = json.dumps({
        "bigo": 10403941, "confidence": "high",
        "evidence_quote": "बिगो रु. १,०४,०३,९४१ कायम भएको छ",
    })
    _run_main(monkeypatch, [PRESS_CASE_READY], invoke_text_stub=lambda **kw: response,
              argv=["--apply"])

    rows = _read_events(_events_path())
    assert rows, "events file must not be empty"

    required_keys = {"ts", "run_id", "stage", "slug", "step", "status", "detail", "elapsed_ms"}
    for row in rows:
        assert required_keys <= set(row.keys())
        assert row["slug"] == "case-ready"
        assert row["stage"] == "bigo"

    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("start", "start") in steps_and_statuses
    assert ("extract", "ok") in steps_and_statuses
    assert ("write", "enriched") in steps_and_statuses


def test_events_file_records_would_enrich_under_dry_run(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    response = json.dumps({
        "bigo": 10403941, "confidence": "high",
        "evidence_quote": "बिगो रु. १,०४,०३,९४१ कायम भएको छ",
    })
    _run_main(monkeypatch, [PRESS_CASE_READY], invoke_text_stub=lambda **kw: response,
              argv=["--dry-run"])

    rows = _read_events(_events_path())
    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("write", "would-enrich") in steps_and_statuses
    # A dry run must never emit an "enriched" write event.
    assert ("write", "enriched") not in steps_and_statuses
