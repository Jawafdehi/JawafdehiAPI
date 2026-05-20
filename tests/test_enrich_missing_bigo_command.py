import tempfile
import sys
import urllib.parse
from pathlib import Path
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile

from cases.management.commands.enrich_missing_bigo import Command
from cases.models import Case, CaseState, CaseType, DocumentSource, SourceType


def _create_case(
    case_id: str,
    title: str,
    state: str,
    bigo: int | None,
    evidence: list[dict] | None = None,
) -> Case:
    return Case.objects.create(
        case_id=case_id,
        case_type=CaseType.CORRUPTION,
        state=state,
        title=title,
        timeline=[],
        evidence=evidence or [],
        bigo=bigo,
    )


def _create_source(source_id: str, title: str, url: str) -> DocumentSource:
    return DocumentSource.objects.create(
        source_id=source_id,
        title=title,
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        url=[url],
    )


@pytest.mark.django_db
def test_enriches_only_draft_cases_with_missing_bigo():
    source = _create_source(
        source_id="source:test:press-001",
        title="CIAA Press Release",
        url="https://example.com/press-release.pdf",
    )
    target = _create_case(
        case_id="case-draft-missing-bigo",
        title="Draft Missing BIGO",
        state=CaseState.DRAFT,
        bigo=None,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )
    _create_case(
        case_id="case-draft-has-bigo",
        title="Draft Has BIGO",
        state=CaseState.DRAFT,
        bigo=999,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )
    _create_case(
        case_id="case-published-missing-bigo",
        title="Published Missing BIGO",
        state=CaseState.PUBLISHED,
        bigo=None,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )

    with (
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._convert_source_to_markdown",
            return_value="# Press Release\nबिगो रु. 123456",
        ),
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._extract_bigo_from_markdown",
            return_value=123456,
        ),
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._patch_case_bigo",
        ) as patch_case,
    ):
        call_command(
            "enrich_missing_bigo",
            "--allow-production",
            "--api-token",
            "test-token",
            "--anthropic-api-key",
            "test-key",
        )

    patch_case.assert_called_once()
    assert patch_case.call_args.kwargs["case"] == target
    assert patch_case.call_args.kwargs["bigo"] == 123456


@pytest.mark.django_db
def test_dry_run_previews_without_external_work():
    source = _create_source(
        source_id="source:test:press-002",
        title="CIAA Press Release",
        url="https://example.com/press-release-2.pdf",
    )
    _create_case(
        case_id="case-draft-missing-bigo",
        title="Draft Missing BIGO",
        state=CaseState.DRAFT,
        bigo=None,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )

    out = StringIO()
    with (
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._convert_source_to_markdown",
        ) as convert_source,
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._extract_bigo_from_markdown",
        ) as extract_bigo,
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._patch_case_bigo",
        ) as patch_case,
    ):
        call_command(
            "enrich_missing_bigo",
            "--allow-production",
            "--dry-run",
            "--api-token",
            "test-token",
            "--anthropic-api-key",
            "test-key",
            stdout=out,
        )

    convert_source.assert_not_called()
    extract_bigo.assert_not_called()
    patch_case.assert_not_called()
    output = out.getvalue()
    assert "DRY-RUN" in output
    assert "selected source=" in output


@pytest.mark.django_db
def test_dry_run_extract_runs_extraction_but_does_not_patch_cases():
    source = _create_source(
        source_id="source:test:press-002-extract",
        title="CIAA Press Release",
        url="https://example.com/press-release-2-extract.pdf",
    )
    _create_case(
        case_id="case-draft-missing-bigo-extract",
        title="Draft Missing BIGO Extract",
        state=CaseState.DRAFT,
        bigo=None,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )

    out = StringIO()
    with (
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._convert_source_to_markdown",
            return_value="# Press Release\nConverted press release text without deterministic BIGO marker",
        ) as convert_source,
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._extract_bigo_from_markdown",
            return_value=123456,
        ) as extract_bigo,
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._patch_case_bigo",
        ) as patch_case,
    ):
        call_command(
            "enrich_missing_bigo",
            "--allow-production",
            "--dry-run",
            "--dry-run-extract",
            "--api-token",
            "test-token",
            "--anthropic-api-key",
            "test-key",
            stdout=out,
        )

    convert_source.assert_called_once()
    extract_bigo.assert_called_once()
    patch_case.assert_not_called()
    assert "would PATCH BIGO=123456" in out.getvalue()


