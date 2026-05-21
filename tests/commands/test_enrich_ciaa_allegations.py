"""Tests for the enrich_ciaa_allegations management command."""

import io
import json
import os
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError

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
        "court_cases": (
            court_cases if court_cases is not None else ["special:081-CR-0123"]
        ),
        "bigo": bigo,
        "evidence": evidence if evidence is not None else [],
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


# ── Model normalization tests ──────────────────────────────────


@pytest.mark.parametrize(
    "input_model,expected",
    [
        ("qwen3.5-plus", "qwen3.5-plus"),
        ("opencode-go/qwen3.5-plus", "qwen3.5-plus"),
        ("openai:qwen3.5-plus", "qwen3.5-plus"),
        ("opencode-go/claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("openai:gpt-4o", "gpt-4o"),
        ("  opencode-go/minimax-m2.5  ", "minimax-m2.5"),
        ("minimax-m2.5", "minimax-m2.5"),
        ("minimax-m2.7", "minimax-m2.7"),
        ("claude-sonnet-4-5", "claude-sonnet-4-5"),
    ],
)
def test_normalize_model(input_model, expected):
    from cases.management.commands.enrich_ciaa_allegations import normalize_model

    assert normalize_model(input_model) == expected


def test_normalize_model_no_prefix_change():
    from cases.management.commands.enrich_ciaa_allegations import normalize_model

    assert normalize_model("bare-model-id") == "bare-model-id"


def test_normalize_model_empty_string():
    from cases.management.commands.enrich_ciaa_allegations import normalize_model

    assert normalize_model("") == ""
    assert normalize_model("  ") == ""


# ── Base URL normalization tests ────────────────────────────────


@pytest.mark.parametrize(
    "input_url,expected",
    [
        ("https://opencode.ai/zen/go/v1", "https://opencode.ai/zen/go/v1"),
        ("https://opencode.ai/zen/v1", "https://opencode.ai/zen/go/v1"),
        ("https://opencode.ai/zen/go", "https://opencode.ai/zen/go/v1"),
        (
            "https://opencode.ai/zen/v1/",
            "https://opencode.ai/zen/go/v1",
        ),
        (
            "https://opencode.ai/zen/go/",
            "https://opencode.ai/zen/go/v1",
        ),
        ("https://custom.proxy/v1", "https://custom.proxy/v1"),
        (
            "https://custom.proxy/v1/",
            "https://custom.proxy/v1",
        ),
    ],
)
def test_normalize_base_url_explicit(input_url, expected):
    from cases.management.commands.enrich_ciaa_allegations import normalize_base_url

    assert normalize_base_url(input_url) == expected


@patch.dict(
    os.environ,
    {},
    clear=True,
)
def test_normalize_base_url_default_no_env():
    from cases.management.commands.enrich_ciaa_allegations import normalize_base_url

    result = normalize_base_url(None)
    assert result == "https://opencode.ai/zen/go/v1"


@patch.dict(
    os.environ,
    {"JAWAFDEHI_LLM_PROXY_URL": "https://llm-proxy.jawafdehi.org/v1"},
    clear=True,
)
def test_normalize_base_url_from_env():
    from cases.management.commands.enrich_ciaa_allegations import normalize_base_url

    result = normalize_base_url(None)
    assert result == "https://llm-proxy.jawafdehi.org/v1"


@patch.dict(
    os.environ,
    {"JAWAFDEHI_LLM_PROXY_URL": "https://opencode.ai/zen/v1"},
    clear=True,
)
def test_normalize_base_url_env_canonicalized():
    from cases.management.commands.enrich_ciaa_allegations import normalize_base_url

    result = normalize_base_url(None)
    assert result == "https://opencode.ai/zen/go/v1"


# ── Endpoint selection tests ────────────────────────────────────


@pytest.mark.parametrize(
    "model,expected_suffix",
    [
        ("qwen3.5-plus", "/chat/completions"),
        ("opencode-go/qwen3.5-plus", "/chat/completions"),
        ("claude-sonnet-4-5", "/chat/completions"),
        ("minimax-m2.5", "/messages"),
        ("minimax-m2.7", "/messages"),
        ("opencode-go/minimax-m2.5", "/messages"),
    ],
)
def test_llm_endpoint_routing(model, expected_suffix):
    from cases.management.commands.enrich_ciaa_allegations import _llm_endpoint

    base = "https://opencode.ai/zen/go/v1"
    endpoint = _llm_endpoint(base, model)
    assert endpoint == f"{base}{expected_suffix}"


def test_llm_endpoint_base_trailing_slash():
    from cases.management.commands.enrich_ciaa_allegations import _llm_endpoint

    base = "https://opencode.ai/zen/go/v1/"
    endpoint = _llm_endpoint(base, "minimax-m2.5")
    assert endpoint == "https://opencode.ai/zen/go/v1/messages"


# ── API key precedence tests ────────────────────────────────────


@patch.dict(
    os.environ,
    {
        "JAWAFDEHI_LLM_API_KEY": "env-jawafdehi-key",
        "OPENCODE_API_KEY": "env-opencode-key",
        "ANTHROPIC_API_KEY": "env-anthropic-key",
    },
    clear=True,
)
def test_resolve_api_key_cli_wins():
    from cases.management.commands.enrich_ciaa_allegations import resolve_api_key

    assert resolve_api_key("cli-key") == "cli-key"


@patch.dict(
    os.environ,
    {
        "JAWAFDEHI_LLM_API_KEY": "env-jawafdehi-key",
        "OPENCODE_API_KEY": "env-opencode-key",
        "ANTHROPIC_API_KEY": "env-anthropic-key",
    },
    clear=True,
)
def test_resolve_api_key_jawafdehi_env():
    from cases.management.commands.enrich_ciaa_allegations import resolve_api_key

    assert resolve_api_key(None) == "env-jawafdehi-key"


@patch.dict(
    os.environ,
    {
        "OPENCODE_API_KEY": "env-opencode-key",
        "ANTHROPIC_API_KEY": "env-anthropic-key",
    },
    clear=True,
)
def test_resolve_api_key_opencode_env():
    from cases.management.commands.enrich_ciaa_allegations import resolve_api_key

    assert resolve_api_key(None) == "env-opencode-key"


@patch.dict(
    os.environ,
    {
        "ANTHROPIC_API_KEY": "env-anthropic-key",
    },
    clear=True,
)
def test_resolve_api_key_anthropic_fallback():
    from cases.management.commands.enrich_ciaa_allegations import resolve_api_key

    assert resolve_api_key(None) == "env-anthropic-key"


@patch.dict(os.environ, {}, clear=True)
def test_resolve_api_key_none_available():
    from cases.management.commands.enrich_ciaa_allegations import resolve_api_key

    with pytest.raises(CommandError, match="No API key provided"):
        resolve_api_key(None)


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


# ── Source scoring tests ───────────────────────────────────


def test_score_source_for_press_release():
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()
    source = MagicMock()
    source.title = "CIAA Press Release FY 080/81"
    source.description = "Press release regarding corruption case"
    source.uploaded_filename = None
    source.url = ["https://ciaa.gov.np/pressrelease/3173"]
    source.uploaded_files.all.return_value = []
    source.source_type = None

    score = cmd._score_source_for_press_release(source)
    assert score >= 8


# ── Content conversion tests ────────────────────────────────


@pytest.mark.django_db
def test_convert_source_no_url_no_file():
    from cases.management.commands.enrich_ciaa_allegations import Command

    source = DocumentSource.objects.create(
        source_id="source:empty",
        title="Empty Source",
        url=[],
    )
    cmd = Command()
    with pytest.raises(CommandError, match="No downloadable URLs found"):
        cmd._convert_source_to_markdown(source)


@pytest.mark.django_db
def test_convert_source_uses_description_first():
    from cases.management.commands.enrich_ciaa_allegations import Command

    source = DocumentSource.objects.create(
        source_id="source:desc",
        title="With Description",
        description="This is a long enough description text that should be used "
        "directly without downloading any URLs. It has more than fifty characters.",
        url=["https://ciaa.gov.np/pressrelease/1"],
    )
    cmd = Command()
    result = cmd._convert_source_to_markdown(source)
    assert "long enough description" in result


def test_ranked_source_urls_prioritizes_ngm_pdf():
    from cases.management.commands.enrich_ciaa_allegations import Command

    source = DocumentSource(
        source_id="source:ngm",
        title="NGM Source",
        url=[
            "https://ciaa.gov.np/pressrelease/3173",
            "https://ngm-store.jawafdehi.org/ciaa/2024/123.pdf",
            "https://other.example.com/doc.docx",
        ],
    )
    cmd = Command()
    ranked = cmd._ranked_source_urls(source)
    # NGM-store PDF should be first
    assert "ngm-store.jawafdehi.org" in ranked[0]
    assert ranked[0].endswith(".pdf")
    # Direct docs (PDF/DOCX) come before non-direct URLs
    assert len(ranked) == 3


def test_ranked_source_urls_no_direct_urls():
    from cases.management.commands.enrich_ciaa_allegations import Command

    source = DocumentSource(
        source_id="source:web",
        title="Web Only Source",
        url=[
            "https://ciaa.gov.np/pressrelease/1",
            "https://other.gov.np/page",
        ],
    )
    cmd = Command()
    ranked = cmd._ranked_source_urls(source)
    # All URLs returned in source order when no direct docs
    assert len(ranked) == 2
    assert ranked[0] == "https://ciaa.gov.np/pressrelease/1"


@pytest.mark.django_db
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._download_url_to_path",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._download_source_to_path",
    return_value=None,
)
def test_convert_source_tries_prioritized_urls_first(
    mock_download_file, mock_download_url
):
    from cases.management.commands.enrich_ciaa_allegations import Command

    source = DocumentSource.objects.create(
        source_id="source:ngm-pdf",
        title="NGM Mapped Source",
        url=[
            "https://ciaa.gov.np/pressrelease/3173",
            "https://ngm-store.jawafdehi.org/ciaa/2024/123.pdf",
        ],
    )
    mock_download_url.return_value = None

    cmd = Command()
    with pytest.raises(CommandError, match="Unable to convert source"):
        cmd._convert_source_to_markdown(source)

    assert mock_download_url.call_count >= 2
    first_url = mock_download_url.call_args_list[0][0][0]
    assert "ngm-store.jawafdehi.org" in first_url
    assert first_url.endswith(".pdf")


