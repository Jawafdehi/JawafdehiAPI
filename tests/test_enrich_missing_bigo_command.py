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
def test_dry_run_does_not_patch_cases():
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
            "--dry-run",
            "--api-token",
            "test-token",
            "--anthropic-api-key",
            "test-key",
            stdout=out,
        )

    patch_case.assert_not_called()
    assert "DRY-RUN" in out.getvalue()


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


def test_validate_url_scheme_allows_http_and_https_only():
    command = Command()

    assert (
        command._validate_url_scheme("https://example.com/press-release.pdf")
        == "https://example.com/press-release.pdf"
    )
    assert (
        command._validate_url_scheme("http://example.com/press-release.pdf")
        == "http://example.com/press-release.pdf"
    )

    with pytest.raises(ValueError, match="Only http and https URLs are allowed"):
        command._validate_url_scheme("file:///tmp/press-release.pdf")

    with pytest.raises(ValueError, match="Only http and https URLs are allowed"):
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


def test_case_patch_url_rejects_non_http_base_url():
    command = Command()

    with pytest.raises(ValueError, match="http or https"):
        command._case_patch_url("ftp://example.com", 42)

    with pytest.raises(ValueError, match="must include a host"):
        command._case_patch_url("https:///api", 42)


def test_extract_bigo_from_markdown_openai_missing_content_raises_command_error(
    monkeypatch,
):
    command = Command()
    case = SimpleNamespace(case_id="case-openai-001", title="Case")

    class FakeOpenAIClient:
        def __init__(self, api_key: str, base_url: str):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(choices=[])
                )
            )

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))

    with pytest.raises(CommandError, match="OpenAI-compatible LLM response"):
        command._extract_bigo_from_markdown(
            markdown="sample markdown",
            case=case,
            model="openai:claude-sonnet-4-6",
            anthropic_api_key="test-key",
            min_confidence="low",
            llm_base_url="https://llm-proxy.jawafdehi.org/v1",
        )


def test_extract_bigo_from_markdown_rejects_non_bigo_context_evidence_quote(
    monkeypatch,
):
    command = Command()
    case = SimpleNamespace(case_id="case-openai-002", title="Case")

    class FakeOpenAIClient:
        def __init__(self, api_key: str, base_url: str):
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
        def __init__(self, api_key: str, base_url: str):
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
        model="openai:claude-sonnet-4-6",
        anthropic_api_key="test-key",
        min_confidence="low",
        llm_base_url="https://llm-proxy.jawafdehi.org/v1",
    )

    assert extracted == 13000


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
