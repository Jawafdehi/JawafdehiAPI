"""Tests for the enrich_ciaa_allegations management command."""

import io
import json
import os
from unittest.mock import patch

import pytest
from django.core.management import call_command, load_command_class

from cases.models import Case, CaseState, CaseType, DocumentSource


def _make_case(
    case_id="test-case-001",
    title="Test CIAA Case",
    state=CaseState.DRAFT,
    court_cases=None,
    key_allegations=None,
    bigo=None,
    evidence=None,
):
    kwargs = {
        "case_id": case_id,
        "title": title,
        "case_type": CaseType.CORRUPTION,
        "state": state,
        "court_cases": court_cases or ["special:081-CR-0123"],
        "bigo": bigo,
        "evidence": evidence or [],
    }
    if key_allegations is not None:
        kwargs["key_allegations"] = key_allegations
    return Case.objects.create(**kwargs)


def _make_source(
    source_id="source:test-001",
    title="CIAA Press Release",
    description="",
    url=None,
):
    return DocumentSource.objects.create(
        source_id=source_id,
        title=title,
        description=description,
        url=(
            url
            if url is not None
            else ["https://ngm-store.jawafdehi.org/case/test-file.md"]
        ),
        source_type=None,
    )


# ── Parser tests ─────────────────────────────────────────────────


def test_parse_valid_json_array():
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()
    raw = (
        '{"allegations": ["First allegation", "Second allegation", "Third allegation"]}'
    )
    result = cmd._parse_allegations(raw)
    assert result == ["First allegation", "Second allegation", "Third allegation"]


def test_parse_json_with_markdown_fence():
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()
    raw = '```json\n{"allegations": ["Nested", "List"]}\n```'
    result = cmd._parse_allegations(raw)
    assert result == ["Nested", "List"]


def test_parse_json_with_surrounding_text():
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()
    raw = 'Here is the result:\n{"allegations": ["Allegation A"]}\nHope that helps.'
    result = cmd._parse_allegations(raw)
    assert result == ["Allegation A"]


def test_parse_invalid_json_fallback():
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()
    raw = "1. First allegation\n2. Second allegation\n"
    result = cmd._parse_allegations(raw)
    assert len(result) == 2


def test_parse_empty_array_returns_none():
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()
    raw = '{"allegations": []}'
    result = cmd._parse_allegations(raw)
    assert result is None


def test_parse_truncates_over_max():
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()
    items = [f"Allegation {i}" for i in range(10)]
    raw = json.dumps({"allegations": items})
    result = cmd._parse_allegations(raw)
    assert len(result) == 5


# ── Case filtering tests ──────────────────────────────────


@pytest.mark.django_db
def test_get_eligible_cases_includes_ciaa():
    from cases.management.commands.enrich_ciaa_allegations import Command

    _make_case(case_id="ciaa-001")
    cmd = Command()
    cases = cmd._get_eligible_cases(limit=None, force=False, case_id=None)
    assert len(cases) == 1
    assert cases[0].case_id == "ciaa-001"


@pytest.mark.django_db
def test_get_eligible_cases_skips_non_ciaa():
    from cases.management.commands.enrich_ciaa_allegations import Command

    _make_case(case_id="non-ciaa", court_cases=["supreme:078-WC-0123"])
    cmd = Command()
    cases = cmd._get_eligible_cases(limit=None, force=False, case_id=None)
    assert len(cases) == 0


@pytest.mark.django_db
def test_get_eligible_cases_skips_already_populated():
    from cases.management.commands.enrich_ciaa_allegations import Command

    _make_case(
        case_id="has-allegations",
        key_allegations=["Already has allegations"],
    )
    cmd = Command()
    cases = cmd._get_eligible_cases(limit=None, force=False, case_id=None)
    assert len(cases) == 0


@pytest.mark.django_db
def test_get_eligible_cases_force_includes_populated():
    from cases.management.commands.enrich_ciaa_allegations import Command

    _make_case(
        case_id="has-allegations",
        key_allegations=["Already has allegations"],
    )
    cmd = Command()
    cases = cmd._get_eligible_cases(limit=None, force=True, case_id=None)
    assert len(cases) == 1