@pytest.mark.django_db
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._download_url_to_path",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._download_source_to_path",
    return_value=None,
)
def test_convert_source_fallback_to_next_url(
    mock_download_file, mock_download_url
):
    from cases.management.commands.enrich_ciaa_allegations import Command

    source = DocumentSource.objects.create(
        source_id="source:fallback",
        title="Fallback Source",
        url=[
            "https://ngm-store.jawafdehi.org/ciaa/2024/broken.pdf",
            "https://ciaa.gov.np/pressrelease/3173",
        ],
    )
    mock_download_url.return_value = None

    cmd = Command()
    with pytest.raises(CommandError, match="Unable to convert source"):
        cmd._convert_source_to_markdown(source)

    # Both URLs should have been attempted for fallback
    assert mock_download_url.call_count >= 2


# ── Dry-run safety test ────────────────────────────────────────


@pytest.mark.django_db
@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._call_llm_opencode",
    return_value='{"allegations": ["Test allegation from opencode"]}',
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._convert_source_to_markdown",
    return_value="Mocked press release content for dry-run test with enough chars",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.resolve_api_key",
    return_value="test-key",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.normalize_base_url",
    return_value="https://opencode.ai/zen/go/v1",
)
def test_dry_run_does_not_save(
    mock_norm_url,
    mock_resolve_key,
    mock_convert,
    mock_llm,
):
    _make_source(
        source_id="source:dry",
        title="Press Release Dry",
        url=["https://example.com/pr.md"],
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

    mock_llm.assert_called_once()
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
        "--llm-base-url",
        "--llm-api-key",
        "--llm-timeout",
        "--verbose",
        "--force",
        "--case-id",
        "--priority",
        "--all",
        "--base-url",
    ],
)
def test_cli_flags_registered(flag):
    cmd = load_command_class("cases", "enrich_ciaa_allegations")
    parser = cmd.create_parser("manage.py", "enrich_ciaa_allegations")
    for action in parser._actions:
        if flag in action.option_strings:
            return
    pytest.fail(f"Flag {flag} not found in command arguments")


