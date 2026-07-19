"""Tests for the A/B harness's pure logic.

The highest-risk pieces here are (a) the arm-invocation asymmetry -- the donor
writes by default while the port needs `--apply`, so a symmetric command
builder would silently turn one arm into a no-op and report false parity --
and (b) the port-ownership gate, which is the only thing standing between an
`--apply` run and writing into another OS user's Django instance.
"""

import os

import pytest

from casework.ab.run_ab import (
    RE_CASE_HEADER,
    STAGES,
    assert_port_is_ours,
    owner_uid,  # noqa: F401 -- imported so the stub target is a real attribute
    build_rows,
    header_slug,
    parse_entities,
    parse_outcomes,
    resolve_arm_values,
    run_stage,
)


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def captured(monkeypatch):
    """Capture the argv the harness would execute instead of running it."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return _Proc()

    monkeypatch.setattr("casework.ab.run_ab.subprocess.run", fake_run)
    return seen


# ------------------------------------------------------ arm asymmetry ---


def test_donor_arm_writes_without_apply_flag(captured):
    """Arm A's --dry-run is OPT-OUT. Adding --apply (which it does not
    define) would crash it; omitting --dry-run is what makes it write."""
    run_stage("A", "/tmp/a", "bigo", ["s1"], "http://x", apply_writes=True,
              model="haiku")
    cmd = captured["cmd"]
    assert "--apply" not in cmd
    assert "--dry-run" not in cmd
    assert "--force" in cmd


def test_donor_arm_dry_run_is_explicit(captured):
    run_stage("A", "/tmp/a", "bigo", ["s1"], "http://x", apply_writes=False,
              model="haiku")
    assert "--dry-run" in captured["cmd"]


def test_port_arm_requires_apply_to_write(captured):
    """Arm B is read-only unless --apply. Omitting it would make the port a
    no-op and manufacture 'the port produced nothing' as a fake result."""
    run_stage("B", "/tmp/b", "bigo", ["s1"], "http://x", apply_writes=True,
              model="haiku")
    assert "--apply" in captured["cmd"]


def test_port_arm_without_apply_has_no_apply_flag(captured):
    run_stage("B", "/tmp/b", "bigo", ["s1"], "http://x", apply_writes=False,
              model="haiku")
    assert "--apply" not in captured["cmd"]


def test_both_arms_are_pinned_to_the_same_provider_and_model(captured):
    """If the arms ran different models the comparison would be worthless."""
    run_stage("A", "/tmp/a", "bigo", ["s"], "http://x", True, "haiku")
    a = captured["cmd"]
    run_stage("B", "/tmp/b", "bigo", ["s"], "http://x", True, "haiku")
    b = captured["cmd"]
    for cmd in (a, b):
        assert cmd[cmd.index("--provider") + 1] == "claude_cli"
        assert cmd[cmd.index("--model") + 1] == "haiku"


def test_every_sample_slug_is_passed_to_the_arm(captured):
    run_stage("B", "/tmp/b", "bigo", ["s1", "s2", "s3"], "http://x", True, "haiku")
    cmd = captured["cmd"]
    assert [cmd[i + 1] for i, v in enumerate(cmd) if v == "--slug"] == ["s1", "s2", "s3"]


def test_force_is_always_passed(captured):
    """Most sample cases already carry June's values; without --force the
    arms would skip them and the run would compare nothing."""
    for arm in ("A", "B"):
        run_stage(arm, "/tmp/x", "tags", ["s"], "http://x", True, "haiku")
        assert "--force" in captured["cmd"]


def test_stage_map_covers_all_five_enrichers():
    assert set(STAGES) == {"bigo", "tags", "timeline", "allegations", "entities"}


# ------------------------------------------------------ safety gate ---


def test_port_gate_refuses_when_nothing_is_listening(monkeypatch):
    monkeypatch.setattr("casework.ab.run_ab.listening_pid", lambda p: None)
    with pytest.raises(SystemExit):
        assert_port_is_ours(48010)


def test_port_gate_refuses_a_foreign_uid(monkeypatch):
    monkeypatch.setattr("casework.ab.run_ab.listening_pid", lambda p: 4242)
    monkeypatch.setattr("casework.ab.run_ab.owner_uid", lambda pid: os.getuid() + 1)
    with pytest.raises(SystemExit) as exc:
        assert_port_is_ours(48010)
    assert "REFUSING" in str(exc.value)


def test_port_gate_accepts_our_own_pid(monkeypatch):
    monkeypatch.setattr("casework.ab.run_ab.listening_pid", lambda p: 4242)
    monkeypatch.setattr("casework.ab.run_ab.owner_uid", lambda pid: os.getuid())
    assert assert_port_is_ours(48010) == 4242


def test_port_gate_refuses_when_the_pid_vanished(monkeypatch):
    monkeypatch.setattr("casework.ab.run_ab.listening_pid", lambda p: 4242)

    def _gone(pid):
        raise FileNotFoundError

    monkeypatch.setattr("casework.ab.run_ab.owner_uid", _gone)
    with pytest.raises(SystemExit):
        assert_port_is_ours(48010)


# ------------------------------------------------------ outcome parsing ---


def test_outcomes_distinguish_unmet_skipped_error_and_enriched():
    stdout = "\n".join([
        "[1/4] s1",
        "  Unmet prerequisite(s): press_release: no MARKDOWN role",
        "[2/4] s2",
        "  LLM could not extract a reliable BIGO — skipping",
        "[3/4] s3",
        "  LLM extraction failed: boom",
        "[4/4] s4",
        "  [UPDATED] s4: BIGO=100",
    ])
    out = parse_outcomes(stdout, ["s1", "s2", "s3", "s4"])
    assert out["s1"] == "unmet"
    assert out["s2"] == "skipped"
    assert out["s3"] == "error"
    assert out["s4"] == "enriched"


def test_a_slug_with_no_output_line_is_not_silently_a_success():
    """A header with no following status line must not read as success."""
    out = parse_outcomes("[1/2] s1\n[2/2] s2", ["s1", "s2"])
    assert out["s1"] == "no-output-line"
    assert out["s2"] == "no-output-line"
    assert "enriched" not in out.values()


def test_a_requested_slug_the_arm_never_mentioned_is_not_a_success():
    """Fewer headers than requested slugs -> the whole run is unattributed,
    never a silent pass for the slug that was skipped."""
    out = parse_outcomes("[1/1] s1\n  [UPDATED] s1", ["s1", "s2"])
    assert "enriched" not in out.values()
    assert set(out.values()) == {"unattributed-header-mismatch"}


def test_summary_counters_are_not_attributed_to_the_last_case():
    """The run summary follows the last case block. Its counter lines
    contain the word 'error'; attributing them to the last case would
    silently misreport one case per stage."""
    stdout = "\n".join([
        "[1/2] s1", "  [UPDATED] s1: BIGO=100",
        "[2/2] s2", "  [UPDATED] s2: BIGO=200",
        "",
        "============================================================",
        "[DRY RUN] BIGO extraction",
        "  Cases processed          2",
        "  Cases llm error          0",
    ])
    out = parse_outcomes(stdout, ["s1", "s2"])
    assert out["s2"] == "enriched", "summary counter leaked onto the last case"
    assert out["s1"] == "enriched"


def test_port_style_summary_also_terminates_attribution():
    stdout = "\n".join([
        "[1/1] s1", "  [UPDATED] s1",
        "", "=== BIGO extraction (DRY RUN) ===", "  error: 1",
    ])
    assert parse_outcomes(stdout, ["s1"])["s1"] == "enriched"


def test_a_real_error_before_the_summary_is_still_recorded():
    stdout = "\n".join([
        "[1/1] s1", "  Failed to PATCH timeline: 422 Client Error",
        "", "============", "  Cases llm error   0",
    ])
    assert parse_outcomes(stdout, ["s1"])["s1"] == "error"


def test_outcomes_ignore_lines_before_any_case_header():
    out = parse_outcomes("some banner [UPDATED] nonsense\n[1/1] s1", ["s1"])
    assert out["s1"] == "no-output-line"


# ------------------------------------------------------ entity parsing ---


# ------------------------------------------------ header attribution ---


def test_port_header_is_attributed_by_its_own_slug():
    m = RE_CASE_HEADER.match("[1/2] real-slug — title")
    assert header_slug(m, ["real-slug", "other"]) == "real-slug"


def test_donor_question_mark_header_is_attributed_positionally():
    """The donor prints case.get('case_id', '?') -- literally '?' today."""
    m = RE_CASE_HEADER.match("[2/3] ? — title")
    assert header_slug(m, ["s1", "s2", "s3"]) == "s2"


def test_positional_attribution_refuses_when_the_arm_skipped_a_case():
    """A failed fetch shifts every later index. Refuse rather than
    misattribute every subsequent case to the wrong slug."""
    m = RE_CASE_HEADER.match("[2/2] ? — title")
    assert header_slug(m, ["s1", "s2", "s3"]) is None


def test_outcomes_report_mismatch_instead_of_guessing():
    stdout = "[1/2] ?\n  [UPDATED] x\n[2/2] ?\n  [UPDATED] y"
    out = parse_outcomes(stdout, ["s1", "s2", "s3"])
    assert set(out.values()) == {"unattributed-header-mismatch"}


def test_outcomes_attribute_donor_headers_positionally_when_counts_line_up():
    stdout = "[1/2] ?\n  [UPDATED] x\n[2/2] ?\n  LLM extraction failed: boom"
    out = parse_outcomes(stdout, ["s1", "s2"])
    assert out["s1"] == "enriched"
    assert out["s2"] == "error"


def test_donor_entities_attributed_exactly_in_per_slug_mode():
    stdout = "[1/1] ?\n  [DRY RUN] location  Kathmandu"
    per = parse_entities(stdout, "A", ["only-slug"])
    assert per["only-slug"]["count"] == 1


def test_port_entity_counts_come_from_its_explicit_total():
    stdout = "\n".join([
        "[1/1] s1",
        "  Extracted 3 entities, 2 accused note(s) — NOT bound",
        "    location  Kathmandu",
        "    related   Ram Bahadur",
    ])
    per = parse_entities(stdout, "B", ["s1"])
    assert per["s1"]["count"] == 3
    assert per["s1"]["accused_notes"] == 2
    assert per["s1"]["names"] == ["Kathmandu", "Ram Bahadur"]


def test_donor_entity_counts_come_from_its_per_item_dry_run_lines():
    stdout = "\n".join([
        "[1/1] s1",
        "  [DRY RUN] location  Kathmandu",
        "  [DRY RUN] related   Sita Devi  — some notes",
    ])
    per = parse_entities(stdout, "A", ["s1"])
    assert per["s1"]["count"] == 2
    assert per["s1"]["names"] == ["Kathmandu", "Sita Devi"]


def test_donor_entity_names_are_recovered_from_create_failures():
    """Under --apply the donor's create_entity 400s; the names still appear."""
    stdout = "\n".join([
        "[1/1] s1",
        "    Failed to create entity 'Kathmandu': 400",
    ])
    per = parse_entities(stdout, "A", ["s1"])
    assert per["s1"]["names"] == ["Kathmandu"]


