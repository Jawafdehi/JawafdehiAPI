"""Tests for the human review file (casework/common/review.py).

This file is the accuracy deliverable of every enricher run, so the things
worth pinning are the ones that would quietly make it useless: escaped
Devanagari, a case silently absent from it, a generated value's own Markdown
headings swallowing the file structure, and any wording that would overclaim
where a passage came from.
"""
import argparse

import pytest

from casework.common.cli import add_common_args
from casework.common.review import (
    EXCERPT_CHARS,
    ReviewFile,
    ReviewRow,
    _quote,
    build_review_file,
    review_path,
)


def _file(tmp_path, **kw):
    kw.setdefault("stage", "description")
    kw.setdefault("field_name", "description")
    kw.setdefault("path", tmp_path / "review.md")
    return ReviewFile(**kw)


def test_devanagari_is_written_unescaped(tmp_path):
    """A review file full of `\\u0915` cannot be reviewed."""
    rf = _file(tmp_path)
    rf.add(ReviewRow(slug="case-1", status="would-enrich",
                     generated="ठेक्कामा भ्रष्टाचार भएको।"))
    text = rf.write().read_text(encoding="utf-8")
    assert "ठेक्कामा भ्रष्टाचार भएको।" in text
    assert "\\u0920" not in text


def test_every_row_appears_in_the_summary_table(tmp_path):
    """A case missing from the file reads as "not selected", not "could not
    run" -- so unmet and already-done cases get a row too."""
    rf = _file(tmp_path)
    rf.add(ReviewRow(slug="case-unmet", status="unmet", note="no MARKDOWN role"))
    rf.add(ReviewRow(slug="case-already", status="already", before="क" * 900))
    rf.add(ReviewRow(slug="case-new", status="would-enrich", generated="नयाँ"))
    text = rf.write().read_text(encoding="utf-8")
    for slug in ("case-unmet", "case-already", "case-new"):
        assert f"`{slug}`" in text
    assert "no MARKDOWN role" in text


def test_generated_markdown_headings_are_quoted_not_promoted(tmp_path):
    """The generated value is itself Markdown (`### क) …`). Pasted raw, its
    headings become headings of the review file and the per-case structure
    collapses."""
    rf = _file(tmp_path)
    rf.add(ReviewRow(slug="case-1", status="would-enrich",
                     generated="### क) अभियोगदावीको सार\nविवरण।"))
    text = rf.write().read_text(encoding="utf-8")
    assert "> ### क) अभियोगदावीको सार" in text
    assert "\n### क) अभियोगदावीको सार" not in text


def test_a_source_records_its_material_iri(tmp_path):
    rf = _file(tmp_path)
    rf.add(ReviewRow(
        slug="case-1", status="would-enrich", generated="विवरण",
        sources=[("press_release", "https://jawafdehi.org/material/ciaa/12345",
                  "अख्तियारको विज्ञप्ति")],
    ))
    text = rf.write().read_text(encoding="utf-8")
    assert "https://jawafdehi.org/material/ciaa/12345" in text
    assert "अख्तियारको विज्ञप्ति" in text


def test_a_long_source_is_excerpted_and_says_so(tmp_path):
    rf = _file(tmp_path)
    rf.add(ReviewRow(
        slug="case-1", status="would-enrich", generated="विवरण",
        sources=[("court_order", "iri-c", "फ" * (EXCERPT_CHARS + 500))],
    ))
    text = rf.write().read_text(encoding="utf-8")
    assert f"first {EXCERPT_CHARS:,}" in text
    assert f"{EXCERPT_CHARS + 500:,} chars" in text


def test_a_short_source_carries_no_truncation_note(tmp_path):
    """A whole source must not be reported as an excerpt -- a reviewer who
    thinks they are seeing a fragment will not treat a missing figure as a
    real omission."""
    rf = _file(tmp_path)
    rf.add(ReviewRow(slug="case-1", status="would-enrich", generated="विवरण",
                     sources=[("press_release", "iri-p", "छोटो पाठ")]))
    text = rf.write().read_text(encoding="utf-8")
    assert "— excerpt, first" not in text
    assert "(8 chars)" in text


