"""
Tests for case_workflows.storage_utils.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from case_workflows.storage_utils import (
    _EXCLUDED_DIRS,
    _EXCLUDED_FILENAMES,
    _WORKFLOW_OUTPUTS_PREFIX,
    build_file_record,
    compute_sha256,
    record_downloaded_files,
    upload_workflow_outputs,
)

# ---------------------------------------------------------------------------
# compute_sha256
# ---------------------------------------------------------------------------


class TestComputeSha256:
    def test_matches_hashlib_directly(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello nepal")

        expected = "sha256:" + hashlib.sha256(b"hello nepal").hexdigest()
        assert compute_sha256(f) == expected

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")

        expected = "sha256:" + hashlib.sha256(b"").hexdigest()
        assert compute_sha256(f) == expected

    def test_large_file_chunked(self, tmp_path):
        """Verifies streaming chunking produces the correct digest."""
        data = b"x" * 200_000  # > one 65536-byte chunk
        f = tmp_path / "large.bin"
        f.write_bytes(data)

        expected = "sha256:" + hashlib.sha256(data).hexdigest()
        assert compute_sha256(f) == expected


# ---------------------------------------------------------------------------
# build_file_record
# ---------------------------------------------------------------------------


class TestBuildFileRecord:
    def test_record_structure(self, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_bytes(b"Ramesh Hamal ko case")

        record = build_file_record(f, "workflow-outputs/case-xyz/report.pdf")

        assert record["backend_path"] == "workflow-outputs/case-xyz/report.pdf"
        assert record["size"] == len(b"Ramesh Hamal ko case")
        assert record["checksum"].startswith("sha256:")
        digest = record["checksum"][len("sha256:") :]
        assert digest == hashlib.sha256(b"Ramesh Hamal ko case").hexdigest()


# ---------------------------------------------------------------------------
# upload_workflow_outputs
# ---------------------------------------------------------------------------


class TestUploadWorkflowOutputs:
    """Uses Django's default_storage mock to avoid real I/O to S3 or disk."""

    def _populate_dir(self, base: Path) -> list[Path]:
        """Create a realistic work directory structure."""
        files = []
        for name, content in [
            ("prd.json", b'{"project": "Jawafdehi"}'),
            ("progress.json", b'{"is_complete": false}'),
            ("logs/run-summary-20260405T120000.stdout.md", b"Agent output here"),
            ("sources/raw/ciaa-press.pdf", b"%PDF-1.4 binary content"),
            ("sources/markdown/ciaa-press.md", b"# CIAA Press Release\nContent"),
        ]:
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            files.append(path)
        return files

    def test_excludes_prd_and_progress_json(self, tmp_path):
        self._populate_dir(tmp_path)

        saved_names: list[str] = []

        mock_storage = MagicMock()
        mock_storage.save.side_effect = lambda name, _fh: (
            saved_names.append(name) or name
        )

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = upload_workflow_outputs(tmp_path, "case-testcase01")

        uploaded_keys = set(result.keys())
        assert "prd.json" not in uploaded_keys
        assert "progress.json" not in uploaded_keys

    def test_uploads_other_files(self, tmp_path):
        self._populate_dir(tmp_path)

        mock_storage = MagicMock()
        mock_storage.save.side_effect = lambda name, _fh: name

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = upload_workflow_outputs(tmp_path, "case-testcase01")

        # Should have three files (log + two sources)
        assert len(result) == 3
        keys = set(result.keys())
        assert "logs/run-summary-20260405T120000.stdout.md" in keys
        assert "sources/raw/ciaa-press.pdf" in keys
        assert "sources/markdown/ciaa-press.md" in keys

    def test_storage_paths_use_prefix_and_case_id(self, tmp_path):
        f = tmp_path / "output.txt"
        f.write_text("नेपाल")

        mock_storage = MagicMock()
        mock_storage.save.side_effect = lambda name, _fh: name

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = upload_workflow_outputs(tmp_path, "case-abc123")

        saved_record = list(result.values())[0]
        assert saved_record["backend_path"].startswith(
            f"{_WORKFLOW_OUTPUTS_PREFIX}/case-abc123/"
        )
        assert saved_record["backend_path"].endswith("output.txt")

    def test_upload_error_is_skipped_not_raised(self, tmp_path):
        f = tmp_path / "good.txt"
        f.write_text("ok")
        bad = tmp_path / "bad.txt"
        bad.write_text("will fail")

        mock_storage = MagicMock()

        def side_effect(name, _fh):
            if "bad" in name:
                raise OSError("permission denied")
            return name

        mock_storage.save.side_effect = side_effect

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = upload_workflow_outputs(tmp_path, "case-x")

        # Only the good file is in the result; the bad file error was swallowed
        assert len(result) == 1
        assert any("good.txt" in k for k in result)

    def test_empty_directory_returns_empty_dict(self, tmp_path):
        mock_storage = MagicMock()

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = upload_workflow_outputs(tmp_path, "case-empty")

        assert result == {}
        mock_storage.save.assert_not_called()

    def test_all_excluded_filenames_are_skipped(self, tmp_path):
        for name in _EXCLUDED_FILENAMES:
            (tmp_path / name).write_text("{}")

        mock_storage = MagicMock()

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = upload_workflow_outputs(tmp_path, "case-x")

        assert result == {}

    def test_excluded_dirs_are_skipped(self, tmp_path):
        for dir_name in _EXCLUDED_DIRS:
            excluded_file = tmp_path / dir_name / "some_file.csv"
            excluded_file.parent.mkdir(parents=True, exist_ok=True)
            excluded_file.write_text("data")
        kept = tmp_path / "logs" / "run.md"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text("log")

        mock_storage = MagicMock()
        mock_storage.save.side_effect = lambda name, _fh: name

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = upload_workflow_outputs(tmp_path, "case-x")

        assert all(k.startswith("logs") for k in result)
        for dir_name in _EXCLUDED_DIRS:
            assert not any(k.startswith(dir_name) for k in result)

    def test_existing_file_is_deleted_before_save(self, tmp_path):
        """Re-running a workflow should overwrite the existing backend file, not suffix it."""
        f = tmp_path / "draft.md"
        f.write_text("updated content")

        mock_storage = MagicMock()
        mock_storage.exists.return_value = True
        mock_storage.save.side_effect = lambda name, _fh: name

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = upload_workflow_outputs(tmp_path, "case-abc123")

        expected_path = f"{_WORKFLOW_OUTPUTS_PREFIX}/case-abc123/draft.md"
        mock_storage.delete.assert_called_once_with(expected_path)
        mock_storage.save.assert_called_once()
        assert "draft.md" in result


