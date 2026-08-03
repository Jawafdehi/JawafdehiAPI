"""Tests for the DB-free standalone description enricher (casework/enrich_description.py).

`enrich_description.py` writes the long public Nepali narrative `Case.description`
from a case's charge sheet, press release and Special Court verdict, at the
premium tier, and writes ONE field.

WHY THERE IS NO BYTE-EQUALITY PROMPT PIN HERE. Every other ported enricher's
test file asserts its prompts are byte-identical to the donor at `0321a85`.
This port's prompts DELIBERATELY diverge -- the title pass is gone, the
`convert_date` tool is gone, and one dates QUALITY RULE is added (see the
module docstring's three numbered deviations). A byte-equality assertion would
therefore have to be deleted or weakened, which is exactly how a real drift
later slips through unnoticed.

So `TestDonorDeviations` pins the DIVERGENCE instead, in both directions: each
intended edit is asserted present, and the donor's untouched section
structure -- the क) … च) block that carries every instruction about what the
public record may contain -- is asserted to have survived verbatim. A clause
lost from that block changes what gets published about named people, with no
other test failing.
"""
import ast
import json
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

from casework import enrich_description as ed
from casework.enrich_description import (
    _assemble_source_text,
    _generate_description,
    _has_substantial_description,
    _ordered_sources,
    _parse_description_response,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DONOR_COMMIT = "0321a85"


def _donor_source() -> str:
    proc = subprocess.run(
        ["git", "show", f"{DONOR_COMMIT}:casework/enrich_description.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(
            f"donor commit {DONOR_COMMIT} not in local history "
            "(shallow clone?); fidelity check needs full git history")
    return proc.stdout


def _donor_system_prompt() -> str:
    """The donor's EXTRACTION_SYSTEM_PROMPT, read out of the donor source.

    It is a BinOp (a string literal, plus the TITLE_RULES name, plus another
    string literal), so `ast.literal_eval` on the whole node fails -- only the
    string literals are evaluable. Joining just those reproduces the donor
    prompt with the `TITLE_RULES` name replaced by nothing, which is exactly
    the comparison this file wants: the donor's own non-title text.

    Collected left-to-right by explicit recursion, NOT `ast.walk`: walk is
    breadth-first, so on `((A + name) + B)` it yields B before A and the
    reassembled prompt comes out with its tail on top.
    """
    tree = ast.parse(_donor_source())
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "EXTRACTION_SYSTEM_PROMPT"):
            continue
        parts = []
        _collect_str_literals(node.value, parts)
        return "".join(parts)
    pytest.fail("donor has no EXTRACTION_SYSTEM_PROMPT assignment")


def _collect_str_literals(node, out):
    """Append every string literal under `node`, in source order."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    elif isinstance(node, ast.BinOp):
        _collect_str_literals(node.left, out)
        _collect_str_literals(node.right, out)


def _shipped_source():
    return Path(ed.__file__).read_text(encoding="utf-8")


def _identifiers(source):
    """Every name the code actually references.

    An identifier check, not a substring search: the module docstring names
    `TITLE_RULES` and `invoke_with_tools` on purpose (it explains why they are
    gone), so grepping the file text for them can only ever fail.
    """
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[-1])
    return names


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _string_literals(source):
    """Every string literal EXCEPT docstrings -- i.e. the ones that reach the
    model or the network."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in skip}


@pytest.fixture(scope="module")
def donor_source():
    return _donor_source()


@pytest.fixture(scope="module")
def donor_system_prompt():
    return _donor_system_prompt()


class TestDonorDeviations:
    """The three deliberate deviations, pinned in both directions."""

    def test_donor_section_structure_survives_verbatim(self, donor_system_prompt):
        """The क) … च) instruction block is the part that must NOT drift.

        Sliced from the donor's own prompt text at runtime, not transcribed
        here -- a transcription would drift silently alongside the code it is
        supposed to be guarding.
        """
        start = donor_system_prompt.index("### क) अभियोगदावीको सार")
        end = donor_system_prompt.index("QUALITY RULES:")
        donor_block = donor_system_prompt[start:end]
        assert donor_block.strip()
        assert donor_block in ed.EXTRACTION_SYSTEM_PROMPT

    def test_donor_quality_rules_survive_verbatim(self, donor_system_prompt):
        """Every donor QUALITY RULE bullet is still present.

        Checked bullet by bullet rather than as one block, because this port
        INSERTS a dates bullet into the middle of that list -- a whole-block
        substring check would fail on the insertion and tell us nothing about
        whether a donor bullet went missing.
        """
        start = donor_system_prompt.index("QUALITY RULES:")
        end = donor_system_prompt.index("OUTPUT FORMAT")
        donor_rules = donor_system_prompt[start:end]
        # Split on the bullet marker at line starts; keep non-trivial ones.
        bullets = [b.strip() for b in donor_rules.split("\n- ") if len(b.strip()) > 40]
        assert len(bullets) >= 4
        for bullet in bullets[1:]:  # bullets[0] is the "QUALITY RULES:" header
            assert bullet in ed.EXTRACTION_SYSTEM_PROMPT, bullet[:60]

    def test_donor_regenerated_the_title_and_this_port_never_mentions_it(
        self, donor_source, donor_system_prompt
    ):
        # Deviation 1. The donor's own source proves the behaviour existed...
        assert "--skip-title" in donor_source
        assert "TITLE_RULES" in donor_source
        assert 'patch_field(case_slug, "title"' in donor_source
        # ...and none of it survives here, in prompt or code.
        ids = _identifiers(_shipped_source())
        assert "skip_title" not in ids
        assert "TITLE_RULES" not in ids
        assert "validate_title" not in ids
        assert "title_has_headcount" not in ids
        assert "titles" not in {
            n.split(".")[-1] for n in _imported_modules(_shipped_source())
        }, "enrich_description must not import casework.common.titles"
        assert "TITLE RULES" not in ed.EXTRACTION_SYSTEM_PROMPT
        assert '"title"' not in ed.EXTRACTION_SYSTEM_PROMPT

    def test_donor_used_a_tool_loop_and_this_port_uses_plain_invoke_text(
        self, donor_source
    ):
        # Deviation 2. The cache cannot key a multi-turn tool loop
        # (casework/common/llm_cache.py), so the port's most expensive call
        # would be the one call never served from disk.
        assert "invoke_with_tools" in donor_source
        assert "convert_date_tool" in donor_source
        ids = _identifiers(_shipped_source())
        assert "invoke_with_tools" not in ids
        assert "convert_date_tool" not in ids
        assert "tools" not in ids

    def test_dropping_the_tool_is_paired_with_a_no_self_conversion_rule(
        self, donor_system_prompt
    ):
        """The safety half of deviation 2.

        Removing the date tool without forbidding mental conversion would
        invite the BS<->AD arithmetic error the tool existed to prevent, so the
        rule is not optional decoration -- it is the reason the removal is safe.
        """
        assert "DATES:" in ed.EXTRACTION_SYSTEM_PROMPT
        assert "Do NOT convert between BS" in ed.EXTRACTION_SYSTEM_PROMPT
        # And it is genuinely an addition, not something the donor already had.
        assert "Do NOT convert between BS" not in donor_system_prompt

    def test_donor_ngm_fetch_is_not_ported(self, donor_source):
        # Deviation 3: the endpoint was removed in the 2026-07-01 cut and the
        # colon-prefixed ref it needs matches 0 of 109 real court_cases
        # entries (measured in casework/enrich_timeline.py).
        assert "/ngm/court_case/" in donor_source
        assert not [s for s in _string_literals(_shipped_source())
                    if "/ngm/court_case/" in s]
        assert "{ngm_section}" not in ed.EXTRACTION_USER_PROMPT

    def test_source_budget_matches_the_donor_default(self, donor_source):
        # Donor: SOURCE_TEXT_BUDGET = env_int("CASEWORK_SOURCE_TEXT_BUDGET", 60000)
        assert 'env_int("CASEWORK_SOURCE_TEXT_BUDGET", 60000)' in donor_source
        assert ed.SOURCE_TEXT_BUDGET == 60000

    def test_substantial_threshold_matches_the_donor(self, donor_source):
        assert ">= 600" in donor_source
        assert ed.SUBSTANTIAL_DESCRIPTION_CHARS == 600

    def test_max_tokens_matches_the_donor(self, donor_source):
        assert "max_tokens=8000" in donor_source
        assert ed.DESCRIPTION_MAX_TOKENS == 8000


def _imported_modules(source):
    """Every module name imported by `source`, for the "must not import" pin."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


# --------------------------------------------------------------------------
# stage + tier registration
# --------------------------------------------------------------------------


def test_stage_is_registered_with_both_material_families():
    from casework.common.pipeline import COURT_TYPES, PRESS_TYPES, STAGES

    stage = STAGES["description"]
    assert set(stage.requires_materials) == set(PRESS_TYPES + COURT_TYPES)
    assert stage.requires_stages == ("convert",)


def test_stage_provides_description_only():
    """`title` must NOT appear in `provides`.

    `provides` feeds "already enriched, skip it" idempotency checks, so
    naming a field this stage never writes makes a case look title-complete
    that this stage never touched -- the phantom-entry defect documented on
    STAGES["allegations"].
    """
    from casework.common.pipeline import STAGES

    assert STAGES["description"].provides == ("description",)


def test_tier_is_premium():
    from casework.common.llm import tier_for

    assert tier_for("description") == "premium"


# --------------------------------------------------------------------------
# _parse_description_response
# --------------------------------------------------------------------------


class TestParseDescriptionResponse:
    def test_parses_the_object(self):
        body = json.dumps({"description": "### क) अभियोगदावीको सार\nविवरण।"})
        assert _parse_description_response(body) == "### क) अभियोगदावीको सार\nविवरण।"

    def test_a_volunteered_title_key_is_ignored_not_returned(self):
        """The single-owner rule has to hold against a chatty model.

        The OUTPUT FORMAT block no longer asks for a title, but models
        volunteer keys. Returning only the string is what makes it impossible
        for a stray `"title"` to reach a PATCH.
        """
        body = json.dumps({"title": "नयाँ शीर्षक (081-CR-0091)", "description": "विवरण।"})
        assert _parse_description_response(body) == "विवरण।"

    def test_fenced_json_is_parsed(self):
        body = 'यहाँ छ:\n```json\n{"description": "विवरण।"}\n```\n'
        assert _parse_description_response(body) == "विवरण।"

    def test_a_leading_unrelated_object_does_not_stop_the_scan(self):
        """Donor behaviour: every `{` is tried, not just the first.

        A reply that opens with an unrelated object (a preamble, an echoed
        tool argument) returns None under a `text.find("{")` parser.
        """
        body = '{"note": "thinking"} then: {"description": "असली विवरण।"}'
        assert _parse_description_response(body) == "असली विवरण।"

    def test_returns_none_when_the_key_is_absent(self):
        assert _parse_description_response('{"other": "value"}') is None

    def test_returns_none_for_a_blank_description(self):
        assert _parse_description_response(json.dumps({"description": "   "})) is None

    def test_returns_none_for_unparseable_text(self):
        assert _parse_description_response("not json at all") is None

    def test_returns_none_for_empty_input(self):
        assert _parse_description_response("") is None


def test_prompt_context_comes_from_the_shared_formatters(donor_source):
    """The donor imported `format_bigo` / `format_list` / `format_entities` from
    the shared `casework/common.py` -- the very same functions `enrich_card` and
    `enrich_title` used. A private per-enricher copy is a fork, and this one
    forked wrong: the first draft of this port read `role` / `entity_iri` /
    `name` off each entity, none of which exist on the live payload
    (`{nes_id, display_name, entity_type, type, outcome, notes}`, built by
    `nes_resolver.build_entity_binds`). Every entity rendered as a blank bullet
    and the model lost every name in the case, with no test failing.
    """
    for name in ("format_bigo", "format_list", "format_entities"):
        assert name in donor_source, f"donor must import {name}"
    imported = _imported_modules(_shipped_source())
    assert "casework.common.format" in imported
    ids = _identifiers(_shipped_source())
    for private in ("_format_bigo", "_format_list", "_format_entities"):
        assert private not in ids, f"{private} must not be a private copy"


# --------------------------------------------------------------------------
# _ordered_sources
# --------------------------------------------------------------------------


class TestOrderedSources:
    def test_charge_sheet_comes_before_press_release_and_verdict(self):
        chunks = [
            ("court_order", "iri-c", "फैसला"),
            ("press_release", "iri-p", "विज्ञप्ति"),
            ("charge_sheet", "iri-a", "अभियोगपत्र"),
        ]
        assert [t for t, _, _ in _ordered_sources(chunks)] == [
            "charge_sheet", "press_release", "court_order"]

    def test_an_unknown_type_is_kept_at_the_end_not_dropped(self):
        """An unexpected material type is still evidence.

        Dropping it would silently shrink the factual basis of a public
        narrative, which is the failure mode this whole port guards against.
        """
        chunks = [("mystery_type", "iri-m", "अज्ञात"), ("charge_sheet", "iri-a", "अ")]
        ordered = _ordered_sources(chunks)
        assert [t for t, _, _ in ordered] == ["charge_sheet", "mystery_type"]
        assert len(ordered) == 2

    def test_order_is_stable_within_one_type(self):
        chunks = [("press_release", "iri-1", "एक"), ("press_release", "iri-2", "दुई")]
        assert [i for _, i, _ in _ordered_sources(chunks)] == ["iri-1", "iri-2"]


# --------------------------------------------------------------------------
# _assemble_source_text
# --------------------------------------------------------------------------


class TestAssembleSourceText:
    def test_a_long_verdict_is_summarised_not_head_truncated(self):
        """A फैसला's ठहर sits at the END, so a head clamp drops the outcome
        that section ग exists to report."""
        long_verdict = "फ" * (ed.VERDICT_SUMMARY_TRIGGER + 500)
        calls = []

        def stub(**kw):
            calls.append(kw)
            return "फैसलाको सारांश: प्रतिवादी दोषी ठहर।"

        block, fed = _assemble_source_text(
            [("court_order", "iri-c", long_verdict)], stub, usage=None)
        assert calls, "the summariser must have been called"
        assert "फैसलाको सारांश" in block
        assert "फैसला सारांश" in fed[0][0]  # the label says it was summarised
        assert len(fed[0][2]) < len(long_verdict)

    def test_a_failed_summariser_falls_back_to_the_donor_head_clamp(self):
        long_verdict = "फ" * (ed.VERDICT_SUMMARY_TRIGGER + 500)

        block, fed = _assemble_source_text(
            [("court_order", "iri-c", long_verdict)],
            lambda **kw: "",  # falsy -> summarize_verdict returns None
            usage=None,
        )
        assert len(fed[0][2]) == ed.VERDICT_SUMMARY_TARGET
        assert fed[0][0] == "court_order"  # not labelled as a summary
        assert block

    def test_a_short_verdict_passes_through_whole(self):
        short = "छोटो फैसला।"
        calls = []

        def stub(**kw):
            calls.append(kw)
            return "should not be called"

        _, fed = _assemble_source_text(
            [("court_order", "iri-c", short)], stub, usage=None)
        assert calls == []
        assert fed == [("court_order", "iri-c", short)]

    def test_the_budget_caps_what_is_fed_and_fed_reflects_it(self, monkeypatch):
        """`fed` is what the review file prints, so it must be the
        post-truncation reality, not what was fetched."""
        monkeypatch.setattr(ed, "SOURCE_TEXT_BUDGET", 100)
        _, fed = _assemble_source_text(
            [("charge_sheet", "iri-a", "अ" * 500)], lambda **kw: "", usage=None)
        assert len(fed[0][2]) == 100

    def test_a_source_beyond_the_budget_is_dropped_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(ed, "SOURCE_TEXT_BUDGET", 10)
        with caplog.at_level(logging.WARNING, logger="casework.enrich_description"):
            _, fed = _assemble_source_text(
                [("charge_sheet", "iri-a", "अ" * 10),
                 ("press_release", "iri-p", "प" * 10)],
                lambda **kw: "", usage=None,
            )
        assert [t for t, _, _ in fed] == ["charge_sheet"]
        assert "budget spent" in caplog.text


# --------------------------------------------------------------------------
# _generate_description -- tier / max_tokens / prompt-content pins
# --------------------------------------------------------------------------


DETAIL_FOR_PROMPT = {
    "slug": "case-081-cr-0091",
    "title": "काठमाडौं महानगरपालिका ठेक्का अनियमितता",
    "bigo": 10403941,
    "court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0091"],
    "key_allegations": ["ठेक्कामा मिलेमतो गरी सार्वजनिक सम्पत्ति हानि पुर्‍याएको।"],
    "timeline": [{"date": "2024-05-01", "date_bs": "2081-01-19", "title": "उजुरी दर्ता"}],
    # The LIVE entity payload shape: nes_id / display_name / entity_type /
    # type (the RELATIONSHIP type) / outcome / notes, per
    # cases/services/nes_resolver.py::build_entity_binds. There is no `role`
    # and no `entity_iri` key on a case's entities.
    "entities": [
        {"nes_id": "person/kamal-raj-gautam", "display_name": "कमल राज गौतम",
         "entity_type": "person", "type": "accused", "outcome": "",
         "notes": "तत्कालीन प्रमुख"},
    ],
}


def test_generate_uses_premium_tier_and_8000_max_tokens():
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"description": "विवरण।"})

    result = _generate_description(
        detail=DETAIL_FOR_PROMPT, court_number="081-cr-0091",
        source_text="स्रोत पाठ", invoke_text=stub, usage=None,
    )
    assert result == "विवरण।"
    assert seen["tier"] == "premium"
    assert seen["max_tokens"] == 8000
    assert seen["system"] == ed.EXTRACTION_SYSTEM_PROMPT
    # No tool loop -- deviation 2. `tools=` would make the call uncacheable.
    assert "tools" not in seen


def test_generate_prompt_carries_every_context_block():
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"description": "विवरण।"})

    _generate_description(
        detail=DETAIL_FOR_PROMPT, court_number="081-cr-0091",
        source_text="अभियोगपत्रको पाठ", invoke_text=stub, usage=None,
    )
    content = seen["content"]
    assert "काठमाडौं महानगरपालिका ठेक्का अनियमितता" in content
    assert "10,403,941" in content
    assert "081-cr-0091" in content
    assert "ठेक्कामा मिलेमतो" in content
    assert "[accused] कमल राज गौतम" in content
    assert "तत्कालीन प्रमुख" in content
    assert "अभियोगपत्रको पाठ" in content


def test_generate_prompt_keeps_the_timeline_in_devanagari():
    """`json.dumps(..., ensure_ascii=False)`: a timeline serialised as
    `\\u0909...` is unreadable to the model's Nepali register and to anyone
    reading the review file."""
    seen = {}
    _generate_description(
        detail=DETAIL_FOR_PROMPT, court_number="081-cr-0091", source_text="x",
        invoke_text=lambda **kw: seen.update(kw) or json.dumps({"description": "व।"}),
        usage=None,
    )
    assert "उजुरी दर्ता" in seen["content"]
    assert "\\u0909" not in seen["content"]


# --------------------------------------------------------------------------
# _has_substantial_description
# --------------------------------------------------------------------------


class TestHasSubstantialDescription:
    def test_a_long_description_counts_as_done(self):
        assert _has_substantial_description({"description": "क" * 600})

    def test_one_char_short_of_the_threshold_does_not(self):
        assert not _has_substantial_description({"description": "क" * 599})

    def test_a_template_stub_does_not_count(self):
        """The threshold is content-based on purpose: an emptiness test would
        treat a one-line stub as a finished public narrative."""
        assert not _has_substantial_description(
            {"description": "यो मुद्दाको विवरण अद्यावधिक हुँदैछ।"})

    def test_missing_and_whitespace_are_both_empty(self):
        assert not _has_substantial_description({})
        assert not _has_substantial_description({"description": "   \n  "})


# --------------------------------------------------------------------------
# main() -- integration over a stubbed API + LLM
# --------------------------------------------------------------------------

_PRESS_MD = "https://x/press.md"
_COURT_MD = "https://x/court.md"

CASE_READY = {
    "slug": "case-ready",
    "title": "काठमाडौं महानगरपालिका ठेक्का अनियमितता",
    "state": "DRAFT",
    "bigo": 10403941,
    "description": "",
    "court_cases": ["https://jawafdehi.org/courtcase/special/081-cr-0091"],
    "key_allegations": ["ठेक्कामा मिलेमतो गरेको।"],
    "timeline": [],
    "entities": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/12345",
         "additional_details": "",
         "material": {"material_type": "press_release",
                      "urls": [{"link": _PRESS_MD, "role": "MARKDOWN"}]}},
        {"material_iri": "https://jawafdehi.org/material/court/special.081-cr-0091",
         "additional_details": "",
         "material": {"material_type": "court_order",
                      "urls": [{"link": _COURT_MD, "role": "MARKDOWN"}]}},
    ],
}

CASE_UNCONVERTED = dict(
    CASE_READY, slug="case-unconverted",
    evidence=[{"material_iri": "https://jawafdehi.org/material/ciaa/99",
               "additional_details": "",
               "material": {"material_type": "press_release",
                            "urls": [{"link": "https://x/99.pdf", "role": "RAW"}]}}],
)

CASE_ALREADY = dict(CASE_READY, slug="case-already", description="क" * 900)

# A template stub title plus a stub description: the case a title-writing
# regression would visibly damage.
CASE_STUB_TITLE = dict(
    CASE_READY, slug="case-stub-title",
    title="विशेष अदालत मुद्दा 081-CR-0091",
    description="विवरण अद्यावधिक हुँदैछ।",
)


class _StubApi:
    """Mirrors `CaseworkApi`'s surface for the calls `main()` makes."""

    def __init__(self, cases, etag="W/\"abc123\"", fail_detail_for=()):
        self._cases = {c["slug"]: dict(c) for c in cases}
        self._etag = etag
        self._fail_detail_for = set(fail_detail_for)
        self.patched = []

    def iter_cases(self, params=None, timeout=60):
        yield from self._cases.values()

    def get_case_with_etag(self, slug, timeout=60):
        if slug in self._fail_detail_for:
            raise RuntimeError(f"simulated detail-fetch failure for {slug}")
        return self._cases[slug], self._etag

    def patch_field(self, slug, field, value, timeout=60, if_match=None):
        self.patched.append((slug, field, value, if_match))
        self._cases[slug][field] = value
        return {}


class _FakeUsage:
    def __init__(self):
        self.calls = 0

    def as_dict(self):
        return {"by_provider": []}


@pytest.fixture
def patched_fetch_markdown(monkeypatch):
    import casework.common.materials as m

    def fake_fetch(link, timeout=60):
        return {
            _PRESS_MD: "अख्तियारले काठमाडौं महानगरपालिकाको ठेक्कामा भ्रष्टाचार भएको जनाएको।",
            _COURT_MD: "विशेष अदालतले प्रतिवादीलाई दोषी ठहर गरेको।",
        }.get(link, "")

    monkeypatch.setattr(m, "fetch_markdown", fake_fetch)


def _run_main(monkeypatch, api, invoke_text_stub, argv):
    """Drive `main()` end to end with a stubbed API and LLM.

    `invoke_text` / `UsageAccumulator` are imported INSIDE `main()` (after
    bootstrap), so they are faked through `sys.modules` rather than
    `monkeypatch.setattr(ed, ...)` -- same approach as every sibling test file.
    """
    monkeypatch.setattr(ed, "build_api", lambda args: api)
    monkeypatch.setattr(ed, "bootstrap", lambda *a, **k: None)

    fake_llm_invoke = types.ModuleType("llm.invoke")
    fake_llm_invoke.invoke_text = invoke_text_stub

    fake_llm_usage = types.ModuleType("llm.usage")
    fake_llm_usage.UsageAccumulator = _FakeUsage
    fake_llm_usage.render_usage_table = lambda by_provider, title=None: ""

    monkeypatch.setitem(sys.modules, "llm.invoke", fake_llm_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_llm_usage)

    return ed.main(argv)


def _tracking_stub(description="### क) अभियोगदावीको सार\nठेक्कामा भ्रष्टाचार भएको।"):
    """Records invocations. An "LLM must not be called" assertion has to check
    `stub.calls == []` rather than rely on a raise: `main()` swallows
    exceptions from the generate step as an `error` status, so a raising stub
    would be counted, not surfaced."""
    calls = []

    def stub(**kw):
        calls.append(kw)
        return json.dumps({"description": description})

    stub.calls = calls
    return stub


BASE_ARGV = ["--api-base-url", "http://127.0.0.1:48010"]


def test_unmet_prerequisite_is_recorded_not_silently_skipped(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_UNCONVERTED])
    stub = _tracking_stub()
    report = _run_main(monkeypatch, api, stub, BASE_ARGV + ["--dry-run"])
    assert report.rows[0]["status"] == "unmet"
    assert report.rows[0]["reason"]
    assert stub.calls == []
    assert api.patched == []