# ── Retry behaviour (429/5xx) tests ─────────────────────────


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, url, code, body, headers=None, fp=None):
        super().__init__(url, code, "", headers or {}, fp)
        self._body = body.encode("utf-8")

    def read(self, n=-1):
        if n == -1:
            return self._body
        return self._body[:n]

    def readinto(self, buf):
        data = self._body
        buf[: len(data)] = data
        return len(data)


@patch("urllib.request.urlopen")
def test_call_llm_opencode_retries_on_429(mock_urlopen):
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()

    url = "https://opencode.ai/zen/go/v1/chat/completions"
    error = _FakeHTTPError(url, 429, '{"error":"rate limited"}')
    mock_urlopen.side_effect = error

    with patch("time.sleep", return_value=None) as mock_sleep:
        with pytest.raises(CommandError, match="LLM HTTP 429"):
            cmd._call_llm_opencode(
                model="claude-sonnet-4-5",
                base_url="https://opencode.ai/zen/go/v1",
                api_key="test-key",
                timeout=30,
                prompt="Test prompt",
            )

    assert mock_urlopen.call_count == 3
    assert mock_sleep.call_count == 2


@patch("urllib.request.urlopen")
def test_call_llm_opencode_retries_on_503(mock_urlopen):
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()

    url = "https://opencode.ai/zen/go/v1/chat/completions"
    error = _FakeHTTPError(url, 503, '{"error":"service unavailable"}')
    mock_urlopen.side_effect = error

    with patch("time.sleep", return_value=None) as mock_sleep:
        with pytest.raises(CommandError, match="LLM HTTP 503"):
            cmd._call_llm_opencode(
                model="claude-sonnet-4-5",
                base_url="https://opencode.ai/zen/go/v1",
                api_key="test-key",
                timeout=30,
                prompt="Test prompt",
            )

    assert mock_urlopen.call_count == 3
    assert mock_sleep.call_count == 2


