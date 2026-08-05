"""Tests for the DB-free evidence-note enricher (casework/enrich_evidence.py).

`enrich_evidence.py` writes two things from ONE document fetch and ONE LLM call:
the document's own reusable abstract onto the shared Material JSON-LD, and a
case-specific note on that document's probative role into the case's
`evidence[].additional_details`.

WHY THE GATE TESTS ARE THE CENTRE OF THIS FILE. The "already enriched" question
here is not "is the field empty" -- 895 of 1,359 production entries are
populated and 646 of those hold a type label rather than a note (median length
32 characters). Gating on emptiness would permanently skip those 646, including
the 105 that are a bare full stop; gating on "populated means placeholder" would
overwrite ~250 real notes, some hand-written, with no way back. So the gate
tests below pin the CONTENT classification against the verbatim strings
production actually holds.
"""
import inspect

import pytest

from casework.enrich_evidence import PROSE_CHARS, classify_note


# ---------------------------------------------------------------------------
# The content gate, one test per row of the brief's content table. The strings
# are VERBATIM production values (profiled 2026-08-03 over all 895 populated
# entries; the `.` and the label family re-measured against the 419 publicly
# readable entries on 2026-08-05).
# ---------------------------------------------------------------------------


def test_an_empty_note_needs_work():
    verdict = classify_note("")
    assert verdict.needs_work
    assert "empty" in verdict.reason


def test_a_whitespace_only_note_needs_work():
    assert classify_note("   \n\t ").needs_work


def test_a_none_note_needs_work():
    """The API returns `None` for a never-set `additional_details`, not `""`."""
    assert classify_note(None).needs_work


def test_a_bare_full_stop_needs_work():
    """105 production entries are exactly this: one period, nothing else. It is
    the single strongest argument against an emptiness gate -- it is populated,
    and it says nothing."""
    verdict = classify_note(".")
    assert verdict.needs_work
    assert "punctuation" in verdict.reason


@pytest.mark.parametrize("filler", ["।", "-", "—", "...", "n/a", "N/A"])
def test_other_content_free_fillers_need_work(filler):
    assert classify_note(filler).needs_work


def test_the_ciaa_press_release_template_label_needs_work():
    """153 production entries. A count of documents is not a note about them."""
    verdict = classify_note("CIAA Press Release (2 documents)")
    assert verdict.needs_work
    assert "label" in verdict.reason


def test_the_court_order_template_label_needs_work():
    """140 production entries."""
    verdict = classify_note("Court Order/Verdict (Document 1)")
    assert verdict.needs_work
    assert "label" in verdict.reason


@pytest.mark.parametrize("label", [
    "CIB press release",            # 90 production entries
    "Nepal Law Commission",         # 18 measured on the public cohort
    "Nepal law commission",         # same label, different casing
    "NEPAL LAW COMMISSION",
    "News Source",                  # 11
    "CIAA",                         # 8
    "CIAA Source",
    "Supreme Court",
    "Special Court",
    "Office of Auditor General",
    "Office of the Attorney General",
    "Office of Attorney General",
    "Power Point Presentation",
    "bolpatra.gov.np",
])
def test_known_type_labels_need_work(label):
    assert classify_note(label).needs_work


def test_a_long_genuine_nepali_note_is_left_alone():
    """~250 production notes are real prose and 131 exceed 400 characters. Some
    are hand-written by a caseworker, and an overwrite is not recoverable."""
    note = (
        "यो प्रेस विज्ञप्ति अख्तियार दुरुपयोग अनुसन्धान आयोगले यस मुद्दामा विशेष अदालतमा "
        "दायर गरेको आरोपपत्रको आधिकारिक अभिलेख हो। यसमा मुद्दा दायर भएको मिति, "
        "प्रतिवादीहरूको परिचय, लगाइएका कसुर, लागू कानुन तथा कायम गरिएको बिगो रकम "
        "उल्लेख भएकाले यो अभियोजनको दाबी पुष्टि गर्ने प्राथमिक स्रोत हो। समाचारमा "
        "आएका विवरणहरू यही कागजातबाट पुष्टि गर्न सकिन्छ। प्रतिवादीले अनुसन्धान "
        "अधिकारी समक्ष गरेको बयान तथा अदालतमा पेस भएका प्रमाणहरूसँग यसलाई मिलाई "
        "हेर्दा आरोपको आधार स्पष्ट हुन्छ, र यसै कारण यो कागजात मुद्दाको सबभन्दा "
        "भरपर्दो प्राथमिक स्रोतका रूपमा रहेको छ।"
    )
    assert len(note) > 400, "the brief pins a 400-char note as the 'leave it alone' case"
    verdict = classify_note(note)
    assert not verdict.needs_work
    assert "prose" in verdict.reason


