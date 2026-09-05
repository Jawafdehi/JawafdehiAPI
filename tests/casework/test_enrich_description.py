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
from casework.common import missing_details as md
from casework.enrich_description import (
    _allocate_budget,
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
        # Deviation 2. The donor advertised the tool nowhere in its prompt, and
        # a tool loop bills every turn -- on the pipeline's most expensive call.
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

    def test_max_tokens_defaults_to_the_donor(self, donor_source):
        # The shipped default must stay at parity so the 23 descriptions already
        # written from it keep reproducing. The env escape is covered by
        # TestMaxTokensConfig.
        assert "max_tokens=8000" in donor_source
        assert ed.DESCRIPTION_MAX_TOKENS == 8000
        assert ed.DONOR_MAX_TOKENS == 8000


class TestMaxTokensConfig:
    """`CASEWORK_DESCRIPTION_MAX_TOKENS` is an operator knob, so it validates.

    The typo that matters is one extra zero: `160000` would reach the model and
    fail every description request after ~10 minutes of work per case.
    """

    VAR = "CASEWORK_DESCRIPTION_MAX_TOKENS"

    def test_an_unset_variable_gives_the_donor_default(self, monkeypatch):
        monkeypatch.delenv(self.VAR, raising=False)
        assert ed._max_tokens_from_env() == ed.DONOR_MAX_TOKENS

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_a_blank_variable_is_treated_as_unset(self, monkeypatch, raw):
        monkeypatch.setenv(self.VAR, raw)
        assert ed._max_tokens_from_env() == ed.DONOR_MAX_TOKENS

    @pytest.mark.parametrize("raw,expected",
                             [("12000", 12000), ("1", 1), ("64000", 64000),
                              (" 16000 ", 16000)])
    def test_a_usable_value_is_taken_including_both_boundaries(
            self, monkeypatch, raw, expected):
        monkeypatch.setenv(self.VAR, raw)
        assert ed._max_tokens_from_env() == expected

    @pytest.mark.parametrize("raw", ["0", "-1", "64001", "160000"])
    def test_a_value_the_model_would_refuse_is_rejected(self, monkeypatch, raw):
        monkeypatch.setenv(self.VAR, raw)
        with pytest.raises(SystemExit) as exc:
            ed._max_tokens_from_env()
        assert self.VAR in str(exc.value)

    @pytest.mark.parametrize("raw", ["16k", "16.5", "abc", "1e4"])
    def test_a_non_integer_exits_cleanly_instead_of_raising_valueerror(
            self, monkeypatch, raw):
        # SystemExit, not ValueError: an operator typo should print a message, not a
        # traceback. Matches `casework.common.select`'s handling of bad --batch-csv.
        monkeypatch.setenv(self.VAR, raw)
        with pytest.raises(SystemExit) as exc:
            ed._max_tokens_from_env()
        assert self.VAR in str(exc.value) and raw in str(exc.value)

    def test_an_unset_variable_warns_that_the_default_is_now_short(
            self, monkeypatch, caplog):
        # The ORDER HEADER block makes the reply ~40% longer and 079-CR-0116
        # failed outright on 8000. Nothing truncates silently -- the reply comes
        # back as unbalanced JSON and the case is recorded `skipped` -- so the
        # cost is a wasted premium call per case, discovered at the END of a run.
        monkeypatch.delenv(self.VAR, raising=False)
        with caplog.at_level(logging.WARNING, logger="casework.enrich_description"):
            assert ed._max_tokens_from_env() == ed.DONOR_MAX_TOKENS
        assert self.VAR in caplog.text
        assert "24000" in caplog.text, "the warning must name a value that worked"

    def test_a_set_variable_warns_about_nothing(self, monkeypatch, caplog):
        monkeypatch.setenv(self.VAR, "24000")
        with caplog.at_level(logging.WARNING, logger="casework.enrich_description"):
            assert ed._max_tokens_from_env() == 24000
        assert caplog.text == ""

    def test_the_ceiling_is_the_one_the_cli_reports(self):
        assert ed.MODEL_MAX_OUTPUT_TOKENS == 64000

    def test_the_env_is_never_read_at_import_time(self):
        # Regression: resolving this at module scope made a typo take out anything
        # that merely IMPORTS the module -- pytest reported INTERNALERROR and
        # `--help` printed the config error instead of help. `main` resolves it.
        # Only top-level statements that are not defs -- walking a FunctionDef
        # descends into it and would flag the legitimate call inside `main`.
        module_scope = [
            call for node in ast.parse(_shipped_source()).body
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef))
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and getattr(call.func, "id", "") == "_max_tokens_from_env"
        ]
        assert not module_scope, (
            "_max_tokens_from_env() must not be called at module scope")


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


def test_stage_provides_description_and_missing_details_but_never_title():
    """`title` must NOT appear in `provides`; `missing_details` must.

    `provides` feeds "already enriched, skip it" idempotency checks, so naming a
    field this stage never writes makes a case look complete that this stage
    never touched -- the phantom-entry defect documented on STAGES["allegations"].
    `title` stays out because `enrich_card` owns it.

    `missing_details` is in because this stage really does write it, from one
    extra key in the same generate call. See the conditional caveat below.
    """
    from casework.common.pipeline import STAGES

    assert STAGES["description"].provides == ("description", "missing_details")


def test_provides_is_conditional_for_missing_details():
    """`missing_details` is the one CONDITIONAL entry in the STAGES table.

    This stage's own gate is any-of(press, court), but `missing_details` needs the
    COURT ORDER specifically, so a press-release-only case gets a description and
    no missing_details. An idempotency check that required BOTH fields before
    calling the stage done would therefore loop forever on press-only cases.

    Pinned as behaviour, not just a comment: `build` must return None on exactly
    that shape.
    """
    from casework.common.missing_details import build

    press_only = {
        "court_cases": [],
        "evidence": [{"material_iri": "x",
                      "material": {"material_type": "ciaa_press_release"}}],
    }
    assert build(press_only) is None
    assert build(press_only, ["साक्षीहरूको वकपत्र"]) is None, (
        "no verdict means no missing_details, even with accepted model items")


def test_tier_is_premium():
    from casework.common.llm import tier_for

    assert tier_for("description") == "premium"


# --------------------------------------------------------------------------
# _parse_description_response
# --------------------------------------------------------------------------