@patch("urllib.request.urlopen")
def test_call_llm_opencode_no_retry_on_400(mock_urlopen):
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()

    url = "https://opencode.ai/zen/go/v1/chat/completions"
    error = _FakeHTTPError(url, 400, '{"error":"bad request"}')
    mock_urlopen.side_effect = error

    with pytest.raises(CommandError, match="LLM HTTP 400"):
        cmd._call_llm_opencode(
            model="claude-sonnet-4-5",
            base_url="https://opencode.ai/zen/go/v1",
            api_key="test-key",
            timeout=30,
            prompt="Test prompt",
        )

    assert mock_urlopen.call_count == 1


@patch("urllib.request.urlopen")
def test_call_llm_opencode_error_contains_body_snippet(mock_urlopen):
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()

    url = "https://opencode.ai/zen/go/v1/chat/completions"
    error = _FakeHTTPError(url, 502, '{"error":"gateway timeout","trace":"abc123"}')
    mock_urlopen.side_effect = error

    with pytest.raises(CommandError, match="gateway timeout"):
        cmd._call_llm_opencode(
            model="claude-sonnet-4-5",
            base_url="https://opencode.ai/zen/go/v1",
            api_key="test-key",
            timeout=30,
            prompt="Test prompt",
        )


@patch("urllib.request.urlopen")
def test_call_llm_opencode_success_standard_model(mock_urlopen):
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {
            "choices": [
                {"message": {"content": '{"allegations": ["Test allegation content"]}'}}
            ]
        }
    ).encode("utf-8")
    mock_urlopen.return_value = mock_resp

    result = cmd._call_llm_opencode(
        model="opencode-go/qwen3.5-plus",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="test-key",
        timeout=30,
        prompt="Test prompt",
    )

    assert "allegations" in result
    assert mock_urlopen.call_count == 1