def test_a_note_at_the_prose_threshold_is_left_alone():
    assert not classify_note("क" * PROSE_CHARS).needs_work


def test_a_note_one_character_under_the_threshold_needs_work():
    """The boundary is asserted from both sides so a threshold change cannot
    pass unnoticed."""
    assert classify_note("क" * (PROSE_CHARS - 1)).needs_work


def test_a_short_outlet_and_date_provenance_line_needs_work():
    """The most common shape in the 61-120 character band, measured on the
    public cohort: it names where the document came from, which is provenance,
    not a note on what the document proves."""
    verdict = classify_note(
        "Onlinekhabar — Article published on Baisakh 14, 2082 (April 27, 2025)")
    assert verdict.needs_work
    assert "short" in verdict.reason


def test_the_threshold_is_120_characters():
    """Not a free parameter. Both of the brief's own production tables count
    "entries with a note >120 chars", and the 61-120 band measured on the public
    cohort is outlet+date provenance lines and headline restatements, while
    everything above it is consistently probative prose ("... पुष्टि गर्छ",
    "प्राथमिक न्यायिक अभिलेख हो")."""
    assert PROSE_CHARS == 120


def test_prose_is_measured_on_the_stripped_note():
    """Padding must not buy its way past the gate."""
    assert classify_note(" " * 200 + "छोटो टिप्पणी।").needs_work


# ---------------------------------------------------------------------------
# The gate is DETERMINISTIC. The donor decided this with an LLM call per entry
# per field (`judge_description_adequacy`); this port must not, because the
# classification has to be reviewable and reproducible.
# ---------------------------------------------------------------------------


def test_classify_note_takes_only_the_note_text():
    """No `invoke_text`, no `usage`, no `api` -- a signature that cannot make a
    network call. This is the structural half of "deterministic, not an LLM
    judge"; a reader can verify it without reading the body."""
    params = list(inspect.signature(classify_note).parameters)
    assert params == ["note"]


def test_classify_note_is_stable_across_calls():
    note = "CIAA Press Release (2 documents)"
    assert classify_note(note) == classify_note(note)


# ---------------------------------------------------------------------------
# The evidence merge. `/evidence` is a whole-list REPLACE: the server deletes
# every join row and recreates from exactly what is sent, so an omission
# silently destroys a binding `bind_materials.py` spent a batch establishing.
# Modelled on `tests/casework/test_bind_materials.py`, which tests the same
# union-not-clobber contract for the same array.
# ---------------------------------------------------------------------------

_PR = "https://jawafdehi.org/material/ciaa_press_release/2579"
_CO = "https://jawafdehi.org/material/court_order/special.078-cr-0001"
_NEWS = "https://jawafdehi.org/material/news/9001"

THREE_ENTRY_CASE = {
    "slug": "case-three",
    "state": "DRAFT",
    "evidence": [
        {"material_iri": _PR, "additional_details": "CIAA Press Release (2 documents)",
         "material": {"material_type": "press_release",
                      "urls": [{"link": "https://x/pr.md", "role": "MARKDOWN"}]}},
        {"material_iri": _CO, "additional_details": "",
         "material": {"material_type": "court_order",
                      "urls": [{"link": "https://x/co.md", "role": "MARKDOWN"}]}},
        {"material_iri": _NEWS, "additional_details": "पहिले लेखिएको साँचो टिप्पणी।",
         "material": {"material_type": "news", "urls": []}},
    ],
}