class TestParseDescriptionResponse:
    """`_parse_description_response` returns `(description, documents)`.

    `documents` is ALWAYS a list -- never None -- so callers can iterate without
    a guard. `description` stays the required key for the object scan: a reply
    carrying only a document list is not a usable answer.
    """

    def test_parses_the_object(self):
        body = json.dumps({"description": "### क) अभियोगदावीको सार\nविवरण।"})
        assert _parse_description_response(body) == (
            "### क) अभियोगदावीको सार\nविवरण।", [])

    def test_a_volunteered_title_key_is_ignored_not_returned(self):
        """The single-owner rule has to hold against a chatty model.

        The OUTPUT FORMAT block no longer asks for a title, but models
        volunteer keys. Never returning it is what makes it impossible for a
        stray `"title"` to reach a PATCH.
        """
        body = json.dumps({"title": "नयाँ शीर्षक (081-CR-0091)", "description": "विवरण।"})
        assert _parse_description_response(body) == ("विवरण।", [])

    def test_fenced_json_is_parsed(self):
        body = 'यहाँ छ:\n```json\n{"description": "विवरण।"}\n```\n'
        assert _parse_description_response(body) == ("विवरण।", [])

    def test_a_leading_unrelated_object_does_not_stop_the_scan(self):
        """Donor behaviour: every `{` is tried, not just the first.

        A reply that opens with an unrelated object (a preamble, an echoed
        tool argument) returns None under a `text.find("{")` parser.
        """
        body = '{"note": "thinking"} then: {"description": "असली विवरण।"}'
        assert _parse_description_response(body) == ("असली विवरण।", [])

    def test_returns_none_when_the_key_is_absent(self):
        assert _parse_description_response('{"other": "value"}') == (None, [])

    def test_returns_none_for_a_blank_description(self):
        assert _parse_description_response(
            json.dumps({"description": "   "})) == (None, [])

    def test_returns_none_for_unparseable_text(self):
        assert _parse_description_response("not json at all") == (None, [])

    def test_returns_none_for_empty_input(self):
        assert _parse_description_response("") == (None, [])

    def test_missing_documents_are_returned(self):
        body = json.dumps({
            "description": "विवरण।",
            "missing_documents": ["३ चरणका ठेक्का सम्झौताका प्रतिलिपि", "साक्षीहरूको वकपत्र"],
        })
        assert _parse_description_response(body) == (
            "विवरण।", ["३ चरणका ठेक्का सम्झौताका प्रतिलिपि", "साक्षीहरूको वकपत्र"])

    def test_a_bare_string_is_accepted_as_one_document(self):
        """A formatting slip, not a wrong answer -- rejecting it would throw away
        a correct finding over punctuation."""
        body = json.dumps({"description": "विवरण।", "missing_documents": "मूल सम्झौता (2011)"})
        assert _parse_description_response(body) == ("विवरण।", ["मूल सम्झौता (2011)"])

    def test_a_quoted_empty_list_is_no_documents(self):
        body = json.dumps({"description": "विवरण।", "missing_documents": "[]"})
        assert _parse_description_response(body) == ("विवरण।", [])

    def test_enumerators_the_model_added_are_stripped(self):
        """The prompt asks for plain phrases; models number them anyway. Leaving
        the marks in would double-enumerate once `render` adds its own."""
        body = json.dumps({
            "description": "विवरण।",
            "missing_documents": ["क) साक्षीहरूको वकपत्र", "- मूल सम्झौता", "२. लेखापरीक्षण प्रतिवेदन"],
        })
        _, docs = _parse_description_response(body)
        assert docs == ["साक्षीहरूको वकपत्र", "मूल सम्झौता", "लेखापरीक्षण प्रतिवेदन"]

    def test_a_newline_joined_blob_is_split(self):
        body = json.dumps({
            "description": "विवरण।",
            "missing_documents": ["साक्षीहरूको वकपत्र\nमूल सम्झौता (2011)"],
        })
        _, docs = _parse_description_response(body)
        assert docs == ["साक्षीहरूको वकपत्र", "मूल सम्झौता (2011)"]

    def test_a_non_list_non_string_shape_is_ignored(self):
        body = json.dumps({"description": "विवरण।", "missing_documents": {"a": 1}})
        assert _parse_description_response(body) == ("विवरण।", [])

    @pytest.mark.parametrize("sentinel", ["कुनै छैन", "कुनै पनि छैन", "[]", "N/A",
                                          "none", "-", "छैन।"])
    def test_a_nothing_sentinel_INSIDE_a_list_is_not_an_item(self, sentinel):
        """The sentinel test used to run only on a bare string. A model answering
        `["कुनै छैन"]` means "nothing" -- treated as an item it publishes the word
        "none" as a missing document, and no content rule in `reject_item` catches
        it."""
        body = json.dumps({"description": "विवरण।", "missing_documents": [sentinel]})
        assert _parse_description_response(body) == ("विवरण।", [])

    def test_a_sentinel_mixed_with_real_findings_drops_only_the_sentinel(self):
        body = json.dumps({"description": "विवरण।",
                           "missing_documents": ["साक्षीहरूको वकपत्र", "कुनै छैन"]})
        assert _parse_description_response(body) == ("विवरण।", ["साक्षीहरूको वकपत्र"])


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

    def test_a_tight_budget_now_truncates_both_sources_instead_of_dropping_one(
        self, monkeypatch
    ):
        """Changed contract. The greedy version gave the whole budget to the
        first source in PRIORITY order and dropped the rest; on `081-CR-0138`
        that dropped the case's only press release so a charge sheet could take
        all 60,000. Both sources now appear, each truncated to a fair share."""
        monkeypatch.setattr(ed, "SOURCE_TEXT_BUDGET", 10)
        _, fed = _assemble_source_text(
            [("charge_sheet", "iri-a", "अ" * 10),
             ("press_release", "iri-p", "प" * 10)],
            lambda **kw: "", usage=None,
        )
        assert [t for t, _, _ in fed] == ["charge_sheet", "press_release"]
        assert [len(text) for _, _, text in fed] == [5, 5]

    def test_a_source_with_no_allowance_left_is_dropped_with_a_warning(
        self, monkeypatch, caplog
    ):
        """Still reachable, but only when there are more sources than budget."""
        monkeypatch.setattr(ed, "SOURCE_TEXT_BUDGET", 1)
        with caplog.at_level(logging.WARNING, logger="casework.enrich_description"):
            _, fed = _assemble_source_text(
                [("charge_sheet", "iri-a", "अ" * 10),
                 ("press_release", "iri-p", "प" * 10)],
                lambda **kw: "", usage=None,
            )
        assert len(fed) == 1
        assert "budget spent" in caplog.text

    def test_a_small_source_is_never_starved_by_a_huge_one(self, monkeypatch):
        """The regression, at the real sizes from `081-CR-0138`: a 529,947-char
        charge sheet used to consume all 60,000 and drop the 4,185-char press
        release -- 7% of the budget, and the most concise account of the case."""
        _, fed = _assemble_source_text(
            [("press_release", "iri-p", "प" * 4185),
             ("charge_sheet", "iri-a", "अ" * 529947)],
            lambda **kw: "", usage=None,
        )
        got = {label: len(text) for label, _, text in fed}
        assert got["press_release"] == 4185, "the small source must arrive whole"
        assert got["charge_sheet"] == 55815
        assert sum(got.values()) == ed.SOURCE_TEXT_BUDGET


class TestAllocateBudget:
    def test_the_small_source_takes_what_it_needs_and_the_rest_flows_up(self):
        assert _allocate_budget([4185, 529947], 60000) == [4185, 55815]

    def test_several_small_sources_all_survive_two_huge_ones(self):
        assert _allocate_budget([500, 800, 1200, 400000, 300000, 2000], 60000) == \
            [500, 800, 1200, 27750, 27750, 2000]

    def test_sources_that_all_fit_are_untouched(self):
        assert _allocate_budget([100, 200, 300], 60000) == [100, 200, 300]

    def test_equal_sources_split_evenly(self):
        assert _allocate_budget([1000, 1000, 1000], 300) == [100, 100, 100]

    def test_no_sources_is_no_allocation(self):
        assert _allocate_budget([], 60000) == []

    def test_the_allocation_never_exceeds_the_budget(self):
        for sizes in ([10] * 7, [1, 999999], [50000, 50000], [0, 100]):
            assert sum(_allocate_budget(sizes, 60000)) <= 60000


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
    assert result == ("विवरण।", [])
    assert seen["tier"] == "premium"
    assert seen["max_tokens"] == 8000
    assert seen["system"] == ed.EXTRACTION_SYSTEM_PROMPT
    # No tool loop -- deviation 2. `tools=` would make the call uncacheable.
    assert "tools" not in seen


def test_generate_honours_a_raised_max_tokens():
    """`main` resolves the env override and threads it through, so the cap has to
    actually reach `invoke_text` -- not just sit in a module constant."""
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"description": "विवरण।"})

    _generate_description(
        detail=DETAIL_FOR_PROMPT, court_number="081-cr-0091",
        source_text="स्रोत पाठ", invoke_text=stub, usage=None,
        max_tokens=16000,
    )
    assert seen["max_tokens"] == 16000


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
    assert "Special-court case number: 081-CR-0091" in content, (
        "the number is presented UPPERCASE. Asserting the bare lowercase string "
        "instead passed for the wrong reason -- it also appears in the raw "
        "court_cases IRI block, so the assertion held either way")
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

# --------------------------------------------------------------------------
# The 2026-08-13 review rules (work/slug-fix/enricher-fix-rules.json)
# --------------------------------------------------------------------------