@patch("urllib.request.urlopen")
def test_call_llm_opencode_success_minimax_model(mock_urlopen):
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {"content": [{"text": '{"allegations": ["Minimax allegation"]}'}]}
    ).encode("utf-8")
    mock_urlopen.return_value = mock_resp

    result = cmd._call_llm_opencode(
        model="minimax-m2.5",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="test-key",
        timeout=30,
        prompt="Test prompt",
    )

    assert "allegations" in result
    assert mock_urlopen.call_count == 1
    called_url = mock_urlopen.call_args[0][0].full_url
    assert "/messages" in called_url


# ── LLM timeout tests ──────────────────────────────────────


@patch.dict(os.environ, {}, clear=True)
def test_llm_timeout_default():
    from cases.management.commands.enrich_ciaa_allegations import _llm_timeout

    assert _llm_timeout(None) == 300


def test_llm_timeout_cli_wins():
    from cases.management.commands.enrich_ciaa_allegations import _llm_timeout

    assert _llm_timeout(60) == 60


@patch.dict(os.environ, {"JAWAFDEHI_LLM_TIMEOUT_SECONDS": "120"}, clear=True)
def test_llm_timeout_from_env():
    from cases.management.commands.enrich_ciaa_allegations import _llm_timeout

    assert _llm_timeout(None) == 120


@patch.dict(os.environ, {"JAWAFDEHI_LLM_TIMEOUT_SECONDS": "notanumber"}, clear=True)
def test_llm_timeout_invalid_env_fallback():
    from cases.management.commands.enrich_ciaa_allegations import _llm_timeout

    assert _llm_timeout(None) == 300


# ── 429 hint message test ───────────────────────────────────


@patch("urllib.request.urlopen")
def test_call_llm_opencode_429_includes_usage_hint(mock_urlopen):
    from cases.management.commands.enrich_ciaa_allegations import Command

    cmd = Command()

    url = "https://opencode.ai/zen/go/v1/chat/completions"
    error = _FakeHTTPError(url, 429, '{"error":"too many requests"}')
    mock_urlopen.side_effect = error

    with patch("time.sleep", return_value=None):
        with pytest.raises(CommandError) as exc_info:
            cmd._call_llm_opencode(
                model="claude-sonnet-4-5",
                base_url="https://opencode.ai/zen/go/v1",
                api_key="test-key",
                timeout=30,
                prompt="Test prompt",
            )

    error_msg = str(exc_info.value)
    assert "429" in error_msg
    assert "usage limits" in error_msg.lower() or "limit" in error_msg.lower()


# ── OpenCode Go vs Anthropic routing tests ───────────────────


@pytest.mark.django_db
@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._call_llm_opencode",
    return_value='{"allegations": ["Jawafdehi proxy route test"]}',
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._call_llm_anthropic",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._convert_source_to_markdown",
    return_value="Mocked press release content for proxy routing test with enough chars for LLM.",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.resolve_api_key",
    return_value="test-key",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.normalize_base_url",
    return_value="https://llm-proxy.jawafdehi.org/v1",
)
def test_jawafdehi_proxy_routes_to_opencode(
    mock_norm_url,
    mock_resolve_key,
    mock_convert,
    mock_llm_anthropic,
    mock_llm_opencode,
):
    _make_source(
        source_id="source:proxy-test",
        title="Press Release Proxy",
        url=["https://example.com/pr.md"],
    )
    _make_case(
        case_id="proxy-test",
        evidence=[{"source_id": "source:proxy-test", "description": "PR"}],
    )

    out = io.StringIO()
    call_command(
        "enrich_ciaa_allegations",
        "--case-id=proxy-test",
        stdout=out,
    )

    mock_llm_opencode.assert_called_once()
    mock_llm_anthropic.assert_not_called()