def test_entity_parsing_of_empty_output_is_empty_not_an_error():
    assert parse_entities("", "A", ["s1"]) == {}
    assert parse_entities("", "B", ["s1"]) == {}


# ------------------------------------------------------ row construction ---


def test_rows_cover_every_field_for_every_slug():
    rows = build_rows(["s1"], {"s1": {}}, {"s1": {}}, {}, {}, {})
    assert {r["stage"] for r in rows} == set(STAGES)
    assert all(r["slug"] == "s1" for r in rows)


def test_rows_carry_arm_values_through_to_the_comparison():
    a = {"s1": {"bigo": 100, "tags": ["x"], "timeline": [], "key_allegations": []}}
    b = {"s1": {"bigo": 250, "tags": ["x"], "timeline": [], "key_allegations": []}}
    g = {"s1": {"bigo": 100}}
    rows = build_rows(["s1"], a, b, g, {}, {})
    bigo = next(r for r in rows if r["stage"] == "bigo")
    assert bigo["verdict"] == "b_diverges"
    tags = next(r for r in rows if r["stage"] == "tags")
    assert tags["verdict"] == "both_diverge_from_golden"


def test_a_case_where_neither_arm_produced_anything_is_no_output():
    empty = {"s1": {"bigo": None, "tags": [], "timeline": [], "key_allegations": []}}
    rows = build_rows(["s1"], empty, empty, {}, {}, {})
    assert {r["verdict"] for r in rows} == {"no_output"}