class TestMaskIdentifiers:
    """`description.mask_account_identifiers` / `..._third_party_...`.

    Every string here is a real one: the three account numbers and the cheque
    number are what the 2026-08-13 hand fix stripped out of production
    (work/slug-fix/desc-fixes.jsonl), and the hyphen-grouped account belongs to
    a NON-ACCUSED land seller in 079-CR-0160, which is still live.

    The rules' own stated test -- "a 9+ digit run" -- misses two of these: the
    cheque number is 8 digits and the grouped account's longest run is 6. So the
    match is anchored on the KEYWORD, never on length.
    """

    def test_an_account_number_keeps_only_its_last_five_digits(self):
        out = ed._mask_identifiers(
            "नबिल बैंक, कान्तिपथ शाखाको खाता नं. ०२०१०१७५०१०३१ मा रु.३५,४८,७८७।– जम्मा गरेको")
        assert "खाता नं. ...०१०३१" in out
        assert "०२०१०१७५०१०३१" not in out

    def test_the_rupee_amount_beside_it_survives(self):
        out = ed._mask_identifiers(
            "खाता नं. ०२०१०१७५०१०३१ मा रु.३५,४८,७८७।– जम्मा गरेको")
        assert "रु.३५,४८,७८७।–" in out

    def test_the_proven_production_form_is_reproduced(self):
        # 079-CR-0116 was patched to exactly this in prod.
        out = ed._mask_identifiers("एभरेष्ट बैंक खाता नं. ०१२००५१६२००४२६ मा जम्मा")
        assert "एभरेष्ट बैंक खाता नं. ...००४२६ मा जम्मा" == out

    def test_a_hyphen_grouped_account_is_masked_too(self):
        # 079-CR-0160, a non-accused third party. Longest digit run is 6, so a
        # length-based rule never fires here.
        out = ed._mask_identifiers(
            "माछापुच्छ्रे बैंकको खाता (१९-३६५२४-१५९३४८-०१-३) मार्फत रकम प्राप्त गरेको")
        assert "१९-३६५२४-१५९३४८-०१-३" not in out
        assert "..." in out
        assert "माछापुच्छ्रे बैंकको" in out

    def test_an_eight_digit_cheque_number_is_masked(self):
        out = ed._mask_identifiers("ग्लोबल आई.एम.ई. चेक नं. २८४९७७४२ बाट भुक्तानी")
        assert "२८४९७७४२" not in out
        assert "चेक नं. ...९७७४२" in out

    def test_latin_digits_are_masked_as_well_as_devanagari(self):
        out = ed._mask_identifiers("खाता नं. 02010175010 मा")
        assert "02010175010" not in out
        assert "...75010" in out

    def test_a_citizenship_number_is_masked(self):
        # The order caption carries these, and the caption is now fed verbatim.
        out = ed._mask_identifiers("(ना.प्र.प.नं. ९१५/२२९१, अर्घाखाँची) को रघुनाथ घिमिरे")
        assert "९१५/२२९१" not in out
        assert "अर्घाखाँची" in out

    def test_a_phone_number_is_masked(self):
        out = ed._mask_identifiers("कौशलेन्द्र मिश्र, सम्पर्क नं. 9851108812")
        assert "9851108812" not in out

    @pytest.mark.parametrize("keep", [
        "च.नं. १९२, मिति २०७९।०९।११ को पत्र",
        "कि.नं. ५४४ को जग्गा",
        "नि.नं. 127 को फैसला",
        "मुद्दा नं. 079-CR-0116",
        "उ.द.नं. ३५२७, मिति २०७२।०८।१६ को उजुरी",
        "विशेष अदालत ऐन, २०५९ को दफा १७ बमोजिम ३५ दिनभित्र",
        "रु.१,२४,१४१।६७ र रु.३०,८१,५४०।७३",
        "बीमा प्रिमियममा रु.४,९४,९८४ खर्च गरेको",
    ])
    def test_a_case_identifier_or_an_amount_is_never_touched(self, keep):
        assert ed._mask_identifiers(keep) == keep

    @pytest.mark.parametrize("keep", [
        "बीमा ४९४९८४ भुक्तानी",
        "जीवन बीमा १०००००० को पोलिसी",
    ])
    def test_a_bare_sum_after_a_keyword_is_not_masked(self, keep):
        # Sums assured, not policy numbers: the keyword is there but nothing says
        # "a reference follows". Destroying the figure breaks the आय–व्यय
        # reconciliation, which is the same defect as leaking the identifier
        # pointing the other way.
        assert ed._mask_identifiers(keep) == keep

    @pytest.mark.parametrize("text,expected", [
        ("खाता नं. ०२०१ ०१७५ ०१०३१ मा जम्मा", "खाता नं. ...०१०३१ मा जम्मा"),
        ("बैंक खाता नं. 0201 0175 01031 मा", "बैंक खाता नं. ...01031 मा"),
    ])
    def test_a_space_grouped_account_number_is_masked(self, text, expected):
        # A bank prints an account grouped by spaces as often as by hyphens, and
        # the hyphen form is already pinned above.
        assert ed._mask_identifiers(text) == expected

    @pytest.mark.parametrize("text", [
        "मोबाइल ९८४११२३४५६ मा सम्पर्क",
        "नागरिकता ९१५२२९१ को",
        "चेक ११९७७४२ बाट",
        "खाता ०१२००५१६२००४२६ मा",
    ])
    def test_a_bare_identifier_is_still_masked(self, text):
        # The anchor demanded of बीमा/पोलिसी must NOT be demanded of the other
        # seven keywords: none of them is ever followed by a rupee figure, so a
        # blanket requirement would trade the destroyed amount for a leak.
        assert "..." in ed._mask_identifiers(text)

    def test_a_separate_number_beside_the_identifier_is_not_absorbed(self):
        # The space-grouped branch must stop at the identifier rather than run on
        # into the year next to it.
        assert (ed._mask_identifiers("खाता नं. ०१२००५१६२००४२६ २०८१ सालमा")
                == "खाता नं. ...००४२६ २०८१ सालमा")

    @pytest.mark.parametrize("text,expected", [
        # 7 digits kept 5, so 5 of the citizenship number plus the issuing
        # district were published under something that reads as masked.
        ("(ना.प्र.प.नं. ९१५/२२९१, अर्घाखाँची)", "(ना.प्र.प.नं. ...२२९१, अर्घाखाँची)"),
        ("चेक नं. १२३४५६", "चेक नं. ...४५६"),
    ])
    def test_a_short_identifier_hides_at_least_three_digits(self, text, expected):
        assert ed._mask_identifiers(text) == expected

    def test_masking_is_idempotent(self):
        once = ed._mask_identifiers("खाता नं. ०१२००५१६२००४२६ मा")
        assert ed._mask_identifiers(once) == once

    def test_every_occurrence_is_masked_not_just_the_first(self):
        out = ed._mask_identifiers(
            "खाता नं. ०२०१०१७५०१०३१ र खाता नं. ०१४०५०५००१२८७६ दुवै")
        assert "०२०१०१७५०१०३१" not in out
        assert "०१४०५०५००१२८७६" not in out

    def test_a_keyword_with_no_number_after_it_is_left_alone(self):
        text = "कर्जा खाताको रकमलाई बैंक मौज्दात मान्न नमिल्ने"
        assert ed._mask_identifiers(text) == text

    def test_a_short_number_after_a_keyword_is_left_alone(self):
        # Not an identifier: masking "खाता २" would be noise, not privacy.
        text = "खाता २ वटा रहेको"
        assert ed._mask_identifiers(text) == text

    def test_the_nepali_word_for_number_is_matched_too(self):
        out = ed._mask_identifiers("बैंक खाता नम्बर ०१२००५१६२००४२६ मा")
        assert "०१२००५१६२००४२६" not in out

    def test_a_colon_between_the_keyword_and_the_number_still_matches(self):
        out = ed._mask_identifiers("बैंक खाता: ०१२००५१६२००४२६")
        assert "०१२००५१६२००४२६" not in out

    def test_the_ellipsis_does_not_run_into_the_abbreviation_dot(self):
        # "खाता नं....००४२६" is four dots and reads as a typo.
        out = ed._mask_identifiers("खाता नं.०१२००५१६२००४२६ मा")
        assert "नं...." not in out
        assert "खाता नं. ...००४२६ मा" == out

    def test_empty_and_none_survive(self):
        assert ed._mask_identifiers("") == ""
        assert ed._mask_identifiers(None) is None