def test_an_already_described_case_is_skipped_without_calling_the_llm(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_ALREADY])
    stub = _tracking_stub()
    report = _run_main(monkeypatch, api, stub, BASE_ARGV + ["--dry-run"])
    assert report.rows[0]["status"] == "already"
    assert stub.calls == []
    assert api.patched == []


def test_force_regenerates_an_already_described_case(monkeypatch, patched_fetch_markdown):
    api = _StubApi([CASE_ALREADY])
    report = _run_main(
        monkeypatch, api, _tracking_stub("नयाँ विवरण।"),
        BASE_ARGV + ["--force", "--apply"],
    )
    assert report.rows[0]["status"] == "enriched"
    assert [(s, f, v) for s, f, v, _ in api.patched] == [
        ("case-already", "description", "नयाँ विवरण।")]


def test_dry_run_generates_but_does_not_patch(monkeypatch, patched_fetch_markdown):
    api = _StubApi([CASE_READY])
    stub = _tracking_stub()
    report = _run_main(monkeypatch, api, stub, BASE_ARGV + ["--dry-run"])
    assert report.rows[0]["status"] == "would-enrich"
    assert api.patched == []
    assert stub.calls, "a dry run still makes the LLM call -- that is why it bills"


def test_apply_patches_description_with_the_etag_as_if_match(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_READY], etag='W/"etag-42"')
    report = _run_main(
        monkeypatch, api, _tracking_stub("विवरण।"), BASE_ARGV + ["--apply"])
    assert report.rows[0]["status"] == "enriched"
    assert api.patched == [("case-ready", "description", "विवरण।", 'W/"etag-42"')]