@pytest.mark.django_db
def test_llm_api_key_alias_is_used_for_extraction():
    source = _create_source(
        source_id="source:test:press-llm-key",
        title="CIAA Press Release",
        url="https://example.com/press-release-llm-key.pdf",
    )
    _create_case(
        case_id="case-draft-missing-bigo-llm-key",
        title="Draft Missing BIGO LLM Key",
        state=CaseState.DRAFT,
        bigo=None,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )

    with (
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._convert_source_to_markdown",
            return_value="# Press Release\nConverted press release text without deterministic BIGO marker",
        ),
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._extract_bigo_from_markdown",
            return_value=123456,
        ) as extract_bigo,
    ):
        call_command(
            "enrich_missing_bigo",
            "--allow-production",
            "--dry-run",
            "--dry-run-extract",
            "--llm-api-key",
            "provider-neutral-key",
        )

    assert extract_bigo.call_args.kwargs["anthropic_api_key"] == "provider-neutral-key"


@pytest.mark.django_db
def test_generic_llm_model_and_base_url_env_defaults_are_forwarded(monkeypatch):
    monkeypatch.setenv("JAWAFDEHI_CASEWORK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("JAWAFDEHI_LLM_PROXY_URL", "https://opencode.ai/zen/go/v1")
    monkeypatch.delenv("BIGO_ENRICHMENT_MODEL", raising=False)
    monkeypatch.delenv("BIGO_ENRICHMENT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BIGO_ENRICHMENT_BASE_URL", raising=False)
    monkeypatch.delenv("JAWAFDEHI_CASEWORK_BASE_URL", raising=False)

    source = _create_source(
        source_id="source:test:generic-llm-env",
        title="CIAA Press Release",
        url="https://example.com/press-release-generic-llm-env.pdf",
    )
    _create_case(
        case_id="case-draft-generic-llm-env",
        title="Draft Generic LLM Env",
        state=CaseState.DRAFT,
        bigo=None,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )

    with (
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._convert_source_to_markdown",
            return_value="# Press Release\nConverted press release text without deterministic BIGO marker",
        ),
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._extract_bigo_from_markdown",
            return_value=123456,
        ) as extract_bigo,
    ):
        call_command(
            "enrich_missing_bigo",
            "--allow-production",
            "--dry-run",
            "--dry-run-extract",
            "--llm-api-key",
            "provider-neutral-key",
        )

    assert extract_bigo.call_args.kwargs["model"] == "deepseek-v4-pro"
    assert (
        extract_bigo.call_args.kwargs["llm_base_url"] == "https://opencode.ai/zen/go/v1"
    )


@pytest.mark.django_db
def test_cli_llm_model_and_base_url_override_generic_env_defaults(monkeypatch):
    monkeypatch.setenv("BIGO_ENRICHMENT_MODEL", "env-model")
    monkeypatch.setenv("BIGO_ENRICHMENT_LLM_BASE_URL", "https://env.example/v1")

    source = _create_source(
        source_id="source:test:generic-llm-cli",
        title="CIAA Press Release",
        url="https://example.com/press-release-generic-llm-cli.pdf",
    )
    _create_case(
        case_id="case-draft-generic-llm-cli",
        title="Draft Generic LLM CLI",
        state=CaseState.DRAFT,
        bigo=None,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )

    with (
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._convert_source_to_markdown",
            return_value="# Press Release\nConverted press release text without deterministic BIGO marker",
        ),
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._extract_bigo_from_markdown",
            return_value=123456,
        ) as extract_bigo,
    ):
        call_command(
            "enrich_missing_bigo",
            "--allow-production",
            "--dry-run",
            "--dry-run-extract",
            "--llm-api-key",
            "provider-neutral-key",
            "--llm-model",
            "cli-model",
            "--llm-base-url",
            "https://cli.example/v1",
        )

    assert extract_bigo.call_args.kwargs["model"] == "cli-model"
    assert extract_bigo.call_args.kwargs["llm_base_url"] == "https://cli.example/v1"