@pytest.mark.django_db
def test_get_eligible_cases_respects_limit():
    from cases.management.commands.enrich_ciaa_allegations import Command

    for i in range(5):
        _make_case(case_id=f"ciaa-{i:03d}")
    cmd = Command()
    cases = cmd._get_eligible_cases(limit=2, force=False, case_id=None)
    assert len(cases) == 2


@pytest.mark.django_db
def test_get_eligible_cases_respects_case_id():
    from cases.management.commands.enrich_ciaa_allegations import Command

    _make_case(case_id="target")
    _make_case(case_id="other")
    cmd = Command()
    cases = cmd._get_eligible_cases(limit=None, force=False, case_id="target")
    assert len(cases) == 1
    assert cases[0].case_id == "target"


# ── Press release content tests ──────────────────────────────────


@pytest.mark.django_db
def test_acquire_press_release_text_no_source_returns_none():
    from cases.management.commands.enrich_ciaa_allegations import Command

    case = _make_case(case_id="no-evidence", evidence=None)
    cmd = Command()
    cmd._source_lookup = {}
    result = cmd._acquire_press_release_text(case, dry_run=False)
    assert result is None


@pytest.mark.django_db
def test_acquire_press_release_text_from_source():
    from cases.management.commands.enrich_ciaa_allegations import Command

    src = _make_source(
        source_id="source:url-test",
        description="",
        url=["https://ngm-store.jawafdehi.org/case/file.md"],
    )
    case = _make_case(
        case_id="test-url",
        evidence=[{"source_id": "source:url-test", "description": "PR"}],
    )
    cmd = Command()
    cmd._source_lookup = {"source:url-test": src}

    with patch.object(
        cmd,
        "_convert_source_to_markdown",
        return_value="Converted markdown content for testing with enough characters to pass the minimum length check of fifty characters easily met",
    ):
        result = cmd._acquire_press_release_text(case, dry_run=False)
    assert (
        result
        == "Converted markdown content for testing with enough characters to pass the minimum length check of fifty characters easily met"
    )


@pytest.mark.django_db
def test_acquire_press_release_text_empty_evidence_returns_none():
    from cases.management.commands.enrich_ciaa_allegations import Command

    case = _make_case(
        case_id="empty-evidence",
        evidence=[{}, {"source_id": ""}],
    )
    cmd = Command()
    cmd._source_lookup = {}
    result = cmd._acquire_press_release_text(case, dry_run=False)
    assert result is None


# ── Dry-run safety test ────────────────────────────────────────


@pytest.mark.django_db
@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._init_client",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._call_llm",
    return_value=["Test allegation"],
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._convert_source_to_markdown",
    return_value="Mocked press release content for dry-run test with enough characters to pass the minimum length check",
)
def test_dry_run_does_not_save(mock_fetch, mock_llm, mock_client):
    _make_source(
        source_id="source:dry",
        description="Dry run test content " * 10,
    )
    _make_case(
        case_id="dry-run-test",
        evidence=[{"source_id": "source:dry", "description": "PR"}],
    )

    out = io.StringIO()
    call_command(
        "enrich_ciaa_allegations",
        "--dry-run",
        "--case-id=dry-run-test",
        stdout=out,
    )

    case = Case.objects.get(case_id="dry-run-test")
    assert case.key_allegations == []


# ── CLI flags test ────────────────────────────────────────────


def test_command_help():
    cmd = load_command_class("cases", "enrich_ciaa_allegations")
    assert cmd is not None
    assert "enrich_ciaa_allegations" in cmd.__module__


@pytest.mark.parametrize(
    "flag",
    [
        "--dry-run",
        "--limit",
        "--llm-model",
        "--base-url",
        "--verbose",
        "--force",
        "--case-id",
    ],
)
def test_cli_flags_registered(flag):
    cmd = load_command_class("cases", "enrich_ciaa_allegations")
    parser = cmd.create_parser("manage.py", "enrich_ciaa_allegations")
    for action in parser._actions:
        if flag in action.option_strings:
            return
    pytest.fail(f"Flag {flag} not found in command arguments")