def test_merge_notes_returns_every_entry_when_only_one_changes():
    """The mandatory merge test: 3 entries in, 3 entries out."""
    from casework.enrich_evidence import merge_notes

    merged = merge_notes(THREE_ENTRY_CASE, {_CO: "यो फैसला मुद्दाको प्राथमिक अभिलेख हो।"})

    assert len(merged) == 3
    assert [e["material_iri"] for e in merged] == [_PR, _CO, _NEWS]


def test_merge_notes_leaves_the_untouched_entries_byte_identical():
    from casework.enrich_evidence import merge_notes

    merged = merge_notes(THREE_ENTRY_CASE, {_CO: "यो फैसला मुद्दाको प्राथमिक अभिलेख हो।"})

    assert merged[0] == {"material_iri": _PR,
                         "additional_details": "CIAA Press Release (2 documents)"}
    assert merged[2] == {"material_iri": _NEWS,
                         "additional_details": "पहिले लेखिएको साँचो टिप्पणी।"}


def test_merge_notes_writes_the_new_note_on_the_named_entry_only():
    from casework.enrich_evidence import merge_notes

    merged = merge_notes(THREE_ENTRY_CASE, {_CO: "यो फैसला मुद्दाको प्राथमिक अभिलेख हो।"})

    assert merged[1]["additional_details"] == "यो फैसला मुद्दाको प्राथमिक अभिलेख हो।"


def test_merge_notes_with_no_new_notes_reproduces_the_current_list():
    """The no-op case has to be recognisable as a no-op, or every run rewrites
    every case's whole evidence array for nothing."""
    from casework.enrich_evidence import merge_notes
    from casework.bind_materials import current_evidence

    assert merge_notes(THREE_ENTRY_CASE, {}) == current_evidence(THREE_ENTRY_CASE)


def test_merge_notes_writes_the_shape_the_case_patch_expects():
    """`{material_iri, additional_details}` and nothing else -- the resolved
    `material` block is read-only serializer output and echoing it back is what
    `EvidenceListField` rejects."""
    from casework.enrich_evidence import merge_notes

    for entry in merge_notes(THREE_ENTRY_CASE, {_PR: "नयाँ टिप्पणी।"}):
        assert set(entry) == {"material_iri", "additional_details"}


def test_merge_notes_ignores_an_iri_that_is_not_bound_to_this_case():
    """A note for a material this case does not cite must not be appended --
    that would BIND a new document as a side effect of writing a note."""
    from casework.enrich_evidence import merge_notes

    merged = merge_notes(THREE_ENTRY_CASE, {"https://jawafdehi.org/material/news/404": "x"})

    assert len(merged) == 3
    assert all(e["additional_details"] != "x" for e in merged)


# ---------------------------------------------------------------------------
# End-to-end `main()`: the dedup rule, the target split, and the write paths.
# ---------------------------------------------------------------------------

import json      # noqa: E402 - grouped with the main() harness it serves
import sys       # noqa: E402
import types     # noqa: E402

from casework import enrich_evidence as ee   # noqa: E402
from tests.casework.fakes import FakeUsage   # noqa: E402

_PR_MD = "https://x/pr.md"
_CO_MD = "https://x/co.md"

BASE_ARGV = ["--api-base-url", "http://127.0.0.1:48010"]


def _case(slug, entries, state="DRAFT"):
    return {"slug": slug, "state": state, "title": f"मुद्दा {slug}",
            "court_cases": ["https://jawafdehi.org/courtcase/special/078-cr-0001"],
            "key_allegations": ["गैरकानूनी सम्पत्ति आर्जन"],
            "entities": [{"display_name": "राम बहादुर", "type": "accused"}],
            "evidence": entries}


def _entry(iri, mtype, note="", md=None):
    urls = [{"link": md, "role": "MARKDOWN"}] if md else []
    return {"material_iri": iri, "additional_details": note,
            "material": {"material_type": mtype, "urls": urls}}