def test_build_bigo_prompt_mentions_multiple_amounts_and_damage_claim_focus():
    command = Command()
    case = SimpleNamespace(case_id="case-prompt-001", title="Case prompt title")

    prompt = command._build_bigo_prompt(
        markdown="""
            रकम रु. 5,00,000 उल्लेख छ।
            बिगो रु. 1,50,000 रहेको छ।
            जरिवाना रु. 20,000 छ।
        """,
        case=case,
    )

    assert "When multiple monetary amounts appear" in prompt
    assert "damage claim amount" in prompt
    assert "मागदाबी" in prompt


@pytest.mark.django_db
def test_verbose_flag_emits_detailed_logs():
    source = _create_source(
        source_id="source:test:press-verbose-001",
        title="CIAA Press Release",
        url="https://example.com/press-release-verbose.pdf",
    )
    _create_case(
        case_id="case-draft-missing-bigo-verbose",
        title="Draft Missing BIGO (verbose)",
        state=CaseState.DRAFT,
        bigo=None,
        evidence=[
            {"source_id": source.source_id, "description": "Press release evidence"}
        ],
    )

    out = StringIO()
    with (
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._convert_source_to_markdown",
            return_value="# Press Release\nबिगो रु. 123456",
        ),
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._extract_bigo_from_markdown",
            return_value=123456,
        ),
        patch(
            "cases.management.commands.enrich_missing_bigo.Command._patch_case_bigo",
        ),
    ):
        call_command(
            "enrich_missing_bigo",
            "--allow-production",
            "--api-token",
            "test-token",
            "--anthropic-api-key",
            "test-key",
            "--verbose",
            stdout=out,
        )

    output = out.getvalue()
    assert "[INFO] Starting BIGO enrichment run" in output
    assert "[INFO] Processing case case-draft-missing-bigo-verbose" in output


@pytest.mark.django_db
def test_production_guardrail_requires_explicit_override(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="refuses to run in production"):
        call_command("enrich_missing_bigo")


@pytest.mark.django_db
def test_limit_guardrail_rejects_over_max():
    with pytest.raises(CommandError, match="must be between 1 and 1000"):
        call_command("enrich_missing_bigo", "--limit", "1001")


def test_validate_url_scheme_allows_https_and_local_http():
    command = Command()

    assert (
        command._validate_url_scheme("https://example.com/press-release.pdf")
        == "https://example.com/press-release.pdf"
    )
    assert (
        command._validate_url_scheme("http://localhost/press-release.pdf")
        == "http://localhost/press-release.pdf"
    )
    assert (
        command._validate_url_scheme("http://127.0.0.1/press-release.pdf")
        == "http://127.0.0.1/press-release.pdf"
    )

    with pytest.raises(
        ValueError, match="Only https URLs are allowed for non-local hosts"
    ):
        command._validate_url_scheme("http://example.com/press-release.pdf")

    with pytest.raises(
        ValueError, match="Only https URLs are allowed for non-local hosts"
    ):
        command._validate_url_scheme("file:///tmp/press-release.pdf")

    with pytest.raises(
        ValueError, match="Only https URLs are allowed for non-local hosts"
    ):
        command._validate_url_scheme("press-release.pdf")


def test_sanitize_download_filename_decodes_urlencoded_unicode_and_keeps_extension():
    command = Command()

    decoded_name = "2413. जिल्ला मोरङ, सुन्दरहरैचा नगरपालिकाका लेखा अधिकृत शेखर ढकालउपर बिगो रु. २,००,०००.– कायम - 2.pdf"  # noqa: RUF001
    encoded_name = urllib.parse.quote(decoded_name, safe="")
    url_path = f"/uploads/ciaa/press-releases/files/{encoded_name}"

    sanitized = command._sanitize_download_filename(
        url_path, source_id="source-test-003"
    )

    assert sanitized.endswith(".pdf")
    assert "%" not in sanitized
    assert len(sanitized) <= 200