class TestReviewRulePromptCoverage:
    """Each rule that can only live in the prompt, pinned by its own marker so a
    prompt rewrite cannot quietly drop one."""

    def test_the_order_header_block_is_offered_to_the_model(self):
        assert "{order_header}" in ed.EXTRACTION_USER_PROMPT

    def test_section_ga_is_told_to_open_with_the_bench_and_the_numbers(self):
        p = ed.EXTRACTION_SYSTEM_PROMPT
        assert "इजलास नं." in p and "नि.नं." in p and "फैसला मिति" in p

    def test_the_absent_field_claim_is_forbidden_without_checking_the_header(self):
        assert "स्रोत कागजातमा खुल्न आएको छैन" in ed.EXTRACTION_SYSTEM_PROMPT

    def test_the_verdict_date_is_pinned_to_the_header_not_the_nearest_date(self):
        p = ed.EXTRACTION_SYSTEM_PROMPT
        assert "बहसनोट" in p, "the 079-CR-0160 slip was a बहसनोट date read as the verdict"
        assert "इति सम्वत्" in p, "the fallback for an order with no header date"

    def test_the_finality_line_is_specified_with_its_skip(self):
        p = ed.EXTRACTION_SYSTEM_PROMPT
        assert "**अन्तिमता:**" in p
        assert "मतैक्य" in p, "the दफा ६(४) split is the case that must NOT get the line"
        assert "दफा १७" in p

    def test_the_finality_line_states_absence_of_a_record_not_of_an_appeal(self):
        assert "यकिन हुन सकेको छैन" in ed.EXTRACTION_SYSTEM_PROMPT

    def test_named_non_defendants_must_be_marked(self):
        p = ed.EXTRACTION_SYSTEM_PROMPT
        assert "प्रतिवादी होइनन्" in p
        assert "जफत" in p, "spouses are often co-defendants for जफत only (079-CR-0143)"

    def test_an_untested_third_party_allegation_must_be_marked_not_deleted(self):
        # `बयान` and `परीक्षण` were BOTH in the pre-rule prompt already -- the
        # first from the section-ख heading, the second inside `लेखापरीक्षण` in the
        # Good examples -- so asserting on them pinned nothing and the whole rule
        # could have been deleted green. These three are new with the rule.
        p = ed.EXTRACTION_SYSTEM_PROMPT
        assert "जिकिर" in p, "the passage must be attributed to the defendant"
        assert "यस जिकिरको परीक्षण" in p and "देखिँदैन" in p

    def test_personal_identifiers_are_forbidden_and_case_identifiers_are_not(self):
        p = ed.EXTRACTION_SYSTEM_PROMPT
        assert "खाता नं." in p and "ना.प्र.प." in p
        assert "मुद्दा नं." in p and "कि.नं." in p

    def test_the_missing_documents_half_carries_the_identifier_rule_too(self):
        # `missing_details` rides in the SAME conditional PATCH and is just as
        # public, but the rule lived only in the description half -- and this is
        # the output whose whole value is naming a specific record.
        half = ed.EXTRACTION_SYSTEM_PROMPT.split("MISSING DOCUMENTS")[-1]
        assert "खाता नं." in half and "ना.प्र.प." in half


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

# `CASE_READY` holds a press release and a court order, no charge sheet, and a
# `special:` court-case ref only -- so BOTH deterministic floor items fire. That
# is the shape 24 of the 25 cases in the first production batch have.
MD_FLOOR_BOTH = (
    "१. अख्तियार दुरुपयोग अनुसन्धान आयोगले दायर गरेको अभियोगपत्र\n"
    "२. हदम्याद भित्र वादी वा प्रतिवादीले सर्वोच्च अदालतमा पुनरावेदन गरे नगरेको ब्याहोरा"
)

# Same case with a Supreme Court reference on file: the appeal item drops, so the
# floor is one item and renders bare, with no enumerator.
CASE_WITH_APPEAL = dict(
    CASE_READY, slug="case-with-appeal",
    court_cases=["https://jawafdehi.org/courtcase/special/081-cr-0091",
                 "https://jawafdehi.org/courtcase/supreme/081-cr-2319"],
)

# Press release only. `description` still runs (its gate is any-of), but
# `missing_details` must not be written -- there is no verdict to diff against.
CASE_PRESS_ONLY = dict(
    CASE_READY, slug="case-press-only",
    evidence=[CASE_READY["evidence"][0]],
)

# A case the importer already flagged. The truncation marker must survive.
TRUNCATION_MARKER = (
    "ACCUSED LIST INCOMPLETE: 2 defendant(s) imported (NGM parsed 2); court "
    "record states ≈5. Roster truncated at source — rebuild from the court "
    "order before publishing."
)
CASE_FLAGGED = dict(
    CASE_READY, slug="case-flagged", missing_details=TRUNCATION_MARKER)

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
        # One entry per PATCH REQUEST, as `(slug, [field, ...], if_match)` --
        # `patched` is per-field and cannot show that both fields went in a
        # single conditional write.
        self.patch_calls = []

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

    def patch_fields(self, slug, pairs, timeout=60, if_match=None):
        """Record each pair as its own `patched` entry.

        Flattened deliberately: every existing assertion in this file reads
        `api.patched` as `(slug, field, value, if_match)` tuples, and the fused
        write is still ONE conditional request per case. `patch_calls` below
        counts requests for the tests that care about that instead.
        """
        pairs = list(pairs)
        self.patch_calls.append((slug, [f for f, _ in pairs], if_match))
        for field, value in pairs:
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


def test_an_already_described_case_is_skipped_when_BOTH_fields_are_done(
    monkeypatch, patched_fetch_markdown
):
    """The gate is PER FIELD now that the stage writes two."""
    case = dict(CASE_ALREADY)
    case["missing_details"] = MD_FLOOR_BOTH
    api = _StubApi([case])
    stub = _tracking_stub()
    report = _run_main(monkeypatch, api, stub, BASE_ARGV + ["--dry-run"])
    assert report.rows[0]["status"] == "already"
    assert stub.calls == []
    assert api.patched == []


def test_a_described_case_with_an_empty_missing_details_is_still_processed(
    monkeypatch, patched_fetch_markdown
):
    """A description-only gate skipped these before `missing_details` was even
    computed -- so the ~188 production cases that already carry a description could
    never get the new field, while `provides` claimed they were complete. The
    description itself is NOT rewritten: only the empty field is patched."""
    api = _StubApi([CASE_ALREADY])          # substantial description, no missing_details
    _run_main(monkeypatch, api, _stub_with_documents("नयाँ विवरण।", []),
              BASE_ARGV + ["--apply"])
    written = {f: v for _, f, v, _ in api.patched}
    assert "missing_details" in written
    assert "description" not in written, "rewrote a description without --force"


def test_force_regenerates_an_already_described_case(monkeypatch, patched_fetch_markdown):
    api = _StubApi([CASE_ALREADY])
    report = _run_main(
        monkeypatch, api, _tracking_stub("नयाँ विवरण।"),
        BASE_ARGV + ["--force", "--apply"],
    )
    assert report.rows[0]["status"] == "enriched"
    written = {f: v for _, f, v, _ in api.patched}
    assert written["description"] == "नयाँ विवरण।"
    # CASE_ALREADY carries no prior missing_details, so the floor is written
    # outright. That --force does NOT touch a populated value is pinned by
    # test_force_never_touches_an_existing_missing_details.
    assert MD_FLOOR_BOTH in written["missing_details"]


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
    assert ("case-ready", "description", "विवरण।", 'W/"etag-42"') in api.patched
    # BOTH fields go in ONE conditional request. Two `patch_field` calls could
    # not stay conditional -- the second would carry an ETag the first invalidated.
    assert api.patch_calls == [
        ("case-ready", ["description", "missing_details"], 'W/"etag-42"')]


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
    fields = {field for _, field, _, _ in api.patched}
    # Asserted as an exclusion, not an exact set: this test is about `title`
    # never being written, and pinning the exact set makes it fail whenever an
    # unrelated field is legitimately added (as `missing_details` was).
    assert "title" not in fields
    assert fields <= {"description", "missing_details"}
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


def test_batch_csv_reaches_selection(tmp_path):
    """`--batch-csv` selects through the shared `select_for_run` path (#410)."""
    import argparse

    from casework.common.cli import add_common_args

    csv = tmp_path / "batch.csv"
    csv.write_text("slug\ncase-078-cr-0103-x\n", encoding="utf-8")
    args = add_common_args(argparse.ArgumentParser()).parse_args(
        ["--batch-csv", str(csv)])
    assert args.batch_csv == str(csv)
    assert "select_for_run" in _shipped_source()


# --------------------------------------------------------------------------
# code-review findings
# --------------------------------------------------------------------------


def test_a_partly_failed_source_fetch_is_reported_not_swallowed(monkeypatch, tmp_path):
    """Review finding 3. `text_unmet` used to be consumed ONLY when nothing
    fetched at all.

    So a case whose charge sheet fetched and whose court order 500'd generated a
    full public narrative from the prosecution claim alone -- silently omitting
    that the defendant was acquitted -- and the review file listed only the
    source that succeeded. The human reviewer had no way to see that a verdict
    source existed and was lost. This is the one stage where the missing source
    can BE the outcome.
    """
    import casework.common.materials as m

    def half_broken(link, timeout=60):
        if link == _COURT_MD:
            raise RuntimeError("502 Bad Gateway")
        return "अख्तियारले काठमाडौं महानगरपालिकाको ठेक्कामा भ्रष्टाचार भएको जनाएको।"

    monkeypatch.setattr(m, "fetch_markdown", half_broken)
    stub = _tracking_stub("### क) अभियोगदावीको सार\nठेक्कामा भ्रष्टाचार भएको।")
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, stub, BASE_ARGV + ["--dry-run"])

    assert stub.calls, "the surviving source still generates -- this is not a skip"
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "SOURCE MISSING" in text, (
        "the reviewer signs off on this file; a lost verdict source must appear in it")
    assert "court_order" in text
    assert "502 Bad Gateway" in text