def test_never_writes_title(monkeypatch, patched_fetch_markdown):
    """The single-owner rule, end to end.

    The model is made to volunteer a title (as the donor's prompt asked it to)
    over a case whose title is a template stub -- exactly the case a
    title-writing regression would damage. The stub title must come back
    byte-identical and `title` must never appear in a PATCH.
    """
    before_title = CASE_STUB_TITLE["title"]

    def stub(**kw):
        return json.dumps({
            "title": "काठमाडौं ठेक्का घोटाला: भ्रष्टाचार मुद्दा (081-CR-0091)",
            "description": "### क) अभियोगदावीको सार\nविवरण।",
        })

    api = _StubApi([CASE_STUB_TITLE])
    report = _run_main(monkeypatch, api, stub, BASE_ARGV + ["--force", "--apply"])

    assert report.rows[0]["status"] == "enriched"
    assert {field for _, field, _, _ in api.patched} == {"description"}
    assert api._cases["case-stub-title"]["title"] == before_title


def test_llm_returning_no_description_is_skipped_not_enriched(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_READY])
    report = _run_main(
        monkeypatch, api, lambda **kw: json.dumps({"other": "no description key"}),
        BASE_ARGV + ["--dry-run"],
    )
    assert report.rows[0]["status"] == "skipped"
    assert api.patched == []