def test_sanitize_download_filename_truncates_very_long_filenames_with_hash():
    command = Command()

    very_long = ("a" * 500) + ".pdf"
    sanitized = command._sanitize_download_filename(
        very_long, source_id="source-test-004"
    )

    assert sanitized.endswith(".pdf")
    assert len(sanitized) <= 200
    assert "-" in sanitized


def test_download_source_to_path_sanitizes_dot_filename_and_confines_output():
    command = Command()
    source = DocumentSource(
        source_id="source-test-001",
        title="CIAA Press Release",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        uploaded_filename="..",
        url=[],
    )
    source.uploaded_file = SimpleUploadedFile("nested/press-release.pdf", b"test-bytes")

    with tempfile.TemporaryDirectory(prefix="bigo-test-") as tmp_dir:
        output_dir = Path(tmp_dir)
        out_path = command._download_source_to_path(source, output_dir)

        assert out_path is not None
        assert out_path.name == "source-test-001.bin"
        assert output_dir.resolve() in out_path.resolve().parents
        assert out_path.read_bytes() == b"test-bytes"


def test_case_patch_url_uses_slug_not_numeric_database_id():
    command = Command()

    assert (
        command._case_patch_url("https://api.example.com", "case-slug-abc123")
        == "https://api.example.com/api/cases/case-slug-abc123/"
    )
    assert (
        command._case_patch_url("https://api.example.com/api", "case slug")
        == "https://api.example.com/api/cases/case%20slug/"
    )


def test_case_patch_url_rejects_non_http_base_url():
    command = Command()

    with pytest.raises(ValueError, match="https for non-local hosts"):
        command._case_patch_url("ftp://example.com", "case-slug")

    with pytest.raises(ValueError, match="must include a host"):
        command._case_patch_url("https:///api", "case-slug")


def test_extract_bigo_from_source_metadata_reads_ngm_filename():
    command = Command()
    filename = "2413. जिल्ला मोरङ, सुन्दरहरैचा नगरपालिकाका लेखा अधिकृत शेखर ढकालउपर बिगो रु. २,००,०००.– कायम - 2.pdf"  # noqa: RUF001
    encoded_filename = urllib.parse.quote(filename, safe="")
    source = DocumentSource(
        source_id="source:test:ngm-metadata-bigo",
        title="CIAA Press Release",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        url=[
            f"https://ngm-store.jawafdehi.org/uploads/ciaa/press-releases/files/{encoded_filename}"
        ],
    )

    assert command._extract_bigo_from_source_metadata(source) == 200000