def test_sources_are_labelled_as_fed_to_the_model(tmp_path):
    """A completion does not report which sentences it drew on. Presenting the
    excerpt as the passage the model quoted would be a fabricated provenance
    claim in the artefact whose whole job is checking for fabrication."""
    rf = _file(tmp_path)
    rf.add(ReviewRow(slug="case-1", status="would-enrich", generated="विवरण",
                     sources=[("press_release", "iri-p", "पाठ")]))
    text = rf.write().read_text(encoding="utf-8")
    assert "### Sources fed to the model" in text
    assert "the text FED to the model, not a span the model reported quoting" in text


def test_an_empty_before_value_is_shown_as_empty_not_blank(tmp_path):
    """"(empty)" and "a value that rendered as nothing" must not look alike."""
    rf = _file(tmp_path)
    rf.add(ReviewRow(slug="case-1", status="would-enrich", before="", generated="नयाँ"))
    text = rf.write().read_text(encoding="utf-8")
    assert "_(empty)_" in text


def test_dry_run_and_applied_are_visibly_different(tmp_path):
    dry = _file(tmp_path, path=tmp_path / "dry.md", dry_run=True).render()
    applied = _file(tmp_path, path=tmp_path / "applied.md", dry_run=False).render()
    assert "DRY RUN" in dry
    assert "nothing was written" in dry
    assert "DRY RUN" not in applied
    assert "APPLIED" in applied


def test_write_creates_missing_parent_directories(tmp_path):
    rf = _file(tmp_path, path=tmp_path / "a" / "b" / "review.md")
    rf.add(ReviewRow(slug="case-1", status="would-enrich", generated="विवरण"))
    assert rf.write().exists()


def test_a_run_with_no_rows_still_renders(tmp_path):
    text = _file(tmp_path).write().read_text(encoding="utf-8")
    assert "Cases: 0" in text


def test_quote_keeps_blank_lines_inside_the_quote(tmp_path):
    """A blank line rendered as an unprefixed empty line ENDS the blockquote,
    so the rest of a multi-paragraph description escapes it."""
    assert _quote("क\n\nख") == "> क\n>\n> ख"


def test_quote_of_blank_input_is_empty():
    assert _quote("") == ""
    assert _quote(None) == ""


# --------------------------------------------------------------------------
# review_path / build_review_file
# --------------------------------------------------------------------------


def test_review_path_prefers_an_explicit_override(tmp_path):
    target = tmp_path / "task-dir" / "review.md"
    assert review_path("description", "abc123", str(target)) == target


def test_review_path_honours_the_env_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CASEWORK_REVIEW_DIR", str(tmp_path / "reviews"))
    path = review_path("description", "abc123")
    assert path.parent == tmp_path / "reviews"
    assert path.name.endswith("-description-abc123.md")


def test_review_path_falls_back_to_the_repo_work_dir(monkeypatch):
    monkeypatch.delenv("CASEWORK_REVIEW_DIR", raising=False)
    path = review_path("description", "abc123")
    # `work/` is gitignored, so the default can never commit generated prose.
    assert path.parent.parts[-2:] == ("work", "reviews")


def test_build_review_file_reads_the_cli_flags(tmp_path):
    ap = add_common_args(argparse.ArgumentParser())
    args = ap.parse_args([
        "--api-base-url", "http://127.0.0.1:48010",
        "--review-file", str(tmp_path / "out.md"),
        "--provider", "claude_cli",
    ])
    rf = build_review_file(args, stage="description", field_name="description",
                           run_id="r1")
    assert rf.path == tmp_path / "out.md"
    assert rf.dry_run is True          # --dry-run is the default
    assert rf.base_url == "http://127.0.0.1:48010"
    assert rf.provider == "claude_cli"


def test_build_review_file_marks_apply_runs(tmp_path):
    ap = add_common_args(argparse.ArgumentParser())
    args = ap.parse_args(["--apply", "--review-file", str(tmp_path / "out.md")])
    rf = build_review_file(args, stage="description", field_name="description",
                           run_id="r1")
    assert rf.dry_run is False


@pytest.mark.parametrize("model,expected", [("", "(provider default)"), ("opus", "opus")])
def test_the_header_names_the_model_or_says_it_defaulted(tmp_path, model, expected):
    rf = _file(tmp_path, model=model, provider="claude_cli")
    assert expected in rf.render()
