"""
Tests for encoding_tool.fix_file_encoding functionality.
"""

from pathlib import Path

from case_workflows.encoding_tool import create_fix_encoding_tool, fix_file_encoding


class TestFixFileEncoding:
    """Tests for the fix_file_encoding function."""

    def test_valid_utf8_returns_ok(self, tmp_path):
        """Valid UTF-8 file returns status ok."""
        f = tmp_path / "valid.md"
        f.write_text("# नेपाली कंटेन्ट\nFacts here.", encoding="utf-8")

        result = fix_file_encoding(str(f))

        assert result["status"] == "ok"
        assert result["file_path"] == str(f)
        assert result["encoding"] == "utf-8"
        assert result["bytes_invalid"] == 0

    def test_invalid_utf8_repaired(self, tmp_path):
        """Invalid UTF-8 bytes are recovered and file is rewritten."""
        f = tmp_path / "invalid.md"
        f.write_bytes(b"Valid text: " + bytes([0xBE]) + b" more text")

        result = fix_file_encoding(str(f))

        assert result["status"] == "repaired"
        assert result["file_path"] == str(f)
        assert "byte" in result["details"]
        assert result["bytes_invalid"] > 0

        # File should now be readable as UTF-8
        recovered = f.read_text(encoding="utf-8")
        assert "Valid text" in recovered
        assert "more text" in recovered

    def test_multiple_invalid_bytes(self, tmp_path):
        """Multiple invalid bytes are all replaced."""
        f = tmp_path / "multi_bad.md"
        f.write_bytes(
            b"Start " + bytes([0xBE, 0xAD]) + b" mid " + bytes([0xFE]) + b" end"
        )

        result = fix_file_encoding(str(f))

        assert result["status"] == "repaired"
        assert result["bytes_invalid"] == 3

    def test_nonexistent_file_returns_error(self, tmp_path):
        """Non-existent file returns error status."""
        f = tmp_path / "ghost.md"

        result = fix_file_encoding(str(f))

        assert result["status"] == "error"
        assert "does not exist" in result["details"]

    def test_relative_path_is_resolved(self, tmp_path, monkeypatch):
        """Relative paths are resolved to absolute."""
        f = tmp_path / "test.md"
        f.write_text("नेपाल", encoding="utf-8")

        monkeypatch.chdir(tmp_path)

        result = fix_file_encoding("test.md")

        assert result["status"] == "ok"
        assert Path(result["file_path"]).is_absolute()

    def test_preserves_file_content_semantics(self, tmp_path):
        """After repair, semantic content is preserved."""
        f = tmp_path / "content.md"
        original = "## Important facts\n- Corruption\n- Fraud\n\nDetails..."
        f.write_text(original, encoding="utf-8")

        result = fix_file_encoding(str(f))

        assert result["status"] == "ok"
        recovered = f.read_text(encoding="utf-8")
        assert recovered == original

    def test_allow_base_path_rejects_outside_target(self, tmp_path):
        """Path outside allow_base_path is rejected."""
        inside = tmp_path / "inside"
        inside.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("content", encoding="utf-8")

        result = fix_file_encoding(str(outside), allow_base_path=str(inside))

        assert result["status"] == "error"
        assert "outside allowed base directory" in result["details"]

    def test_allow_base_path_accepts_inside_target(self, tmp_path):
        """Path inside allow_base_path is accepted."""
        base = tmp_path / "case"
        base.mkdir()
        target = base / "draft.md"
        target.write_text("नेपाल", encoding="utf-8")

        result = fix_file_encoding(str(target), allow_base_path=str(base))

        assert result["status"] == "ok"

    def test_tool_uses_env_allowed_work_dir(self, tmp_path, monkeypatch):
        """LangChain tool enforces JAWAFDEHI_ALLOWED_WORK_DIR boundary."""
        base = tmp_path / "case"
        base.mkdir()
        allowed = base / "allowed.md"
        allowed.write_text("ok", encoding="utf-8")
        denied = tmp_path / "denied.md"
        denied.write_text("no", encoding="utf-8")

        monkeypatch.setenv("JAWAFDEHI_ALLOWED_WORK_DIR", str(base))
        tool = create_fix_encoding_tool()

        ok = tool.invoke({"file_path": str(allowed)})
        err = tool.invoke({"file_path": str(denied)})

        assert ok["status"] == "ok"
        assert err["status"] == "error"
        assert "outside allowed base directory" in err["details"]

    def test_tool_returns_error_when_work_dir_unset(self, tmp_path, monkeypatch):
        """LangChain tool returns error status when JAWAFDEHI_ALLOWED_WORK_DIR is absent."""
        f = tmp_path / "test.md"
        f.write_text("hello", encoding="utf-8")

        monkeypatch.delenv("JAWAFDEHI_ALLOWED_WORK_DIR", raising=False)
        tool = create_fix_encoding_tool()

        result = tool.invoke({"file_path": str(f)})

        assert result["status"] == "error"
        assert "JAWAFDEHI_ALLOWED_WORK_DIR" in result["details"]


def test_create_fix_encoding_tool():
    """create_fix_encoding_tool returns a LangChain tool."""
    tool = create_fix_encoding_tool()

    # LangChain tool is not callable but has required attributes
    assert hasattr(tool, "name")
    assert hasattr(tool, "description")
    assert tool.name == "fix_encoding"
    assert "UTF-8" in tool.description
