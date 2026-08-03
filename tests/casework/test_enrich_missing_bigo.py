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
import urllib.error
from pathlib import Path

import pytest

from casework import enrich_missing_bigo as emb
from casework.enrich_missing_bigo import (
    amount_is_grounded,
    coerce_bigo_int,
    is_explicit_bigo_context,
    parse_bigo_response,
    rupee_amounts_in,
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


def test_va_spelled_bigo_quote_is_accepted():
    """A quote spelling the word विगो must not be discarded.

    These press releases are legacy-font PDFs, not scans, and render बिगो as विगो
    in 150 of the 796 bigo tokens across the FY078/079 238-case batch. The gate
    listed only the ब spelling, so 079-CR-0080's correct high-confidence 7000 was
    thrown away and recorded as "LLM could not extract a reliable BIGO".
    Verbatim quote from that document.
    """
    body = json.dumps(
        {
            "bigo": 7000,
            "confidence": "high",
            "evidence_quote": (
                "निज सशस्त्र प्रहरी जवान मुख्तार देवानलाई उल्लेखित कसुरमा "
                "विगो रु.7,000।- (सात हजार रुपैयाँ) कायम गरी"
            ),
            "press_release_type": "charge_filing",
        }
    )
    assert parse_bigo_response(body) == 7000


class TestAmountIsGrounded:
    """The grounding gate: a bigo the source never states must not be written.

    Every fixture here is a verbatim span from a real document, and every rejected
    value is one that a real run actually produced.
    """

    # 078-CR-0116, the 10x inflation. Charge sheet declares रु.१,३८,९९,९९८।८७;
    # the run proposed 138999998, whose digits appear nowhere in the document.
    CR_0116 = (
        "ओम सप्लायर्स, कलैया 6, बारा र ऐ.ऐ.का प्रोपाइटर निज ओम शंकर प्रसादलाई "
        "बिगो रु.1,38,99,998।87 (एक करोड अठतीस लाख उनान्सय हजार नौ सय अन्ठानब्बे) कायम गरी"
    )

    def test_rejects_the_078_cr_0116_ten_x_inflation(self):
        assert amount_is_grounded(self.CR_0116, 13899998) is True
        assert amount_is_grounded(self.CR_0116, 138999998) is False

    def test_rejects_a_paisa_fold(self):
        """1389999887 is रупees..paisa concatenated -- never a real amount."""
        assert amount_is_grounded(self.CR_0116, 1389999887) is False

    def test_rejects_an_unstated_arithmetic_sum(self):
        """079-CR-0067: the run summed three per-defendant विगो into a total the
        release never states (35,200 + 55,200 + 177,600 = 268,000)."""
        text = (
            "मोहन प्रसाद आचायिउपर बिगो रु.55,200।– कायम गरी ... "
            "जानुका श्रेष्ठउपर बिगो रु.1,77,600।- कायम गरी ... "
            "पवन श्रेष्ठउपर विगो रु.35,200।- कायम गरी"
        )
        for stated in (55200, 177600, 35200):
            assert amount_is_grounded(text, stated) is True
        assert amount_is_grounded(text, 268000) is False

    def test_accepts_devanagari_digits(self):
        """079-CR-0136 states the amount in Devanagari; the gate must normalise."""
        text = "निज विष्णु कान्त मिरलेलाई बिगो रु.७२,५२,४८०।५९ कायम गरी"
        assert amount_is_grounded(text, 7252480) is True
        assert amount_is_grounded(text, 725248059) is False

    def test_accepts_a_table_cell_amount(self):
        """078-CR-0073 states its total in a बिगो रु. table column, not inline."""
        text = "सि.नं | पद, नामथर | कार्यालय | बिगो रु. |\n1. | अध्यक्ष | लिखु | रु.4,21,00,706।24 |"
        assert amount_is_grounded(text, 42100706) is True

    def test_short_value_is_not_grounded_by_being_a_prefix(self):
        """The guard against the obvious false positive: 1389 must not count as
        'stated' just because 13899998 contains it."""
        assert amount_is_grounded("रु.1,38,99,998।87", 1389) is False
        assert amount_is_grounded("रु.1,38,99,998।87", 138) is False

    def test_none_passes_through(self):
        """A null bigo is the enricher's own 'found nothing' -- not ungrounded."""
        assert amount_is_grounded("anything", None) is True

    def test_zero_paisa_dash_suffix(self):
        """078-CR-0101 writes whole rupees as '।-' (danda then hyphen)."""
        assert amount_is_grounded("निजलाई बिगो रु.७,६७,९८८।- कायम गरी", 767988) is True

    def test_rupee_amounts_drops_paisa_and_commas(self):
        assert rupee_amounts_in("रु.6,79,12,383।98") == {67912383}


def test_va_spelled_magadavi_quote_is_accepted():
    """मागदावी is the same ब<->व variant as मागदाबी and appears in 156 of the 238.

    The quote deliberately carries NO other whitelisted keyword -- no बिगो, no
    हानि/नोक्सानी -- so मागदावी alone has to carry the gate. Otherwise the test
    passes on an unrelated keyword and proves nothing.
    """
    quote = "सजायको मागदावी लिई आज विशेष अदालत, काठमाडौंमा आरोपपत्र दायर गरिएको छ"
    assert not any(k in quote for k in emb.BIGO_CONTEXT_KEYWORDS if k != "मागदावी")
    body = json.dumps(
        {"bigo": 2451526, "confidence": "high", "evidence_quote": quote}
    )
    assert parse_bigo_response(body) == 2451526


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


def _quoting_invoke(bigo, captured=None):
    """A fake `invoke_text` returning `bigo` with a quote that clears the
    keyword guard. Pass `captured` to record the prompt that was actually sent.
    """
    def fake_invoke(**kw):
        if captured is not None:
            captured["content"] = kw["content"]
        return json.dumps({
            "bigo": bigo, "confidence": "high",
            "evidence_quote": f"बिगो रु.{bigo} कायम गरी",
        })

    return fake_invoke


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


class TestGroundingSpansEverythingTheModelWasShown:
    """`_extract_bigo` sends the source METADATA as well as the markdown body, so
    the gate has to check both. `_source_metadata`'s own docstring records that a
    material `display_name` is frequently where the बिगो is first stated
    ("... उपर बिगो रु.९०,३९,६२०।३९ कायम"). Grounding the body alone rejected a
    figure the model read correctly out of the title, and skipped the case.
    """

    # The बिगो is stated ONLY in the material display_name -- never in the body.
    CASE = {
        "slug": "case-x", "title": "t",
        "evidence": [{
            "material_iri": "https://jawafdehi.org/material/1",
            "material": {
                "material_type": "press_release",
                "display_name": "... उपर बिगो रु.९०,३९,६२०।३९ कायम",
                "urls": [{"role": "MARKDOWN", "link": "https://x/p.md"}],
            },
        }],
    }

    def test_an_amount_stated_only_in_the_display_name_is_grounded(self):
        body = "आरोपपत्र दायर गरिएको छ।"
        assert amount_is_grounded(body, 9039620) is False
        _, shown = emb._extract_bigo(
            body, self.CASE, _quoting_invoke(9039620), _FakeUsage()
        )
        assert amount_is_grounded(shown, 9039620) is True

    def test_the_gate_checks_the_same_string_the_prompt_carried(self):
        """`_extract_bigo` returns its composed source text so the gate can check
        that exact string. Rebuilding it at the call site meant two constructions
        of "what the model was shown" that a prompt change would silently drift
        apart -- and it ran `_source_metadata` a second time per case.
        """
        captured = {}
        _, shown = emb._extract_bigo(
            "आरोपपत्र दायर गरिएको छ।", self.CASE,
            _quoting_invoke(9039620, captured), _FakeUsage(),
        )
        for line in shown.splitlines():
            if line.strip():
                assert line in captured["content"], (
                    f"grounding text carries a line the prompt never did: {line!r}"
                )

    def test_a_figure_the_clamp_cut_out_of_the_prompt_is_not_grounded(self):
        """The clamp is why the gate cannot rebuild its own text.

        `_extract_bigo` truncates the body at `FEED_CHARS`. An unclamped rebuild
        grounds against the discarded tail too, so a fabricated amount that
        happens to match text the model could not read would pass -- exactly the
        coincidence the gate exists to reject.
        """
        long_body = "क" * emb.FEED_CHARS + " बिगो रु.९०,३९,६२०।३९ कायम"
        captured = {}
        _, shown = emb._extract_bigo(
            long_body, {"slug": "case-y", "title": "t", "evidence": []},
            _quoting_invoke(9039620, captured), _FakeUsage(),
        )

        assert "९०,३९,६२०" not in captured["content"], "clamp did not cut the tail"
        assert amount_is_grounded(long_body, 9039620) is True, (
            "control: the raw body does state the figure"
        )
        assert amount_is_grounded(shown, 9039620) is False


class TestRupeeAmountsInTokenisation:
    """Tokenisation rules measured against the 238 FY078/079 sources. Each of
    these was a silent data-loss bug: the gate rejected a correct extraction and
    `main` recorded `skipped`, so the case simply never got a value.
    """

    def test_markdown_table_pipes_are_not_read_as_paisa_separators(self):
        """`|` is in the separator class because OCR renders the danda '।' as a
        pipe -- but in a markdown table it is a cell delimiter. Unbounded, the
        paisa group ate `<serial> | <first group>` and dropped the real amount:
        `| 1 | 35,200 |` yielded {1, 200}, never 35200. 2 of the 238 sources
        contain that shape, and a बिगो in a table column is Rule 6's
        high-confidence signal #2."""
        assert 35200 in rupee_amounts_in("| सि.नं | बिगो रु. |\n| 1 | 35,200 |")
        assert 177600 in rupee_amounts_in("| 2 | 1,77,600 |")
        assert 42100706 in rupee_amounts_in("| 1. | अध्यक्ष | 4,21,00,706।24 |")

    def test_comma_groups_split_by_whitespace_still_parse(self):
        """Markdown extraction turns रु.1,38,99,998।87 into रु. 1, 38, 99, 998।87
        in 8 of the 238 sources."""
        assert 13899998 in rupee_amounts_in("रु. 1, 38, 99, 998।87")

    def test_a_paisa_fold_is_never_admitted(self):
        """The un-truncated reading of a paisa-bearing token IS the paisa-fold
        error (080-CR-0158). Adding it to the set would make the gate bless the
        exact class of bug it exists to catch."""
        amounts = rupee_amounts_in("बिगो रु.1,46,81,225।90")
        assert 14681225 in amounts
        assert 1468122590 not in amounts

    def test_the_dot_separator_matches_coerce_bigo_int(self):
        """An earlier copy of this function omitted '.', which `coerce_bigo_int`
        honours -- so रु.324.57 reduced to 324 on the model's answer but yielded
        {57, 324} here. The two sides of the comparison must not drift."""
        amounts = rupee_amounts_in("रु.324.57")
        assert coerce_bigo_int("रु.324.57") == 324
        assert 324 in amounts
        assert 57 not in amounts


class TestExtractionPromptDecisions:
    """Two prompt choices that were measured, not guessed. Both cost real money
    to establish, so they are pinned here rather than left to a reader's judgement.
    """

    def test_rule_3_subordinates_type_routing_to_a_declared_bigo(self):
        """Rule 3 used to route sting/appeal releases to null BEFORE reading the
        text. Measured over the 238 FY078/079 sources: 0 documents contain any
        sting trigger and 0 contain any appeal trigger, so it never fired -- while
        65 declare a bribe AS the बिगो under दफा ३(१). Had it ever fired it would
        have produced false nulls on figures the document states outright."""
        rule3 = emb.EXTRACTION_SYSTEM_PROMPT.split("Rule 3")[1].split("Rule 4")[0]
        declared = rule3.index("declares a बिगो")
        sting = rule3.index("Sting Operation")
        assert declared < sting, (
            "Rule 3 routes on document type before checking for a declared बिगो; "
            "that is the false-null ordering, and 65 of 238 releases declare a "
            "bribe as the बिगो"
        )

    def test_prompt_has_no_multi_defendant_decision_procedure(self):
        """The four-step "Rule 7b" for multi-defendant cases was built, run over
        all 238, and reverted: accuracy fell from 235/238 to ~228/238. It nulled
        078-CR-0073 despite the release stating `कुल जम्मा रु.4,21,00,706।24`, and
        made three cases return one defendant's figure instead of the case total.
        Giving the model an ordered procedure gave it more ways to go wrong than
        simply reporting the figure labelled बिगो.
        """
        prompt = emb.EXTRACTION_SYSTEM_PROMPT
        assert "Rule 7b" not in prompt
        assert "Rule 7c" not in prompt
        # Rule 7 itself -- "no clear bigo label, return null" -- must stay.
        assert "Rule 7 — Multiple amounts" in prompt


class TestBigoIsPressReleaseOnly:
    """The judgment is deliberately NOT read by this stage.

    Feeding the court order alongside the press release was built, run against
    all 238 bound FY078/079 cases, and reverted (2026-08-03):
      - 52k chars sent per case vs 2.4k press-only -- 22x the input on EVERY
        case, ~2 min/case vs ~15s, projecting ~8h and >$100 against $23.
      - It changed the answer only on the ~18 multi-defendant cases, where the
        press release states per-defendant figures and no total.
      - `bigo` is the ALLEGED loss. The press release IS that claim; the
        judgment records what was ESTABLISHED, so it is the wrong primary
        source for this field regardless of cost.
    These tests exist so re-adding it has to be a deliberate act, not a
    one-word edit to `requires_materials`.
    """

    def test_bigo_stage_gates_on_press_material_only(self):
        """Pins the LITERAL membership, not PRESS_TYPES against itself.

        `set(requires_materials) == set(PRESS_TYPES)` is vacuous -- it stays green
        if `court_order` is added TO `PRESS_TYPES`, or if `charge_sheet` is dropped
        from it. `test_pipeline.py` documents that exact trap.
        """
        from casework.common.pipeline import STAGES

        assert set(STAGES["bigo"].requires_materials) == {
            "press_release", "ciaa_press_release", "charge_sheet",
        }, ("court_order is back on the bigo stage -- that is 22x the input per "
            "case for the ~18 multi-defendant ones; bind the charge sheet instead")

    def test_no_court_material_leaks_into_the_prompt(self):
        """Not just the body text -- the source-metadata block must not advertise
        the judgment either, or the model will report figures it cannot see."""
        captured = {}

        def fake_invoke(**kw):
            captured["content"] = kw["content"]
            return json.dumps({
                "bigo": 7252480, "confidence": "high",
                "evidence_quote": "बिगो रु.७२,५२,४८०।५९ कायम गरी",
            })

        case = {
            "slug": "case-x", "title": "t",
            "evidence": [{
                "material_iri": "https://jawafdehi.org/material/1",
                "material": {
                    "material_type": "court_order",
                    "display_name": "SPECIAL-COURT-JUDGMENT-MARKER",
                    "urls": [{"role": "MARKDOWN", "link": "https://x/j.md"}],
                },
            }],
        }
        emb._extract_bigo("press release text", case, fake_invoke, _FakeUsage())
        assert "SPECIAL-COURT-JUDGMENT-MARKER" not in captured["content"]

    def test_a_total_stated_only_in_the_judgment_is_not_grounded(self):
        """Grounding spans the press release alone. A figure that exists only in
        the unread judgment must be rejected, not written."""
        press = "प्रतिवादीहरूउपर आरोपपत्र दायर गरिएको छ।"
        assert amount_is_grounded(press, 16405000) is False


class TestPreLoopFailuresAreLogged:
    """Everything before the per-case loop must leave a record.

    These paths used to fail silently: a bootstrap error printed to stderr and
    exited, leaving both the run `.log` and `.events.jsonl` at ZERO bytes with no
    record of the target, the mode, or the reason. `log_run_header`/`_footer` only
    reach the `.log`, and `ledger.py` reads only `*.events.jsonl`.
    """

    def test_bootstrap_failure_is_recorded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            emb, "bootstrap",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("SECRET_KEY environment variable must be set")),
        )
        with pytest.raises(SystemExit) as exc:
            emb.main(["--dry-run", "--api-base-url", "https://api.jawafdehi.org"])
        assert exc.value.code == 1

        rows = _read_events(_events_path())
        assert rows, "bootstrap failure left the events log empty"
        started = [r for r in rows if r["step"] == "run" and r["status"] == "start"]
        assert started, "no run/start event -- the run's target and mode are unrecorded"
        assert "target=https://api.jawafdehi.org" in started[0]["detail"]
        assert "mode=DRY-RUN" in started[0]["detail"]
        failed = [r for r in rows if r["step"] == "bootstrap" and r["status"] == "error"]
        assert failed, "the bootstrap error itself was not recorded"
        assert "SECRET_KEY" in failed[0]["detail"]

    def test_missing_credentials_are_recorded_despite_raising_systemexit(
        self, monkeypatch, tmp_path
    ):
        """The credential path raises `SystemExit`, not `Exception`.

        `basic_auth_from_env` calls `raise SystemExit(...)`, which derives from
        BaseException -- so an `except Exception` guard let exactly the failure it
        was added for escape, leaving `run/start` as the last line in the events
        file. That is indistinguishable from a killed run, which is the very thing
        these terminal events exist to disambiguate.
        """
        monkeypatch.setattr(emb, "bootstrap", lambda *a, **k: None)
        for key in ("JAWAFDEHI_API_TOKEN", "CASEWORK_API_USER", "CASEWORK_API_PASSWORD"):
            monkeypatch.delenv(key, raising=False)

        with pytest.raises(SystemExit) as exc:
            emb.main(["--dry-run", "--api-base-url", "https://api.jawafdehi.org"])
        assert exc.value.code == 1

        rows = _read_events(_events_path())
        failed = [r for r in rows if r["step"] == "build_api" and r["status"] == "error"]
        assert failed, "a missing credential was not recorded -- SystemExit escaped"
        assert "SystemExit" in failed[0]["detail"]

    def test_case_listing_failure_is_recorded(self, monkeypatch, tmp_path):
        """An expired token mid-listing must not die as a bare traceback."""
        monkeypatch.setattr(emb, "bootstrap", lambda *a, **k: None)

        class _Boom:
            def iter_cases(self, *a, **k):
                raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

        monkeypatch.setattr(emb, "build_api", lambda args: _Boom())
        with pytest.raises(SystemExit):
            emb.main(["--dry-run", "--api-base-url", "https://api.jawafdehi.org"])

        rows = _read_events(_events_path())
        failed = [r for r in rows if r["step"] == "list_cases" and r["status"] == "error"]
        assert failed, "a failure during case listing was not recorded"
        assert "401" in failed[0]["detail"]

    def test_run_scoped_events_never_appear_as_cases_in_the_ledger(
        self, monkeypatch, tmp_path
    ):
        """Run rows carry no slug, and build_ledger requires slug AND stage."""
        monkeypatch.setattr(
            emb, "bootstrap",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
        )
        with pytest.raises(SystemExit):
            emb.main(["--dry-run", "--api-base-url", "http://127.0.0.1:48010"])

        from casework.ledger import build_ledger

        rows = _read_events(_events_path())
        # Must be meaningful in both directions: the rows exist on disk...
        run_rows = [r for r in rows if r["step"] in ("run", "bootstrap")]
        assert run_rows, "no run-scoped rows were written at all"
        assert all(r["slug"] == "" for r in run_rows), "run rows must carry no slug"
        assert any(r["status"] == "error" for r in run_rows), (
            "an outcome-shaped status is what would leak; the test is vacuous without one"
        )
        # ...and none of them becomes a phantom case in the ledger.
        ledger = build_ledger(tmp_path)
        assert ledger == {}, f"run bookkeeping leaked into the ledger: {ledger}"

    def test_successful_run_emits_a_terminal_run_event(
        self, monkeypatch, tmp_path, patched_fetch_markdown
    ):
        """The presence of run/complete is how you tell a finished run from a
        killed one -- the ledger never reads the .log footer."""
        _run_main(
            monkeypatch,
            [PRESS_CASE_READY],
            invoke_text_stub=lambda **kw: json.dumps(
                {"bigo": 10403941, "confidence": "high",
                 "evidence_quote": "बिगो रु. १,०४,०३,९४१ कायम गरी"}),
            argv=["--dry-run"],
        )
        rows = _read_events(_events_path())
        steps = {(r["step"], r["status"]) for r in rows}
        assert ("run", "start") in steps
        assert ("list_cases", "ok") in steps
        assert ("select", "ok") in steps
        assert ("run", "complete") in steps
        done = [r for r in rows if r["step"] == "run" and r["status"] == "complete"]
        assert "would-enrich=1" in done[0]["detail"]


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
    # Run-scoped rows (run/list_cases/select) deliberately carry no slug so the
    # ledger skips them -- see TestPreLoopFailuresAreLogged. Every CASE row must
    # still name its case, which is what this assertion is for.
    RUN_STEPS = {"run", "list_cases", "select", "bootstrap", "build_api"}
    case_rows = [r for r in rows if r["step"] not in RUN_STEPS]
    assert case_rows, "no per-case events were written"
    for row in rows:
        assert required_keys <= set(row.keys())
        assert row["stage"] == "bigo"
    for row in case_rows:
        assert row["slug"] == "case-ready"

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