class _StubApi:
    """Mirrors `CaseworkApi`'s surface for the calls `main()` makes."""

    def __init__(self, cases, etag='W/"case-1"', materials=None,
                 material_etag='"mat-1"'):
        self._cases = {c["slug"]: dict(c) for c in cases}
        self._etag = etag
        self._materials = dict(materials or {})
        self._material_etag = material_etag
        self.replaced = []          # (slug, path, items, if_match)
        self.material_patches = []  # (iri, ops, if_match)

    def iter_cases(self, params=None, timeout=60, progress=None):
        yield from self._cases.values()

    def get_case_with_etag(self, slug, timeout=60):
        return self._cases[slug], self._etag

    def get_material_with_etag(self, iri, timeout=60):
        return self._materials.get(iri, {"@id": iri}), self._material_etag

    def replace_list(self, slug, path, items, timeout=60, if_match=None):
        self.replaced.append((slug, path, items, if_match))
        return {}

    def patch_material(self, iri, ops, timeout=60, if_match=None):
        self.material_patches.append((iri, ops, if_match))
        return {}


def _stub_llm(material_description="यो अख्तियारको प्रेस विज्ञप्ति हो।",
              evidence_note="यो कागजातले आरोपको आधार पुष्टि गर्छ।"):
    calls = []

    def stub(**kw):
        calls.append(kw)
        return json.dumps({"material_description": material_description,
                           "evidence_note": evidence_note})

    stub.calls = calls
    return stub


@pytest.fixture
def stub_fetch(monkeypatch):
    import casework.common.materials as m
    monkeypatch.setattr(m, "fetch_markdown", lambda link, timeout=60: {
        _PR_MD: "अख्तियारले मिति २०७८।०४।०१ मा विशेष अदालतमा आरोपपत्र दायर गरेको।",
        _CO_MD: "विशेष अदालतले प्रतिवादीलाई दोषी ठहर गरेको फैसला।",
    }.get(link, ""))


def _run_main(monkeypatch, api, llm, argv):
    monkeypatch.setattr(ee, "build_api", lambda args: api)
    monkeypatch.setattr(ee, "bootstrap", lambda *a, **k: None)
    fake_invoke = types.ModuleType("llm.invoke")
    fake_invoke.invoke_text = llm
    fake_usage = types.ModuleType("llm.usage")
    fake_usage.UsageAccumulator = FakeUsage
    fake_usage.render_usage_table = lambda by_provider, title=None: ""
    monkeypatch.setitem(sys.modules, "llm.invoke", fake_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_usage)
    return ee.main(argv)


# -- the dedup rule ---------------------------------------------------------


def test_one_material_cited_by_two_cases_gets_one_abstract_write(
        monkeypatch, stub_fetch):
    """The brief's dedup requirement. A press release cited by 40 cases must not
    be described 40 times -- the abstract is case-agnostic, so every rewrite
    after the first pays for an answer already on the document."""
    api = _StubApi([
        _case("case-a", [_entry(_PR, "press_release", md=_PR_MD)]),
        _case("case-b", [_entry(_PR, "press_release", md=_PR_MD)]),
    ])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert [iri for iri, _, _ in api.material_patches] == [_PR]


def test_both_cases_still_get_their_own_case_specific_note(
        monkeypatch, stub_fetch):
    """The other half of dedup: the ABSTRACT is shared, the NOTE is not. A run
    that deduplicated the note as well would leave the second case blank."""
    api = _StubApi([
        _case("case-a", [_entry(_PR, "press_release", md=_PR_MD)]),
        _case("case-b", [_entry(_PR, "press_release", md=_PR_MD)]),
    ])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert sorted(slug for slug, _, _, _ in api.replaced) == ["case-a", "case-b"]


def test_an_already_described_material_is_never_rewritten(monkeypatch, stub_fetch):
    """"Only when blank" -- the abstract is shared, so overwriting one is a write
    whose blast radius this run cannot see."""
    api = _StubApi(
        [_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])],
        materials={_PR: {"@id": _PR, "description": {"ne": "पहिले लेखिएको सार।"}}},
    )

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert api.material_patches == []