def test_the_court_number_reaches_the_model_uppercase(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    """Review finding 6. `select.court_number()` reads the number off the
    canonical IRI, which is lowercase, and the prompt tells the model to prefer
    specifics from the context -- so `मुद्दा 081-cr-0091` lands in public prose.

    Fixed for the card on evidence (2 of 5 titles shipped lowercase in the
    2026-08-04 evaluation, against 50/50 uppercase in PUBLISHED titles); this is
    the same defect one stage earlier.
    """
    stub = _tracking_stub("### क) सार\nठेक्का विवरण।")
    _run_main(monkeypatch, _StubApi([CASE_READY]), stub, BASE_ARGV + ["--dry-run"])
    content = stub.calls[0]["content"]
    assert "Special-court case number: 081-CR-0091" in content
    assert "case number: 081-cr-0091" not in content


def test_a_successful_fetch_with_no_etag_refuses_to_write(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    """Review finding 10, description side. A 200 missing the ETag header writes
    with no precondition -- and a description is the most expensive output in the
    pipeline, so losing someone else's edit to it matters most here."""
    stub = _tracking_stub("### क) सार\nठेक्का विवरण।")
    api = _StubApi([CASE_READY], etag="")
    report = _run_main(monkeypatch, api, stub, BASE_ARGV + ["--apply"])
    assert api.patched == []
    assert stub.calls == [], "and it must not pay for the premium call first"
    assert any("no ETag" in r["reason"] for r in report.rows)


def test_the_donor_fallback_route_still_cannot_write_unconditionally(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    """The fetch-time guard is scoped to a SUCCESSFUL fetch, so it deliberately
    leaves the donor fallback (`detail = case`, `etag = None`) alone -- that path
    is meant to report from the unmet-material gate.

    But the gate only stops it because a LIST payload carries `material: null`,
    which is an accident of the serializer rather than a precondition. So the
    write itself is guarded too. This drives the fallback with a detail-shaped
    payload -- the case the material gate does NOT catch -- and asserts nothing is
    written.
    """
    api = _StubApi([CASE_READY], fail_detail_for=["case-ready"])
    report = _run_main(monkeypatch, api, _tracking_stub("### क) सार\nठेक्का विवरण।"),
                       BASE_ARGV + ["--apply"])
    assert api.patched == []
    assert any("no ETag" in r["reason"] for r in report.rows)


def test_a_dry_run_is_not_blocked_by_a_missing_etag(
    monkeypatch, patched_fetch_markdown, tmp_path
):
    """The write guard sits after the dry-run branch, so the read-only path still
    produces its accuracy artefact. Blocking it would mean a fetch quirk could
    stop output ever being reviewed."""
    api = _StubApi([CASE_READY], fail_detail_for=["case-ready"])
    _run_main(monkeypatch, api, _tracking_stub("### क) सार\nठेक्का विवरण।"),
              BASE_ARGV + ["--dry-run"])
    assert api.patched == []
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "ठेक्का विवरण।" in text


# --------------------------------------------------------------------------
# missing_details: the second field this stage writes
#
# The value is assembled from a DETERMINISTIC floor (what is bound) plus up to
# four specific documents the model found referenced in the sources but absent
# from our evidence. Pure-function rules live in
# tests/casework/test_missing_details.py; these are the end-to-end paths.
# --------------------------------------------------------------------------


def _stub_with_documents(description, documents):
    def stub(**kw):
        return json.dumps(
            {"description": description, "missing_documents": documents})

    return stub


def test_apply_writes_the_floor_when_the_model_finds_nothing(
    monkeypatch, patched_fetch_markdown
):
    """An empty document list is a good answer, not a failure.

    The floor alone is a complete, publishable value -- it is what 5 published
    cases carry -- so the field is still written.
    """
    api = _StubApi([CASE_READY])
    report = _run_main(monkeypatch, api, _stub_with_documents("विवरण।", []),
                       BASE_ARGV + ["--apply"])
    assert report.rows[0]["status"] == "enriched"
    written = {f: v for _, f, v, _ in api.patched}
    assert written["missing_details"] == MD_FLOOR_BOTH


def test_model_documents_are_appended_and_switch_the_enumerator(
    monkeypatch, patched_fetch_markdown
):
    """Two floor items plus two found documents = 4 items, so the enumerator
    moves from Devanagari numerals to Nepali letters. Chosen AFTER the model's
    items are accepted -- never before."""
    api = _StubApi([CASE_READY])
    _run_main(
        monkeypatch, api,
        _stub_with_documents(
            "विवरण।", ["३ चरणका ठेक्का सम्झौताका प्रतिलिपि", "साक्षीहरूको वकपत्र"]),
        BASE_ARGV + ["--apply"],
    )
    value = {f: v for _, f, v, _ in api.patched}["missing_details"]
    assert value.startswith("क) अख्तियार")
    assert "ग) ३ चरणका ठेक्का सम्झौताका प्रतिलिपि" in value
    assert "घ) साक्षीहरूको वकपत्र" in value
    assert "१." not in value, "numerals are for the 2-item shape only"


def test_a_document_we_already_hold_is_rejected(monkeypatch, patched_fetch_markdown):
    """THE CHECKABLE GROUNDING RULE. The model is told what we hold; a claim that
    a held document is missing is contradicted by our own bindings, so it is
    dropped in code rather than trusted."""
    api = _StubApi([CASE_READY])
    _run_main(
        monkeypatch, api,
        _stub_with_documents("विवरण।", ["प्रेस विज्ञप्ति", "साक्षीहरूको वकपत्र"]),
        BASE_ARGV + ["--apply"],
    )
    value = {f: v for _, f, v, _ in api.patched}["missing_details"]
    assert "प्रेस विज्ञप्ति" not in value
    assert "साक्षीहरूको वकपत्र" in value


def test_filler_items_are_rejected(monkeypatch, patched_fetch_markdown):
    """`अन्य आवश्यक स्रोतहरू` appears in 15 published cases and names nothing a
    reader can go and look for. It must not consume an item slot."""
    api = _StubApi([CASE_READY])
    _run_main(
        monkeypatch, api,
        _stub_with_documents(
            "विवरण।", ["अन्य आवश्यक स्रोतहरू।", "थप आधार र प्रमाण पुष्टि गर्ने प्रमाणिक स्रोत"]),
        BASE_ARGV + ["--apply"],
    )
    value = {f: v for _, f, v, _ in api.patched}["missing_details"]
    assert value == MD_FLOOR_BOTH


def test_a_document_restating_the_floor_is_rejected(monkeypatch, patched_fetch_markdown):
    """A copy of a floor document, and bare commentary about the appeal, both go --
    leaving the floor exactly as it was."""
    api = _StubApi([CASE_READY])
    _run_main(
        monkeypatch, api,
        _stub_with_documents("विवरण।", ["अभियोगपत्रको पूर्णपाठ",
                                        "पुनरावेदन भएको वा नभएको ब्यहोरा"]),
        BASE_ARGV + ["--apply"],
    )
    value = {f: v for _, f, v, _ in api.patched}["missing_details"]
    assert value == MD_FLOOR_BOTH


def test_a_supreme_reference_drops_the_appeal_item(monkeypatch, patched_fetch_markdown):
    """One floor item renders BARE, with no enumerator -- the 1-item corpus shape
    (mishra-revenue-leakage-080-cr-0061)."""
    api = _StubApi([CASE_WITH_APPEAL])
    _run_main(monkeypatch, api, _stub_with_documents("विवरण।", []),
              BASE_ARGV + ["--apply"])
    value = {f: v for _, f, v, _ in api.patched}["missing_details"]
    assert value == "अख्तियार दुरुपयोग अनुसन्धान आयोगले दायर गरेको अभियोगपत्र"
    assert "पुनरावेदन" not in value


def test_a_press_only_case_gets_a_description_but_no_missing_details(
    monkeypatch, patched_fetch_markdown
):
    """The two fields have DIFFERENT gates. `description` needs press OR verdict;
    `missing_details` needs the verdict specifically. Reported, not an error."""
    api = _StubApi([CASE_PRESS_ONLY])
    report = _run_main(monkeypatch, api, _stub_with_documents("विवरण।", ["साक्षीको वकपत्र"]),
                       BASE_ARGV + ["--apply"])
    assert report.rows[0]["status"] == "enriched"
    fields = {f for _, f, _, _ in api.patched}
    assert fields == {"description"}


def test_an_existing_missing_details_is_not_overwritten(
    monkeypatch, patched_fetch_markdown
):
    api = _StubApi([CASE_FLAGGED])
    report = _run_main(monkeypatch, api, _stub_with_documents("विवरण।", []),
                       BASE_ARGV + ["--apply"])
    assert report.rows[0]["status"] == "enriched"
    fields = {f for _, f, _, _ in api.patched}
    assert fields == {"description"}, "description still writes; missing_details does not"
    assert api._cases["case-flagged"]["missing_details"] == TRUNCATION_MARKER


def test_force_never_touches_an_existing_missing_details(monkeypatch,
                                                        patched_fetch_markdown):
    """NEVER TOUCH A NON-EMPTY VALUE, not even with --force. The importer's
    truncation guard puts `ACCUSED LIST INCOMPLETE` in this same field, and the 61
    published values are hand-written. The description is still rewritten."""
    api = _StubApi([CASE_FLAGGED])
    _run_main(monkeypatch, api, _stub_with_documents("विवरण।", []),
              BASE_ARGV + ["--force", "--apply"])
    written = {f: v for _, f, v, _ in api.patched}
    assert "missing_details" not in written
    assert written["description"] == "विवरण।"


@pytest.mark.parametrize("second_run_item", [
    None,                       # byte-identical repeat
    "मूल सम्झौता (2011)",       # a DIFFERENT model item -- the case a substring test missed
])
def test_a_repeated_forced_run_never_duplicates_the_floor(
        monkeypatch, patched_fetch_markdown, second_run_item):
    """An earlier fix tested `missing in before_missing`, which only caught the
    byte-identical repeat. The floor is deterministic but the model's items are
    not, so one changed item made the new value a non-substring and it appended
    wholesale -- both floor items twice and a restarted `क)` enumeration, growing
    every run. The floor strings cannot serve as a "we wrote this" signature
    either: they were copied verbatim FROM hand-written published cases."""
    case = dict(CASE_READY)
    case["missing_details"] = f"{TRUNCATION_MARKER}\n\n{MD_FLOOR_BOTH}"
    api = _StubApi([case])
    _run_main(monkeypatch, api,
              _stub_with_documents("विवरण।", [second_run_item] if second_run_item else []),
              BASE_ARGV + ["--force", "--apply"])
    written = {f: v for _, f, v, _ in api.patched}
    assert "missing_details" not in written, "wrote over or appended to a live value"


def test_a_lost_verdict_source_falls_back_to_the_floor(monkeypatch, tmp_path):
    """`has_verdict` is BINDING-based, so a case whose court order failed to fetch
    still reports True and `held_summary` still tells the model we hold the verdict.
    The model is then diffing against an inventory it could not read, so its items
    are dropped and only the deterministic floor -- which is binding-based and
    therefore still true -- is written."""

    def one_source_fails(link):
        if "court" in link:
            raise RuntimeError("500 from the material store")
        return "प्रेस विज्ञप्तिको पाठ"

    monkeypatch.setattr("casework.common.materials.fetch_markdown", one_source_fails)
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api,
              _stub_with_documents("विवरण।", ["साक्षी रामु खनालको वकपत्र"]),
              BASE_ARGV + ["--apply"])
    written = {f: v for _, f, v, _ in api.patched}
    assert written["missing_details"] == MD_FLOOR_BOTH, "used items from an unread verdict"
    events = "\n".join(p.read_text() for p in tmp_path.glob("*.events.jsonl"))
    assert "verdict source was not fetched" in events


def test_no_accepted_documents_are_logged_on_a_case_that_writes_nothing(
        monkeypatch, patched_fetch_markdown, tmp_path):
    """`build` writes nothing without a verdict, so "accepted" would be followed by
    "skipped: no verdict bound". This log exists to tell a prompt problem from a
    sourcing problem, which a contradictory pair defeats."""
    api = _StubApi([CASE_PRESS_ONLY])
    _run_main(monkeypatch, api, _stub_with_documents("विवरण।", ["साक्षीहरूको वकपत्र"]),
              BASE_ARGV + ["--dry-run"])
    events = "\n".join(p.read_text() for p in tmp_path.glob("*.events.jsonl"))
    assert '"status": "accepted"' not in events
    assert '"status": "discarded"' in events


def test_the_prompt_tells_the_model_what_we_hold(monkeypatch, patched_fetch_markdown):
    """Without the inventory the model guesses at absence instead of computing a
    difference -- and the held-document rejection rule has nothing to check."""
    seen = {}

    def stub(**kw):
        seen.update(kw)
        return json.dumps({"description": "विवरण।", "missing_documents": []})

    _run_main(monkeypatch, _StubApi([CASE_READY]), stub, BASE_ARGV + ["--dry-run"])
    assert "DOCUMENTS WE ALREADY HOLD" in ed.EXTRACTION_USER_PROMPT
    assert "प्रेस विज्ञप्ति" in seen["content"]
    assert "विशेष अदालतको फैसला" in seen["content"]


def test_missing_details_appears_in_the_review_file(monkeypatch, patched_fetch_markdown,
                                                    tmp_path):
    """The review file is what a human reads before approving a run, so the
    value has to be IN it, not only in the events log."""
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api,
              _stub_with_documents("### क) सार\nविवरण।", ["साक्षीहरूको वकपत्र"]),
              BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "missing_details →" in text
    assert "साक्षीहरूको वकपत्र" in text


def test_the_review_files_char_count_measures_the_description_alone(
        monkeypatch, patched_fetch_markdown, tmp_path):
    """The file's header names `description`, and its before/after size comparison
    is what flags a truncated or runaway one. Folding missing_details into
    `generated` over-reported it by the length of a second field."""
    description = "### क) सार\n" + "क" * 400
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api,
              _stub_with_documents(description, ["साक्षीहरूको वकपत्र"]),
              BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert f"### Generated ({len(description):,} chars)" in text


def test_discarded_findings_reach_the_review_file_not_just_the_events_log(
        monkeypatch, patched_fetch_markdown, tmp_path):
    """Otherwise "found four documents and threw them away" is indistinguishable
    from "found nothing" to the person approving the run."""
    api = _StubApi([CASE_PRESS_ONLY])       # no verdict -> nothing is written
    _run_main(monkeypatch, api,
              _stub_with_documents("विवरण।", ["साक्षी रामु खनालको वकपत्र"]),
              BASE_ARGV + ["--dry-run"])
    text = _review_file(tmp_path).read_text(encoding="utf-8")
    assert "NOT written" in text and "no verdict bound" in text
    assert "साक्षी रामु खनालको वकपत्र" in text


def test_the_prompt_and_the_enforced_item_cap_agree():
    """`MAX_LLM_ITEMS` is duplicated as a bare literal in the system prompt, which
    cannot be an f-string because of the JSON braces in OUTPUT FORMAT. Raise the
    constant and the model keeps returning the old number; lower it and the model
    reliably produces one item that `accept_items` then logs as over-cap on every
    single case."""
    assert f"up to {md.MAX_LLM_ITEMS}," in ed.EXTRACTION_SYSTEM_PROMPT


def test_nothing_left_to_write_when_both_fields_are_satisfied(monkeypatch,
                                                              patched_fetch_markdown):
    """The per-field gate admits a described case for its EMPTY missing_details --
    but if BOTH floor items are already satisfied (charge sheet bound AND a Supreme
    reference on file) and the model finds nothing, there is nothing honest to say
    and no reason to rewrite the description either. That leaves an empty patch,
    which must not become a request. Raised by CodeRabbit on #441."""
    case = dict(
        CASE_WITH_APPEAL,                       # Supreme ref -> appeal item drops
        slug="case-nothing-left",
        description="क" * 900,                  # substantial -> not rewritten
        evidence=CASE_WITH_APPEAL["evidence"] + [
            {"material_iri": "https://jawafdehi.org/material/charge_sheet/1",
             "additional_details": "",
             "material": {"material_type": "charge_sheet",
                          "urls": [{"link": _PRESS_MD, "role": "MARKDOWN"}]}},
        ],                                      # charge sheet -> that item drops too
    )
    api = _StubApi([case])
    report = _run_main(monkeypatch, api, _stub_with_documents("नयाँ विवरण।", []),
                       BASE_ARGV + ["--apply"])
    assert api.patched == [], "sent a PATCH with nothing in it"
    assert api.patch_calls == []
    assert report.rows[-1]["status"] == "already"
    assert report.rows[-1]["reason"] == "nothing left to write"


# --------------------------------------------------------------------------
# The review rules, end to end through main()
# --------------------------------------------------------------------------

# A caption of the shape every one of the 24 cached FY078/079 orders has, and a
# closing block of the shape 23 of them have, with 40k of filler between -- long
# enough that `_assemble_source_text` replaces the order with a SUMMARY, which is
# exactly the path that used to lose the header.
_ORDER_CAPTION = (
    "विशेष अदालत, काठमाडौँ\nइजलास नं.२\n"
    "सदस्य माननीय न्यायाधीश श्री तेज नारायण सिंह राई\n"
    "फैसला\nमुद्दा नं. 079-CR-0116\nफैसला मितिः 2081।01।20।5\nनिर्णय नं. 127\n"
)
_ORDER_CLOSING = (
    "\nप्रस्तुत फैसला उपर चित्त नबुझे विशेष अदालत ऐन, २०५९ को दफा १७ बमोजिम ३५ "
    "दिनभित्र श्री सर्वोच्च अदालतमा पुनरावेदन गर्नु।\n"
    "इति सम्वत् २०७९ साल माघ महिना १० गते रोज ३ मा शुभम्।\n"
)
LONG_ORDER = _ORDER_CAPTION + ("म" * 40_000) + _ORDER_CLOSING
# The same order under `VERDICT_SUMMARY_TRIGGER`, which is the path
# `_assemble_source_text` passes through VERBATIM.
SHORT_ORDER = _ORDER_CAPTION + ("म" * 8_000) + _ORDER_CLOSING
# A तारेख order, not the फैसला: the second court_order a case can carry, and the
# one that comes first in evidence order.
PROCEDURAL_ORDER = (
    "विशेष अदालत, काठमाडौँ\nइजलास नं.१\n"
    "आदेश\nमुद्दा नं. 079-CR-0116\nतारेख तोक्ने आदेश\n" + "स" * 600
)
_COURT_MD_PROCEDURAL = "https://x/court-procedural.md"

# A procedural order bound AHEAD of the judgment -- what `next(...)` over the
# chunks picked up.
CASE_TWO_ORDERS = dict(
    CASE_READY, slug="case-two-orders",
    evidence=[
        {"material_iri": "https://jawafdehi.org/material/court/special.079-cr-0116-a",
         "additional_details": "",
         "material": {"material_type": "court_order",
                      "urls": [{"link": _COURT_MD_PROCEDURAL, "role": "MARKDOWN"}]}},
        {"material_iri": "https://jawafdehi.org/material/ciaa/12345",
         "additional_details": "",
         "material": {"material_type": "press_release",
                      "urls": [{"link": _PRESS_MD, "role": "MARKDOWN"}]}},
        {"material_iri": "https://jawafdehi.org/material/court/special.079-cr-0116",
         "additional_details": "",
         "material": {"material_type": "court_order",
                      "urls": [{"link": _COURT_MD, "role": "MARKDOWN"}]}},
    ],
)


def _fetch_fixture(monkeypatch, mapping):
    import casework.common.materials as m

    monkeypatch.setattr(m, "fetch_markdown",
                        lambda link, timeout=60: mapping.get(link, ""))


@pytest.fixture
def patched_fetch_long_order(monkeypatch):
    _fetch_fixture(monkeypatch, {
        _PRESS_MD: "अख्तियारले ठेक्कामा भ्रष्टाचार भएको जनाएको।",
        _COURT_MD: LONG_ORDER})


@pytest.fixture
def patched_fetch_short_order(monkeypatch):
    _fetch_fixture(monkeypatch, {
        _PRESS_MD: "अख्तियारले ठेक्कामा भ्रष्टाचार भएको जनाएको।",
        _COURT_MD: SHORT_ORDER})


@pytest.fixture
def patched_fetch_two_orders(monkeypatch):
    _fetch_fixture(monkeypatch, {
        _PRESS_MD: "अख्तियारले ठेक्कामा भ्रष्टाचार भएको जनाएको।",
        _COURT_MD_PROCEDURAL: PROCEDURAL_ORDER,
        _COURT_MD: LONG_ORDER})


def _order_header_block(content):
    """The rendered ORDER HEADER slot alone.

    The SOURCE DOCUMENTS block below it carries the same order, so a
    whole-prompt substring test cannot tell the two apart. Split on the slot's
    own delimiters -- both the block's instructions and the "it is below in
    full" note mention SOURCE DOCUMENTS, so the bare heading is not a boundary.
    """
    after = content.split("header field, this block is the record:")[1]
    return after.split("SOURCE DOCUMENTS (press release")[0]


def _capture_prompt(response):
    seen = {}

    def stub(**kw):
        # The verdict summariser shares this stub; only the description call
        # carries the system prompt, so key off that.
        if kw.get("system") == ed.EXTRACTION_SYSTEM_PROMPT:
            seen["content"] = kw["content"]
            return response
        return "फैसलाको सारांश।"

    return stub, seen


def test_the_order_header_survives_summarisation_and_reaches_the_prompt(
    monkeypatch, patched_fetch_long_order
):
    """The whole point of the block: a 40k order is summarised, and a summary is
    not trusted to carry the caption. Both prompts have ASKED for the bench since
    2026-06-17 and 14 of 16 published descriptions still had no judge in them."""
    stub, seen = _capture_prompt(json.dumps({"description": "क" * 900}))
    _run_main(monkeypatch, _StubApi([CASE_READY]), invoke_text_stub=stub,
              argv=["--apply"])
    content = seen["content"]
    assert "इजलास नं.२" in content
    assert "तेज नारायण सिंह राई" in content
    assert "फैसला मितिः 2081।01।20।5" in content
    assert "निर्णय नं. 127" in content
    # ...and the closing block, which is where an order with no फैसला मिति field
    # keeps its date and where the appeal म्याद lives.
    assert "इति सम्वत् २०७९ साल माघ महिना १० गते" in content
    assert "दफा १७" in content
    # The middle is NOT duplicated into the header block.
    assert content.count("म" * 5_000) <= 1


def test_an_order_below_the_summary_trigger_is_not_sent_twice(
    monkeypatch, patched_fetch_short_order
):
    """Under `VERDICT_SUMMARY_TRIGGER` the order reaches SOURCE DOCUMENTS
    VERBATIM, so the header block repeated the whole document under a second
    label -- and the block's claim to outrank the summary means nothing when
    there is no summary. Measured at 8,127 chars: caption twice, prompt 17,950."""
    stub, seen = _capture_prompt(json.dumps({"description": "क" * 900}))
    _run_main(monkeypatch, _StubApi([CASE_READY]), invoke_text_stub=stub,
              argv=["--apply"])
    content = seen["content"]
    assert content.count("इजलास नं.२") == 1
    assert content.count("इति सम्वत् २०७९ साल माघ महिना १० गते") == 1
    assert content.count("म" * 5_000) == 1


def test_an_order_below_the_trigger_is_not_reported_as_absent(
    monkeypatch, patched_fetch_short_order
):
    """Dropping the block must not make the prompt claim the case has no court
    order. The NAMED NON-DEFENDANTS rule sends the model to this block for the
    प्रतिवादी list, and "there is none" is the answer that gets a co-defendant
    marked a non-defendant."""
    stub, seen = _capture_prompt(json.dumps({"description": "क" * 900}))
    _run_main(monkeypatch, _StubApi([CASE_READY]), invoke_text_stub=stub,
              argv=["--apply"])
    header = _order_header_block(seen["content"])
    assert "(कुनै अदालती आदेश छैन)" not in header
    assert ed.ORDER_IN_SOURCES_NOTE in header


def test_the_order_header_comes_from_the_judgment_not_the_first_bound_order(
    monkeypatch, patched_fetch_two_orders
):
    """`next(...)` took the FIRST court_order in evidence order. Two orders on
    one case is a shape this module already knows -- see the `verdict_read` note
    -- and a तारेख order's caption handed over as "the record" is then trusted
    over the summary for फैसला मिति, नि.नं. and the प्रतिवादी list."""
    stub, seen = _capture_prompt(json.dumps({"description": "क" * 900}))
    _run_main(monkeypatch, _StubApi([CASE_TWO_ORDERS]), invoke_text_stub=stub,
              argv=["--apply"])
    header = _order_header_block(seen["content"])
    assert "इजलास नं.२" in header, "the judgment's bench, not the तारेख order's"
    assert "फैसला मितिः 2081।01।20।5" in header
    assert "तारेख तोक्ने आदेश" not in header


def test_a_case_with_no_court_order_says_so_instead_of_crashing(
    monkeypatch, patched_fetch_markdown
):
    stub, seen = _capture_prompt(json.dumps({"description": "क" * 900}))
    _run_main(monkeypatch, _StubApi([CASE_PRESS_ONLY]), invoke_text_stub=stub,
              argv=["--apply"])
    assert "(कुनै अदालती आदेश छैन)" in seen["content"]


def test_an_account_number_the_model_leaves_in_is_masked_before_the_patch(
    monkeypatch, patched_fetch_markdown
):
    leaky = ("### ग) विशेष अदालतको फैसलाको सार\n\nप्रतिवादीको एभरेष्ट बैंक "
             "खाता नं. ०१२००५१६२००४२६ मा रु.८,८८,०००।– जम्मा भएको।" + "क" * 800)
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: json.dumps(
        {"description": leaky}), argv=["--apply"])
    patched = [v for _, f, v, _ in api.patched if f == "description"]
    assert patched, "the description must still be written"
    assert "०१२००५१६२००४२६" not in patched[0]
    assert "खाता नं. ...००४२६" in patched[0]
    # The amount beside it is load-bearing and stays.
    assert "रु.८,८८,०००।–" in patched[0]


def test_an_account_number_in_missing_details_is_masked_before_the_patch(
    monkeypatch, patched_fetch_markdown
):
    """The SAME conditional PATCH writes two public fields and only
    `description` went through the mask. `missing_details` is model-authored
    Nepali whose whole value is naming a specific record, so it is the output
    most likely to carry an account number -- and it carried it in full."""
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, _stub_with_documents(
        "क" * 900,
        ["सुनिल पौडेलको एभरेष्ट बैंक खाता नं. ०१२००५१६२००४२६ को लेनदेन विवरण"]),
        BASE_ARGV + ["--apply"])
    written = {f: v for _, f, v, _ in api.patched}
    assert "०१२००५१६२००४२६" not in written["missing_details"]
    assert "खाता नं. ...००४२६" in written["missing_details"]