def test_extract_bigo_from_source_metadata_reads_direct_ngm_url_for_case_a4a309dd9ebc():
    command = Command()
    url = "https://ngm-store.jawafdehi.org/uploads/ciaa/press-releases/files/2681.%20%E0%A4%9C%E0%A4%BF%E0%A4%B2%E0%A5%8D%E0%A4%B2%E0%A4%BE%20%E0%A4%B2%E0%A4%B2%E0%A4%BF%E0%A4%A4%E0%A4%AA%E0%A5%81%E0%A4%B0%2C%20%E0%A4%97%E0%A5%8B%E0%A4%A6%E0%A4%BE%E0%A4%B5%E0%A4%B0%E0%A5%80%20%E0%A4%A8%E0%A4%97%E0%A4%B0%E0%A4%AA%E0%A4%BE%E0%A4%B2%E0%A4%BF%E0%A4%95%E0%A4%BE%E0%A4%95%E0%A4%BE%20%E0%A4%A4%E0%A4%A4%E0%A5%8D%E0%A4%95%E0%A4%BE%E0%A4%B2%E0%A5%80%E0%A4%A8%20%E0%A4%A8%E0%A4%97%E0%A4%B0%E0%A4%AA%E0%A5%8D%E0%A4%B0%E0%A4%AE%E0%A5%81%E0%A4%96%20%E0%A4%97%E0%A4%9C%E0%A5%87%E0%A4%A8%E0%A5%8D%E0%A4%A6%E0%A5%8D%E0%A4%B0%20%E0%A4%AE%E0%A4%B9%E0%A4%B0%E0%A5%8D%E0%A4%9C%E0%A4%A8%2C%20%E0%A4%A4%E0%A4%A4%E0%A5%8D%E0%A4%95%E0%A4%BE%E0%A4%B2%E0%A5%80%E0%A4%A8%20%E0%A4%A8%E0%A4%97%E0%A4%B0%20%E0%A4%89%E0%A4%AA%E0%A4%AA%E0%A5%8D%E0%A4%B0%E0%A4%AE%E0%A5%81%E0%A4%96%20%E0%A4%AE%E0%A5%81%E0%A4%A8%E0%A4%BE%20%E0%A4%85%E0%A4%A7%E0%A4%BF%E0%A4%95%E0%A4%BE%E0%A4%B0%E0%A5%80%E0%A4%B8%E0%A4%AE%E0%A5%87%E0%A4%A4%20%E0%A5%AE%20%E0%A4%9C%E0%A4%A8%E0%A4%BE%E0%A4%B0%E0%A4%B5%E0%A4%BF%E0%A4%B0%E0%A5%81%E0%A4%A6%E0%A5%8D%E0%A4%A7%20%E0%A4%AC%E0%A4%BF%E0%A4%97%E0%A5%8B%20%E0%A4%B0%E0%A5%81.%20%E0%A5%A8%E0%A5%AC%2C%E0%A5%AC%E0%A5%A9%2C%E0%A5%A9%E0%A5%AD%2C%E0%A5%A9%E0%A5%AF%E0%A5%AE%20%E0%A4%95%E0%A4%BE%E0%A4%AF%E0%A4%AE%20%E0%A4%97%E0%A4%B0%E0%A5%80%20%E0%A4%AD%E0%A5%8D%E0%A4%B0%E0%A4%B7%E0%A5%8D%E0%A4%9F%E0%A4%BE%E0%A4%9A%E0%A4%BE%E0%A4%B0%20%E0%A4%AE%E0%A5%81%E0%A4%A6%E0%A5%8D%E0%A4%A6%E0%A4%BE%20%E0%A4%A6%E0%A4%BE%E0%A4%AF%E0%A4%B0%20-%202.pdf"
    source = DocumentSource(
        source_id="source:test:case-a4a309dd9ebc",
        title="CIAA Press Release",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        url=[url],
    )

    assert command._extract_bigo_from_source_metadata(source) == 266337398


def test_extract_explicit_bigo_from_text_ignores_noisy_small_candidate():
    command = Command()

    extracted = command._extract_explicit_bigo_from_text(
        "बिगो विवरण 1 प्रतिवादीउपर बिगो रु. २६,६३,३७,३९८ कायम गरी मुद्दा दायर।"
    )

    assert extracted == 266337398


def test_extract_explicit_bigo_from_text_rejects_only_noisy_small_candidate():
    command = Command()

    assert command._extract_explicit_bigo_from_text("बिगो विवरण 1") is None


def test_build_bigo_prompt_includes_source_metadata():
    command = Command()
    case = SimpleNamespace(case_id="case-prompt-source", title="Case")
    source = DocumentSource(
        source_id="source:test:prompt-source",
        title="CIAA Press Release बिगो रु. ३८६,७१७,६४० कायम",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        url=["https://ngm-store.jawafdehi.org/test.pdf"],
    )

    prompt = command._build_bigo_prompt(
        markdown="Converted PDF content",
        case=case,
        source=source,
    )

    assert "Source metadata" in prompt
    assert "बिगो रु. ३८६,७१७,६४० कायम" in prompt
    assert "https://ngm-store.jawafdehi.org/test.pdf" in prompt