# ------------------------------------------------ the residue trap ---


def test_a_value_the_arm_did_not_write_is_not_credited_to_it():
    """June's shipped value is already in the field. An arm that produced
    nothing must NOT be credited with the residue sitting there."""
    readback = {"s1": {"bigo": 913280, "tags": ["क"], "timeline": [],
                       "key_allegations": []}}
    outcomes = {"bigo": {"s1": "unmet"}, "tags": {"s1": "enriched"}}
    out = resolve_arm_values(readback, outcomes, ["s1"])
    assert out["s1"]["bigo"] is None, "unmet stage must not claim the residue"
    assert out["s1"]["tags"] == ["क"], "enriched stage keeps its value"


def test_residue_cannot_manufacture_agreement_between_the_arms():
    """Both arms fail on a case that already holds June's value. Neither
    produced anything, so the comparison must say so -- not 'all_agree'."""
    readback = {"s1": {"bigo": 913280, "tags": [], "timeline": [],
                       "key_allegations": []}}
    outcomes = {"bigo": {"s1": "unmet"}}
    a = resolve_arm_values(readback, outcomes, ["s1"])
    b = resolve_arm_values(readback, outcomes, ["s1"])
    rows = build_rows(["s1"], a, b, {"s1": {"bigo": 913280}}, {}, {})
    bigo = next(r for r in rows if r["stage"] == "bigo")
    assert bigo["verdict"] == "no_output"
    assert bigo["verdict"] != "all_agree"