def test_force_rewrites_an_already_described_material(monkeypatch, stub_fetch):
    api = _StubApi(
        [_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])],
        materials={_PR: {"@id": _PR, "description": {"ne": "पहिले लेखिएको सार।"}}},
    )

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply", "--force"])

    assert [iri for iri, _, _ in api.material_patches] == [_PR]


# -- the --target split -----------------------------------------------------


def test_target_evidence_writes_no_material(monkeypatch, stub_fetch):
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(),
              BASE_ARGV + ["--apply", "--target", "evidence"])

    assert api.material_patches == []
    assert len(api.replaced) == 1


def test_target_material_writes_no_case(monkeypatch, stub_fetch):
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(),
              BASE_ARGV + ["--apply", "--target", "material"])

    assert api.replaced == []
    assert len(api.material_patches) == 1


def test_target_source_is_no_longer_accepted():
    """Deviation 5: the donor's `source` named a model that no longer exists.
    Silently accepting it would write materials while the operator believed they
    were writing the dead endpoint."""
    with pytest.raises(SystemExit):
        ee.build_parser().parse_args(BASE_ARGV + ["--target", "source"])


# -- the write shape --------------------------------------------------------


def test_the_material_write_uses_an_add_op_on_a_language_map(monkeypatch, stub_fetch):
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    _, ops, _ = api.material_patches[0]
    assert ops == [{"op": "add", "path": "/description",
                    "value": {"ne": "यो अख्तियारको प्रेस विज्ञप्ति हो।"}}]


def test_the_material_write_is_conditional_on_the_material_etag(
        monkeypatch, stub_fetch):
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert api.material_patches[0][2] == '"mat-1"'


def test_the_case_write_is_conditional_on_the_case_etag(monkeypatch, stub_fetch):
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert api.replaced[0][3] == 'W/"case-1"'


def test_a_case_with_no_etag_is_not_written(monkeypatch, stub_fetch):
    """The whole-list replace is destructive, so without an If-Match a
    concurrent edit is silently destroyed. Refuse instead -- the same contract
    `bind_materials.apply_plan` already holds for this array."""
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])],
                   etag=None)

    report = _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert api.replaced == []
    assert "error" in report.summary()


def test_the_case_write_preserves_an_unwritable_sibling_entry(
        monkeypatch, stub_fetch):
    """An entry with no MARKDOWN gets no note -- and must still come back in the
    list, or the write deletes the binding."""
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", md=_PR_MD),
        _entry(_NEWS, "news", note="News Source"),   # no MARKDOWN role
    ])])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    _, _, items, _ = api.replaced[0]
    assert [e["material_iri"] for e in items] == [_PR, _NEWS]
    assert items[1]["additional_details"] == "News Source"


# -- the gate, driven through main() ---------------------------------------


def test_a_case_whose_notes_are_all_prose_is_not_written(monkeypatch, stub_fetch):
    prose = "क" * 400
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", note=prose, md=_PR_MD)])],
        materials={_PR: {"@id": _PR, "description": {"ne": "सार छ।"}}})

    report = _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert api.replaced == []
    assert "already" in report.summary()


def test_a_prose_note_makes_no_llm_call_at_all(monkeypatch, stub_fetch):
    """The gate must run BEFORE the model. The donor's LLM judge paid for a call
    to decide this; paying a premium document-reading call to then skip is
    strictly worse."""
    prose = "क" * 400
    llm = _stub_llm()
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", note=prose, md=_PR_MD)])],
        materials={_PR: {"@id": _PR, "description": {"ne": "सार छ।"}}})

    _run_main(monkeypatch, api, llm, BASE_ARGV + ["--apply"])

    assert llm.calls == []


def test_force_overrides_the_prose_gate(monkeypatch, stub_fetch):
    prose = "क" * 400
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", note=prose, md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply", "--force"])

    assert len(api.replaced) == 1


def test_a_filler_note_is_regenerated(monkeypatch, stub_fetch):
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", note=".", md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    _, _, items, _ = api.replaced[0]
    assert items[0]["additional_details"] == "यो कागजातले आरोपको आधार पुष्टि गर्छ।"


# -- one fetch, one call ----------------------------------------------------