@pytest.mark.django_db
@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._call_llm_anthropic",
    return_value=["Anthropic route test"],
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._call_llm_opencode",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._convert_source_to_markdown",
    return_value="Mocked press release content for anthropic routing test with enough chars for LLM.",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.resolve_api_key",
    return_value="test-key",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.normalize_base_url",
    return_value="https://api.anthropic.com",
)
def test_anthropic_com_routes_to_anthropic_sdk(
    mock_norm_url,
    mock_resolve_key,
    mock_convert,
    mock_llm_opencode,
    mock_llm_anthropic,
):
    _make_source(
        source_id="source:anthropic-test",
        title="Press Release Anthropic",
        url=["https://example.com/pr.md"],
    )
    _make_case(
        case_id="anthropic-test",
        evidence=[{"source_id": "source:anthropic-test", "description": "PR"}],
    )

    out = io.StringIO()
    call_command(
        "enrich_ciaa_allegations",
        "--case-id=anthropic-test",
        stdout=out,
    )

    mock_llm_anthropic.assert_called_once()
    mock_llm_opencode.assert_not_called()


# ── Priority/all flags tests ─────────────────────────────────


@pytest.mark.django_db
def test_priority_and_case_id_mutually_exclusive():
    from django.core.management import CommandError

    _make_case(
        case_id="prio-mutex",
        court_cases=["special:080-CR-0007"],
    )

    with pytest.raises(CommandError, match="mutually exclusive"):
        call_command(
            "enrich_ciaa_allegations",
            "--priority",
            "--case-id=prio-mutex",
        )


@pytest.mark.django_db
@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._call_llm_opencode",
    return_value='{"allegations": ["Priority test"]}',
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._convert_source_to_markdown",
    return_value="Mocked press release content for priority flag test with enough chars for LLM.",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.resolve_api_key",
    return_value="test-key",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.normalize_base_url",
    return_value="https://opencode.ai/zen/go/v1",
)
def test_priority_flag_filters_to_loader_cases(
    mock_norm_url,
    mock_resolve_key,
    mock_convert,
    mock_llm,
):
    _make_source(
        source_id="source:prio",
        title="Priority Press Release",
        url=["https://example.com/pr.md"],
    )
    # Create one case in the priority list and one outside
    _make_case(
        case_id="prio-yes",
        court_cases=["special:080-CR-0007"],
        evidence=[{"source_id": "source:prio", "description": "PR"}],
    )
    _make_case(
        case_id="prio-no",
        court_cases=["special:999-WC-0001"],
        evidence=[{"source_id": "source:prio", "description": "PR"}],
    )

    out = io.StringIO()
    call_command(
        "enrich_ciaa_allegations",
        "--priority",
        stdout=out,
    )

    # Only the priority case should have been processed
    case_yes = Case.objects.get(case_id="prio-yes")
    case_no = Case.objects.get(case_id="prio-no")
    assert case_yes.key_allegations == ["Priority test"]
    assert case_no.key_allegations == []


@pytest.mark.django_db
@patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._call_llm_opencode",
    return_value='{"allegations": ["All test"]}',
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.Command._convert_source_to_markdown",
    return_value="Mocked press release content for all flag test with enough chars for LLM.",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.resolve_api_key",
    return_value="test-key",
)
@patch(
    "cases.management.commands.enrich_ciaa_allegations.normalize_base_url",
    return_value="https://opencode.ai/zen/go/v1",
)
def test_all_flag_processes_all_not_just_priority(
    mock_norm_url,
    mock_resolve_key,
    mock_convert,
    mock_llm,
):
    _make_source(
        source_id="source:all",
        title="All Press Release",
        url=["https://example.com/pr.md"],
    )
    ev = [{"source_id": "source:all", "description": "PR"}]
    _make_case(case_id="all-a", court_cases=["special:080-CR-0007"], evidence=ev)
    _make_case(case_id="all-b", court_cases=["special:999-WC-0001"], evidence=ev)

    out = io.StringIO()
    call_command(
        "enrich_ciaa_allegations",
        "--all",
        stdout=out,
    )

    case_a = Case.objects.get(case_id="all-a")
    case_b = Case.objects.get(case_id="all-b")
    assert case_a.key_allegations == ["All test"]
    assert case_b.key_allegations == ["All test"]
