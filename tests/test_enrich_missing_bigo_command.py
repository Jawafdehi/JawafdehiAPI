import tempfile
from pathlib import Path
from io import StringIO
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