def test_extract_explicit_bigo_from_markdown_handles_paisa_and_dotted_rupee_marker():
    command = Command()

    extracted = command._extract_explicit_bigo_from_markdown(
        "प्रतिवादीउपर बिगो रु. ३८,६७,१७,६४०/९० कायम गरी सजाय मागदाबी गरिएको छ।"
    )

    assert extracted == 386717640


def test_log_bigo_snippets_reports_context_when_verbose():
    command = Command()
    command._verbose = True
    out = StringIO()
    command.stdout = out

    command._log_bigo_snippets(
        "कुल आय १००० थियो। प्रतिवादीउपर बिगो रु. २,५०,००० कायम गरी मागदाबी गरिएको।"
    )

    output = out.getvalue()
    assert "BIGO-context markdown snippets" in output
    assert "बिगो रु. 2,50,000" in output


def test_extract_bigo_from_markdown_openai_missing_content_raises_command_error(
    monkeypatch,
):
    command = Command()
    case = SimpleNamespace(case_id="case-openai-001", title="Case")

    class FakeOpenAIClient:
        init_kwargs = {}
        call_kwargs = {}

        def __init__(self, api_key: str, base_url: str, timeout: float):
            self.__class__.init_kwargs = {
                "api_key": api_key,
                "base_url": base_url,
                "timeout": timeout,
            }
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        def _create(self, **kwargs):
            self.__class__.call_kwargs = kwargs
            return SimpleNamespace(choices=[])

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))

    with pytest.raises(CommandError, match="OpenAI-compatible LLM response"):
        command._extract_bigo_from_markdown(
            markdown="sample markdown",
            case=case,
            source=None,
            model="openai:claude-sonnet-4-6",
            anthropic_api_key="test-key",
            min_confidence="low",
            llm_base_url="https://llm-proxy.jawafdehi.org/v1",
            llm_timeout=7,
            llm_max_tokens=1234,
        )

    assert FakeOpenAIClient.call_kwargs["model"] == "openai:claude-sonnet-4-6"
    assert FakeOpenAIClient.call_kwargs["max_tokens"] == 1234
    assert FakeOpenAIClient.call_kwargs["response_format"] == {"type": "json_object"}
    assert FakeOpenAIClient.init_kwargs["timeout"] == 7


def test_extract_bigo_from_markdown_rejects_non_bigo_context_evidence_quote(
    monkeypatch,
):
    command = Command()
    case = SimpleNamespace(case_id="case-openai-002", title="Case")

    class FakeOpenAIClient:
        def __init__(self, api_key: str, base_url: str, timeout: float):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    content='{"bigo": 13000, "confidence": "high", "evidence_quote": "घुस/रिसवत बापत रु.१३,००० माग गरी लिएको"}'
                                )
                            )
                        ]
                    )
                )
            )

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))

    extracted = command._extract_bigo_from_markdown(
        markdown="sample markdown",
        case=case,
        source=None,
        model="openai:claude-sonnet-4-6",
        anthropic_api_key="test-key",
        min_confidence="low",
        llm_base_url="https://llm-proxy.jawafdehi.org/v1",
    )

    assert extracted is None


def test_extract_bigo_from_markdown_accepts_explicit_bigo_context_quote(monkeypatch):
    command = Command()
    case = SimpleNamespace(case_id="case-openai-003", title="Case")

    class FakeOpenAIClient:
        def __init__(self, api_key: str, base_url: str, timeout: float):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    content='{"bigo": 13000, "confidence": "high", "evidence_quote": "बिगो रु.१३,००० कायम गरी मागदाबी"}'
                                )
                            )
                        ]
                    )
                )
            )

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))

    extracted = command._extract_bigo_from_markdown(
        markdown="sample markdown",
        case=case,
        source=None,
        model="openai:claude-sonnet-4-6",
        anthropic_api_key="test-key",
        min_confidence="low",
        llm_base_url="https://llm-proxy.jawafdehi.org/v1",
    )

    assert extracted == 13000