@pytest.mark.parametrize("status", ["unmet", "skipped", "error",
                                    "no-output-line",
                                    "unattributed-header-mismatch"])
def test_only_enriched_counts_as_production(status):
    readback = {"s1": {"bigo": 5, "tags": [], "timeline": [],
                       "key_allegations": []}}
    out = resolve_arm_values(readback, {"bigo": {"s1": status}}, ["s1"])
    assert out["s1"]["bigo"] is None


def test_enriched_value_is_preserved_exactly():
    readback = {"s1": {"bigo": 5, "tags": ["क"], "timeline": [{"date": "d"}],
                       "key_allegations": ["a"]}}
    outcomes = {s: {"s1": "enriched"} for s in
                ("bigo", "tags", "timeline", "allegations")}
    out = resolve_arm_values(readback, outcomes, ["s1"])
    assert out["s1"] == {"bigo": 5, "tags": ["क"],
                         "timeline": [{"date": "d"}], "key_allegations": ["a"]}


def test_resolve_preserves_readback_errors():
    out = resolve_arm_values({"s1": {"_error": "HTTP 500"}}, {}, ["s1"])
    assert "_error" in out["s1"]


def test_readback_failure_is_flagged_not_read_as_empty_output():
    """An arm whose values could not be read back must not be scored as
    'produced nothing' -- that is a measurement failure, not a result."""
    good = {"s1": {"bigo": 100, "tags": [], "timeline": [], "key_allegations": []}}
    broken = {"s1": {"_error": "HTTP 500"}}
    rows = build_rows(["s1"], good, broken, {}, {}, {})
    assert all(r["verdict"] == "readback_error" for r in rows)
    assert all(r["readback_error"] == ["B"] for r in rows)


def test_rows_without_readback_errors_record_an_empty_flag():
    ok = {"s1": {"bigo": 1, "tags": [], "timeline": [], "key_allegations": []}}
    rows = build_rows(["s1"], ok, ok, {}, {}, {})
    assert all(r["readback_error"] == [] for r in rows)
    assert not any(r["verdict"] == "readback_error" for r in rows)


def test_entity_rows_use_extracted_names_not_case_fields():
    rows = build_rows(["s1"], {"s1": {}}, {"s1": {}}, {},
                      {"s1": {"names": ["A"]}}, {"s1": {"names": ["A"]}})
    ent = next(r for r in rows if r["stage"] == "entities")
    assert ent["a"] == ["A"] and ent["b"] == ["A"]
    assert ent["write_path_comparable"] is False