def test_a_malformed_llm_response_is_skipped_not_written(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_READY])
    report = _run_main(
        monkeypatch, api, lambda **kw: "sorry, I can't help with that",
        BASE_ARGV + ["--apply"],
    )
    assert report.rows[0]["status"] == "skipped"
    assert api.patched == []


def test_an_llm_exception_is_recorded_as_error_not_a_crash(
    monkeypatch, patched_fetch_markdown
):
    def boom(**kw):
        raise RuntimeError("provider exploded")

    api = _StubApi([CASE_READY])
    report = _run_main(monkeypatch, api, boom, BASE_ARGV + ["--apply"])
    assert report.rows[0]["status"] == "error"
    assert "provider exploded" in report.rows[0]["reason"]
    assert api.patched == []


def test_a_detail_fetch_failure_falls_back_and_reports_unmet(
    monkeypatch, patched_fetch_markdown
):
    """Donor-preserved: a failed detail fetch does not abort the case.

    The LIST-shaped fallback never resolves `material`, so it must surface as
    a well-formed unmet reason -- never a crash, never a fabricated success.
    """
    list_shaped = dict(CASE_READY, evidence=[
        {"material_iri": "https://jawafdehi.org/material/ciaa/12345",
         "material": None}])
    api = _StubApi([list_shaped], fail_detail_for=["case-ready"])
    stub = _tracking_stub()
    report = _run_main(monkeypatch, api, stub, BASE_ARGV + ["--dry-run"])
    assert {r["status"] for r in report.rows} == {"unmet"}
    assert any("UNRESOLVED" in r["reason"] for r in report.rows)
    assert stub.calls == []