# ---------------------------------------------------------------------------
# record_downloaded_files
# ---------------------------------------------------------------------------


class TestRecordDownloadedFiles:
    def test_adds_files_dict_if_missing(self, tmp_path):
        f = tmp_path / "ciaa.pdf"
        f.write_bytes(b"PDF content")

        progress_data = {"is_complete": False, "progress": []}

        mock_storage = MagicMock()
        mock_storage.save.side_effect = lambda name, _fh: name

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = record_downloaded_files(
                progress_data, "case-abc123", tmp_path, [f]
            )

        assert "files" in result
        assert "ciaa.pdf" in result["files"]

    def test_file_record_has_required_keys(self, tmp_path):
        content = b"Bikash Thapa \xe0\xa4\x95\xe0\xa5\x8b \xe0\xa4\xab\xe0\xa4\xbe\xe0\xa4\x87\xe0\xa4\xb2"
        f = tmp_path / "charge-sheet.pdf"
        f.write_bytes(content)

        progress_data: dict = {}

        mock_storage = MagicMock()
        mock_storage.save.side_effect = lambda name, _fh: name

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = record_downloaded_files(progress_data, "case-test", tmp_path, [f])

        record = result["files"]["charge-sheet.pdf"]
        assert "backend_path" in record
        assert "case-test" in record["backend_path"]
        assert "/sources/" in record["backend_path"]
        assert "size" in record
        assert record["size"] == len(content)
        assert "checksum" in record
        assert record["checksum"].startswith("sha256:")

    def test_non_existent_file_is_skipped(self, tmp_path):
        ghost = tmp_path / "ghost.pdf"  # does not exist

        progress_data: dict = {}

        with patch("case_workflows.storage_utils.default_storage", MagicMock()):
            result = record_downloaded_files(
                progress_data, "case-skip", tmp_path, [ghost]
            )

        assert result.get("files", {}) == {}

    def test_mutates_existing_files_dict(self, tmp_path):
        f = tmp_path / "new.pdf"
        f.write_bytes(b"new")

        progress_data = {
            "files": {
                "old.pdf": {
                    "backend_path": "workflow-outputs/case-x/old.pdf",
                    "size": 3,
                    "checksum": "sha256:abc",
                }
            }
        }

        mock_storage = MagicMock()
        mock_storage.save.side_effect = lambda name, _fh: name

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = record_downloaded_files(progress_data, "case-mut", tmp_path, [f])

        assert "old.pdf" in result["files"]
        assert "new.pdf" in result["files"]

    def test_upload_fallback_on_storage_error(self, tmp_path):
        """If default_storage.save fails, the record still uses the intended path."""
        f = tmp_path / "fallback.pdf"
        f.write_bytes(b"content")

        mock_storage = MagicMock()
        mock_storage.save.side_effect = OSError("S3 unreachable")

        progress_data: dict = {}

        with patch("case_workflows.storage_utils.default_storage", mock_storage):
            result = record_downloaded_files(progress_data, "case-err", tmp_path, [f])

        # Record should still exist (with intended path as fallback)
        record = result["files"].get("fallback.pdf")
        assert record is not None
        assert "backend_path" in record
        assert "case-err" in record["backend_path"]