def test_a_plate_in_missing_details_is_flagged_like_one_in_the_description(
    monkeypatch, patched_fetch_markdown
):
    """`residual_identifiers` ran over the description only, so a plate in the
    second field published with nothing raised. Reported, never edited: only the
    model knows the asset class the rule asks for."""
    rows = _captured_review_rows(monkeypatch)
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, _stub_with_documents(
        "क" * 900, ["बा.१२ प ३४५६ नम्बरको सवारी दर्ता प्रमाणपत्र"]),
        BASE_ARGV + ["--apply"])
    written = {f: v for _, f, v, _ in api.patched}
    assert "बा.१२ प ३४५६" in written["missing_details"], "reported, never edited"
    # The PRIVACY note, not the `missing_details →` echo -- that one repeats the
    # whole field and would carry the plate whether or not anything detected it.
    assert "vehicle plate" in rows[-1].note
    assert "बा.१२ प ३४५६" in rows[-1].note.split("vehicle plate")[1]


def test_the_dry_run_shows_the_masked_text_not_the_raw_one(
    monkeypatch, patched_fetch_markdown
):
    # The review file is what a human signs off before --apply, so it has to show
    # what would actually be written.
    leaky = ("प्रतिवादीको खाता नं. ०१२००५१६२००४२६ मा जम्मा भएको।" + "क" * 800)
    rows = _captured_review_rows(monkeypatch)
    api = _StubApi([CASE_READY])
    report = _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: json.dumps(
        {"description": leaky}), argv=["--dry-run"])
    assert api.patched == []
    assert report.rows[0]["status"] == "would-enrich"
    # The point of the test: the row a human signs off carries the MASKED text.
    review_row = rows[-1]
    assert "०१२००५१६२००४२६" not in review_row.generated
    assert "खाता नं. ...००४२६" in review_row.generated