# --------------------------------------------------------------------------
# the review file -- the accuracy deliverable
# --------------------------------------------------------------------------


def _review_file(tmp_path):
    files = sorted((tmp_path / "reviews").glob("*.md"))
    assert files, "every run must write exactly one review file"
    return files[-1]


def test_a_dry_run_writes_a_review_file(monkeypatch, patched_fetch_markdown, tmp_path):
    """The dry run is the read-only path, so it is where accuracy is judged.
    A review file that only appeared on --apply would mean output could never
    be checked without first writing it somewhere."""
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, _tracking_stub("### क) सार\nठेक्का विवरण।"),
              BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "DRY RUN" in text
    assert "case-ready" in text


def test_the_review_file_carries_before_generated_and_the_source_iri(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    api = _StubApi([CASE_STUB_TITLE])
    _run_main(monkeypatch, api, _tracking_stub("नयाँ लामो विवरण।"),
              BASE_ARGV + ["--force", "--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "विवरण अद्यावधिक हुँदैछ।" in text          # before
    assert "नयाँ लामो विवरण।" in text                  # generated
    assert "https://jawafdehi.org/material/ciaa/12345" in text   # source IRI
    assert "अख्तियारले काठमाडौं" in text               # the passage fed to the model


def test_the_review_file_keeps_devanagari_unescaped(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, _tracking_stub("देवनागरी विवरण।"),
              BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "देवनागरी विवरण।" in text
    assert "\\u0926" not in text


def test_unmet_and_already_cases_still_get_a_row(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    """One row per case means the file is a complete account of the run.
    A case missing from it reads as "not selected", not "could not run"."""
    api = _StubApi([CASE_UNCONVERTED, CASE_ALREADY])
    _run_main(monkeypatch, api, _tracking_stub(), BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "case-unconverted" in text
    assert "case-already" in text
    assert "unmet" in text
    assert "already" in text


def test_the_review_file_labels_its_excerpts_as_fed_not_quoted(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    """A completion does not report which sentences it drew on. Calling the
    excerpt "the passage the model quoted" would be a fabricated provenance
    claim in the one artefact whose job is checking for fabrication."""
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, _tracking_stub(), BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "Sources fed to the model" in text
    assert "FED" in text


def test_a_run_selecting_nothing_still_writes_a_review_file(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    api = _StubApi([])
    _run_main(monkeypatch, api, _tracking_stub(), BASE_ARGV + ["--dry-run"])
    assert _review_file(tmp_path).read_text(encoding="utf-8")


def test_review_file_flag_overrides_the_default_location(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    """`--review-file` is how a run drops its file into the meta-repo task
    directory the work belongs to."""
    target = tmp_path / "task-dir" / "description-review.md"
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, _tracking_stub("विवरण।"),
              BASE_ARGV + ["--dry-run", "--review-file", str(target)])
    assert "विवरण।" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# guard wiring
# --------------------------------------------------------------------------


def test_dry_run_is_the_default_and_apply_opts_in():
    import argparse

    from casework.common.cli import add_common_args

    ap = add_common_args(argparse.ArgumentParser())
    assert ap.parse_args([]).dry_run is True
    assert ap.parse_args(["--apply"]).dry_run is False


def test_build_api_refuses_a_remote_write_by_default(monkeypatch):
    """`CaseworkApi` must still refuse a PATCH to production. This port does
    nothing to that guard and must not be able to."""
    monkeypatch.setenv("CASEWORK_API_USER", "caseworker")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "caseworker")

    class _Args:
        api_base_url = "https://api.jawafdehi.org"
        api_token = "tok"
        allow_remote_writes = False

    api = ed.build_api(_Args())
    with pytest.raises(RuntimeError, match="refusing to write"):
        api.patch_field("case-ready", "description", "विवरण।")