def test_openai_response_text_extracts_dict_message_content():
    command = Command()
    response = {
        "choices": [
            {
                "message": {
                    "content": {
                        "text": '{"bigo": 13000, "confidence": "high", "evidence_quote": "बिगो रु.१३,००० कायम"}'
                    }
                }
            }
        ]
    }

    assert command._openai_response_text(response).startswith('{"bigo": 13000')


def test_openai_response_text_extracts_choice_text():
    command = Command()
    response = {
        "choices": [
            {
                "text": '{"bigo": 25000, "confidence": "high", "evidence_quote": "मागदाबी रु.२५,०००"}'
            }
        ]
    }

    assert command._openai_response_text(response).startswith('{"bigo": 25000')


def test_openai_response_text_error_includes_response_shape():
    command = Command()
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "tool_calls": []},
            }
        ]
    }

    with pytest.raises(CommandError, match="message_keys=.*role"):
        command._openai_response_text(response)


def test_parse_json_response_extracts_fenced_json():
    command = Command()

    parsed = command._parse_json_response(
        'Here is the extraction:\n```json\n{"bigo": 25000, "confidence": "high"}\n```'
    )

    assert parsed == {"bigo": 25000, "confidence": "high"}


def test_parse_json_response_error_includes_preview():
    command = Command()

    with pytest.raises(ValueError, match="Response preview"):
        command._parse_json_response("I could not find a BIGO amount in this text.")


@pytest.mark.django_db
def test_download_source_to_path_enforces_max_bytes_and_cleans_partial_file():
    command = Command()
    source = _create_source(
        source_id="source-test-002",
        title="CIAA Press Release",
        url="https://example.com/press-release.pdf",
    )

    class StreamingResponse:
        def __init__(self, chunks: list[bytes]):
            self._chunks = iter(chunks)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size: int = -1) -> bytes:
            return next(self._chunks, b"")

    with tempfile.TemporaryDirectory(prefix="bigo-test-") as tmp_dir:
        output_dir = Path(tmp_dir)
        out_file = output_dir / "press-release.pdf"

        with (
            patch(
                "cases.management.commands.enrich_missing_bigo.MAX_DOWNLOAD_BYTES",
                16,
                create=True,
            ),
            patch(
                "urllib.request.urlopen",
                return_value=StreamingResponse([b"a" * 8, b"b" * 8, b"c" * 8]),
            ),
        ):
            with pytest.raises(CommandError, match="exceeds max size"):
                command._download_source_to_path(source, output_dir)

        assert not out_file.exists()


@pytest.mark.django_db
def test_download_source_to_path_passes_configured_timeout():
    command = Command()
    source = _create_source(
        source_id="source-test-timeout",
        title="CIAA Press Release",
        url="https://example.com/press-release-timeout.pdf",
    )

    class StreamingResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size: int = -1) -> bytes:
            return b""

    with tempfile.TemporaryDirectory(prefix="bigo-test-") as tmp_dir:
        with patch(
            "urllib.request.urlopen", return_value=StreamingResponse()
        ) as urlopen:
            command._download_source_to_path(source, Path(tmp_dir), timeout=3.5)

    assert urlopen.call_args.kwargs["timeout"] == 3.5


def test_extract_bigo_from_markdown_wraps_openai_403(monkeypatch):
    command = Command()
    case = SimpleNamespace(case_id="case-openai-403", title="Case")

    class FakeOpenAIClient:
        def __init__(self, api_key: str, base_url: str, timeout: float):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        def _create(self, **_kwargs):
            response = SimpleNamespace(status_code=403)
            exc = Exception("Your request was blocked.")
            exc.response = response
            raise exc

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))

    with pytest.raises(CommandError, match="authentication/authorization failed"):
        command._extract_bigo_from_markdown(
            markdown="sample markdown",
            case=case,
            source=None,
            model="openai:gpt-5.4",
            anthropic_api_key="test-key",
            min_confidence="low",
            llm_base_url="https://llm-proxy.jawafdehi.org/v1",
        )