def _captured_review_rows(monkeypatch):
    """Every `ReviewRow` the run adds, in order.

    The review FILE is what a human signs off before `--apply`, and `main()`
    returns only the report, so the rows are intercepted at `ReviewFile.add`.
    """
    from casework.common.review import ReviewFile

    rows = []
    monkeypatch.setattr(ReviewFile, "add", lambda self, row: rows.append(row))
    return rows


class TestResidualIdentifiers:
    """`description.mask_account_identifiers`, the one clause with no mechanical
    fix: a plate must become an asset class, and only the model knows the class.

    The three plates below are live in production today -- they are in the
    description of the ministry vehicle-purchase case, which is why this reports
    rather than assumes the prompt was obeyed.
    """

    @pytest.mark.parametrize("text", [
        "साविक कृषि मन्त्री चढ्ने स्कार्पियो नम्बर ग १ झ ६१६ को गाडी खरिद",
        "बा १ झ ९१८२ नं. को गाडी मर्मत गराउँदा अनियमितता",
        "बा.१२ प ३४५६ नम्बरको मोटरसाइकल जफत गर्ने ठहर",
        "ना ५ च ८९०१ को स्कुटर",
        "बा १२ प 3456 को सवारी साधन",          # Latin serial
    ])
    def test_a_plate_is_reported(self, text):
        assert ed.residual_identifiers(text)

    @pytest.mark.parametrize("text", [
        "नि.नं. १२७ मिति २०८१।०१।२०",
        "कि.नं. २८४९ को जग्गा",
        "मुद्दा नं. ०७९-CR-०१६० को फैसला मिति २०८१।०२।०६",
        "दफा १७ बमोजिम ३५ दिने म्याद",
        "रु.७,४९,८४० बिगो कायम गरिएको",
        "एक मोटरसाइकल र एक कार जफत गर्ने ठहर",   # the form the rule asks for
        "खाता नं. ...००४२६ मा जम्मा",
    ])
    def test_a_case_identifier_an_amount_or_an_asset_class_is_not(self, text):
        assert ed.residual_identifiers(text) == []

    def test_it_is_measured_against_production_not_guessed(self):
        # Run over all 213 populated descriptions in the 2026-09-01 snapshot the
        # detector fired on ONE case, and all three hits were real plates. The
        # regression that matters is a false positive on ordinary prose.
        prose = ("प्रतिवादीले सरकारी जग्गा आफ्नो नाममा दर्ता गराई रु.२ करोड "
                 "गैरकानूनी लाभ लिएको र निजको आय स्रोत नखुलेको भन्ने आरोप छ। "
                 "विशेष अदालतले नि.नं. २८३, फैसला मिति २०८१।०२।०६ मा सफाइ दिएको।")
        assert ed.residual_identifiers(prose) == []

    def test_none_and_empty_survive(self):
        assert ed.residual_identifiers("") == []
        assert ed.residual_identifiers(None) == []

    def test_each_plate_is_reported_once_sorted(self):
        text = "बा.१२ प ३४५६ र ना ५ च ८९०१ र फेरि बा.१२ प ३४५६"
        assert ed.residual_identifiers(text) == ["ना ५ च ८९०१", "बा.१२ प ३४५६"]