# ---------------------------------------------------------------------------
# read_draft_md_safe
# ---------------------------------------------------------------------------


class TestReadDraftMdSafe:
    """Tests for safe draft.md reading with auto-repair."""

    def test_reads_valid_utf8_draft(self, tmp_path):
        from case_workflows.storage_utils import read_draft_md_safe

        draft = tmp_path / "draft.md"
        draft.write_text("# Case Title\n\nNepali content: नेपाल", encoding="utf-8")

        result = read_draft_md_safe(draft)

        assert "Case Title" in result
        assert "नेपाल" in result

    def test_auto_repairs_invalid_utf8_bytes(self, tmp_path):
        from case_workflows.storage_utils import read_draft_md_safe

        draft = tmp_path / "draft.md"
        bad_content = (
            b"The defendant is charged: " + bytes([0xBE]) + b" with corruption."
        )
        draft.write_bytes(bad_content)

        result = read_draft_md_safe(draft)

        # Should recover the readable text with replacement character
        assert "defendant" in result
        assert "corruption" in result
        # File should now be valid UTF-8
        repaired_content = draft.read_text(encoding="utf-8")
        assert repaired_content is not None

    def test_rewrites_file_with_valid_utf8(self, tmp_path):
        from case_workflows.storage_utils import read_draft_md_safe

        draft = tmp_path / "draft.md"
        bad_bytes = b"prefix " + bytes([0xBE]) + b" suffix"
        draft.write_bytes(bad_bytes)

        read_draft_md_safe(draft)

        # After read, file should be readable without errors
        try:
            recovered = draft.read_text(encoding="utf-8")
            assert "prefix" in recovered
            assert "suffix" in recovered
        except UnicodeDecodeError:
            pytest.fail("draft.md should be valid UTF-8 after auto-repair")