def test_both_descriptions_come_from_a_single_llm_call(monkeypatch, stub_fetch):
    """The donor's shared-fetch design: one document fetch and one call yields
    BOTH descriptions. Two calls would double the cost of the most expensive
    stage in the pipeline for no new information."""
    llm = _stub_llm()
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    _run_main(monkeypatch, api, llm, BASE_ARGV + ["--apply"])

    assert len(llm.calls) == 1
    assert len(api.material_patches) == 1
    assert len(api.replaced) == 1


def test_the_call_runs_at_the_premium_tier(monkeypatch, stub_fetch):
    llm = _stub_llm()
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    _run_main(monkeypatch, api, llm, BASE_ARGV + ["--apply"])

    assert llm.calls[0]["tier"] == "premium"


# -- dry run ----------------------------------------------------------------


def test_dry_run_writes_nothing_anywhere(monkeypatch, stub_fetch):
    llm = _stub_llm()
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    report = _run_main(monkeypatch, api, llm, BASE_ARGV + ["--dry-run"])

    assert api.replaced == []
    assert api.material_patches == []
    assert llm.calls, "a dry run still makes the LLM call -- it only skips the write"
    assert "would-enrich" in report.summary()


def test_dry_run_still_writes_a_review_file(monkeypatch, stub_fetch, tmp_path):
    """The dry run is the read-only path, so it is where accuracy gets judged.
    A review file that only appeared on --apply would mean output could never be
    checked without first writing it somewhere."""
    out = tmp_path / "review.md"
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(),
              BASE_ARGV + ["--dry-run", "--review-file", str(out)])

    text = out.read_text(encoding="utf-8")
    assert "case-a" in text
    assert "यो कागजातले आरोपको आधार पुष्टि गर्छ।" in text, "Devanagari must be unescaped"
    assert _PR in text, "the review file must name the material IRI it came from"


def test_the_review_file_shows_the_before_value(monkeypatch, stub_fetch, tmp_path):
    out = tmp_path / "review.md"
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", note="CIAA Press Release (2 documents)",
               md=_PR_MD)])])

    _run_main(monkeypatch, api, _stub_llm(),
              BASE_ARGV + ["--dry-run", "--review-file", str(out)])

    assert "CIAA Press Release (2 documents)" in out.read_text(encoding="utf-8")


# -- failure paths ----------------------------------------------------------


def test_an_unmet_prerequisite_is_recorded_not_silently_skipped(
        monkeypatch, stub_fetch):
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release")])])  # no MARKDOWN

    report = _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--dry-run"])

    assert "unmet" in report.summary()
    assert api.replaced == []


def test_an_unparseable_llm_reply_writes_nothing(monkeypatch, stub_fetch):
    api = _StubApi([_case("case-a", [_entry(_PR, "press_release", md=_PR_MD)])])

    report = _run_main(monkeypatch, api, lambda **kw: "sorry, no JSON here",
                       BASE_ARGV + ["--apply"])

    assert api.replaced == []
    assert api.material_patches == []
    assert report.summary()


def test_one_failing_case_does_not_sink_the_batch(monkeypatch, stub_fetch):
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider exploded")
        return json.dumps({"material_description": "सार।", "evidence_note": "टिप्पणी।"})

    api = _StubApi([
        _case("case-a", [_entry(_PR, "press_release", md=_PR_MD)]),
        _case("case-b", [_entry(_CO, "court_order", md=_CO_MD)]),
    ])

    _run_main(monkeypatch, api, flaky, BASE_ARGV + ["--apply"])

    assert [slug for slug, _, _, _ in api.replaced] == ["case-b"]


# -- the prompt's case-agnostic guard --------------------------------------


def test_the_system_prompt_forbids_case_framing_in_the_shared_abstract():
    """The donor's hard guard, and the reason the abstract is safe to share: the
    text lands on a document cited by many cases, so any "यस मुद्दामा" in it is
    wrong for all but one of them. The brief requires this guard be kept."""
    prompt = ee.SYSTEM_PROMPT
    assert "CASE-AGNOSTIC" in prompt
    assert "यस मुद्दा" in prompt
    assert "MUST NOT" in prompt