def test_a_plate_the_model_left_in_is_flagged_on_the_review_row(
    monkeypatch, patched_fetch_markdown
):
    # Reported, NOT edited: masking the digits would satisfy the rule's letter
    # and lose its action, which is to name the asset class instead.
    leaky = ("### ग) विशेष अदालतको फैसलाको सार\n\nबा.१२ प ३४५६ नम्बरको "
             "मोटरसाइकल जफत गर्ने ठहर।" + "क" * 800)
    rows = _captured_review_rows(monkeypatch)
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: json.dumps(
        {"description": leaky}), argv=["--dry-run"])
    row = rows[-1]
    assert "बा.१२ प ३४५६" in row.note
    assert "बा.१२ प ३४५६" in row.generated, "the plate is reported, never edited out"


def test_a_clean_description_carries_no_privacy_note(
    monkeypatch, patched_fetch_markdown
):
    clean = ("### ग) विशेष अदालतको फैसलाको सार\n\nएक मोटरसाइकल जफत गर्ने "
             "ठहर।" + "क" * 800)
    rows = _captured_review_rows(monkeypatch)
    api = _StubApi([CASE_READY])
    _run_main(monkeypatch, api, invoke_text_stub=lambda **kw: json.dumps(
        {"description": clean}), argv=["--dry-run"])
    assert "vehicle plate" not in rows[-1].note