def test_the_system_prompt_asks_for_a_probative_role_not_a_resummary():
    assert "PROBATIVE ROLE" in ee.SYSTEM_PROMPT
    assert "re-summarise" in ee.SYSTEM_PROMPT


def test_the_system_prompt_forbids_invention():
    assert "never invent" in ee.SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Fetch only what the gate asked for. A court order averages 52,000 characters,
# so downloading one for an entry that is then skipped is pure waste -- and on a
# re-run over the 567-case population that is ~250 discarded fetches.
# ---------------------------------------------------------------------------


@pytest.fixture
def counting_fetch(monkeypatch):
    import casework.common.materials as m
    seen = []

    def fake(link, timeout=60):
        seen.append(link)
        return {_PR_MD: "अख्तियारको प्रेस विज्ञप्ति।",
                _CO_MD: "विशेष अदालतको फैसला।"}.get(link, "")

    monkeypatch.setattr(m, "fetch_markdown", fake)
    return seen


def test_a_skipped_entry_never_has_its_document_fetched(monkeypatch, counting_fetch):
    """Entry 1 carries real prose and is skipped; entry 2 is blank and is
    enriched. Only entry 2's document may be downloaded."""
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", note="क" * 400, md=_PR_MD),
        _entry(_CO, "court_order", note="", md=_CO_MD),
    ])], materials={_PR: {"@id": _PR, "description": {"ne": "सार छ।"}}})

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert counting_fetch == [_CO_MD]


def test_a_fully_enriched_case_fetches_no_documents_at_all(monkeypatch, counting_fetch):
    """The re-run case. Nothing needs writing, so nothing is downloaded."""
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", note="क" * 400, md=_PR_MD),
        _entry(_CO, "court_order", note="ख" * 400, md=_CO_MD),
    ])], materials={
        _PR: {"@id": _PR, "description": {"ne": "सार छ।"}},
        _CO: {"@id": _CO, "description": {"ne": "सार छ।"}},
    })

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert counting_fetch == []


def test_an_entry_needing_only_an_abstract_still_fetches_its_document(
        monkeypatch, counting_fetch):
    """The note is prose (skip) but the material has no abstract, so the
    document is still needed. A filter keyed on the note alone would starve the
    abstract half."""
    api = _StubApi([_case("case-a", [
        _entry(_PR, "press_release", note="क" * 400, md=_PR_MD),
    ])])

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert counting_fetch == [_PR_MD]
    assert len(api.material_patches) == 1


def test_a_material_is_read_once_per_run_not_once_per_citing_case(
        monkeypatch, stub_fetch):
    """`materials_done` must suppress the repeated GET too, not only the
    repeated write -- otherwise a material cited by 40 cases costs 40 reads."""
    reads = []
    api = _StubApi([
        _case("case-a", [_entry(_PR, "press_release", md=_PR_MD)]),
        _case("case-b", [_entry(_PR, "press_release", md=_PR_MD)]),
    ])
    original = api.get_material_with_etag

    def counting(iri, timeout=60):
        reads.append(iri)
        return original(iri, timeout)

    api.get_material_with_etag = counting

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert reads == [_PR]


def test_an_already_described_material_is_read_once_per_run_too(
        monkeypatch, stub_fetch):
    """The dedup set has to cover materials this run DECIDES NOT to write, not
    only ones it writes. An already-described document cited by 40 cases would
    otherwise cost 40 GETs to reach the same "leave it alone" answer."""
    reads = []
    api = _StubApi([
        _case("case-a", [_entry(_PR, "press_release", md=_PR_MD)]),
        _case("case-b", [_entry(_PR, "press_release", md=_PR_MD)]),
    ], materials={_PR: {"@id": _PR, "description": {"ne": "पहिलेको सार।"}}})
    original = api.get_material_with_etag

    def counting(iri, timeout=60):
        reads.append(iri)
        return original(iri, timeout)

    api.get_material_with_etag = counting

    _run_main(monkeypatch, api, _stub_llm(), BASE_ARGV + ["--apply"])

    assert reads == [_PR]
    assert api.material_patches == []
