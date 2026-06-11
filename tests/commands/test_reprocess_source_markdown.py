"""Tests for the reprocess_source_markdown command + shared conversion core."""

import sys
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command, load_command_class

from review import converter
from review.upstream_client import UpstreamClient, UpstreamError


@pytest.fixture(autouse=True)
def _stub_markitdown_if_missing(monkeypatch):
    if "markitdown" not in sys.modules:
        monkeypatch.setitem(sys.modules, "markitdown", MagicMock())


# ── Flag registration ───────────────────────────────────────


@pytest.mark.parametrize(
    "flag", ["--slug", "--limit", "--overwrite", "--dry-run", "--sleep"]
)
def test_cli_flags_registered(flag):
    cmd = load_command_class("review", "reprocess_source_markdown")
    parser = cmd.create_parser("manage.py", "reprocess_source_markdown")
    for action in parser._actions:
        if flag in action.option_strings:
            return
    pytest.fail(f"Flag {flag} not found in command arguments")


# ── Shared converter: convert_case_to_attach_candidates ──────


def _case_with_source(**source_extra):
    """A minimal case dict whose single evidence source carries source_extra."""
    src = {"title": "S1", "source_type": "PDF", "url": ["http://x/a.pdf"]}
    src.update(source_extra)
    return {"evidence": [{"source_id": 11, "description": "d", "source": src}]}


def test_candidates_default_includes_freshly_converted():
    case = _case_with_source()
    with patch.object(
        converter,
        "convert_all",
        return_value=[
            {"source_id": 11, "markdown": "md", "conversion_status": "converted"}
        ],
    ):
        converted, candidates = converter.convert_case_to_attach_candidates(case)
    assert converted and candidates == [{"source_id": 11, "markdown": "md"}]


def test_candidates_default_excludes_source_with_markdown_link():
    """A source that already has a MARKDOWN link is not a candidate by default."""
    case = _case_with_source()
    converted_src = {
        "source_id": 11,
        "markdown": "md",
        "conversion_status": "converted",
        "markdown_url": "http://x/11.md",
    }
    with patch.object(converter, "convert_all", return_value=[converted_src]):
        _, candidates = converter.convert_case_to_attach_candidates(case)
    assert candidates == []


def test_candidates_overwrite_includes_source_with_markdown_link():
    """Load-bearing: --overwrite re-emits a source that already has markdown."""
    case = _case_with_source()
    converted_src = {
        "source_id": 11,
        "markdown": "fresh",
        "conversion_status": "attached",
        "markdown_url": "http://x/11.md",
    }
    with patch.object(converter, "convert_all", return_value=[converted_src]) as ca:
        _, candidates = converter.convert_case_to_attach_candidates(
            case, overwrite=True
        )
    assert candidates == [{"source_id": 11, "markdown": "fresh"}]
    # overwrite is threaded into convert_all.
    assert ca.call_args.kwargs.get("overwrite") is True


def test_candidates_skips_empty_markdown_and_missing_sid():
    case = _case_with_source()
    with patch.object(
        converter,
        "convert_all",
        return_value=[
            {"source_id": 11, "markdown": "   ", "conversion_status": "converted"},
            {"source_id": None, "markdown": "x", "conversion_status": "converted"},
        ],
    ):
        _, candidates = converter.convert_case_to_attach_candidates(case)
    assert candidates == []


# ── convert_source overwrite short-circuit ───────────────────


def test_convert_source_attached_short_circuit():
    res = converter.convert_source({"markdown": "x"})
    assert res["status"] == "attached" and res["markdown"] == "x"


def test_convert_source_overwrite_bypasses_short_circuit(settings, tmp_path):
    """With overwrite, a source carrying markdown still goes through conversion."""
    # Redirect the on-disk markdown cache to a tmp dir so the test never writes
    # into the repo's review_source_markdown/.
    settings.SOURCE_MARKDOWN_DIR = tmp_path
    src = {"markdown": "old", "url": ["http://x/a.pdf"]}
    with patch.object(
        converter.jds_client,
        "download_source_file",
        return_value=(b"pdf", "application/pdf"),
    ), patch.object(converter, "_markitdown") as md, patch.object(
        converter, "_patch_likhit_ocr_dpi"
    ):
        md.return_value.convert.return_value = MagicMock(text_content="new md")
        res = converter.convert_source(src, overwrite=True)
    assert res["status"] == "converted" and res["markdown"] == "new md"


def test_convert_source_overwrite_bypasses_disk_cache(settings, tmp_path):
    """overwrite must re-convert even when a stale cache file exists for the url.

    Regression guard: the cache is keyed by url (not converter version), so
    refreshing markdown after a converter change must NOT re-serve the cache.
    """
    settings.SOURCE_MARKDOWN_DIR = tmp_path
    url = "http://x/a.pdf"
    import hashlib

    cache_path = tmp_path / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.md"
    cache_path.write_text("stale cached", encoding="utf-8")

    src = {"url": [url]}
    with patch.object(
        converter.jds_client,
        "download_source_file",
        return_value=(b"pdf", "application/pdf"),
    ), patch.object(converter, "_markitdown") as md, patch.object(
        converter, "_patch_likhit_ocr_dpi"
    ):
        md.return_value.convert.return_value = MagicMock(text_content="fresh")
        res = converter.convert_source(src, overwrite=True)
    assert res["markdown"] == "fresh"
    # And the fresh result is written back to the cache.
    assert cache_path.read_text(encoding="utf-8") == "fresh"

    # Without overwrite, the cache is served as-is.
    with patch.object(converter.jds_client, "download_source_file") as dl:
        res2 = converter.convert_source(src)
    assert res2["markdown"] == "fresh" and dl.call_count == 0


# ── Command: dry-run does not write upstream ─────────────────


def test_dry_run_does_not_attach():
    case = _case_with_source()
    with patch.object(
        jds_client_in_cmd(), "iter_paginated", return_value=[{"slug": "c1"}]
    ), patch.object(jds_client_in_cmd(), "get_case", return_value=case), patch.object(
        converter,
        "convert_case_to_attach_candidates",
        return_value=(
            [{"conversion_status": "converted"}],
            [{"source_id": 11, "markdown": "md"}],
        ),
    ), patch.object(
        UpstreamClient, "attach_markdown"
    ) as attach:
        call_command("reprocess_source_markdown", "--dry-run")
    attach.assert_not_called()


def test_live_run_attaches_via_client(settings):
    settings.CASEWORK_POLLER_TOKEN = "tok"
    case = _case_with_source()
    with patch.object(
        jds_client_in_cmd(), "iter_paginated", return_value=[{"slug": "c1"}]
    ), patch.object(jds_client_in_cmd(), "get_case", return_value=case), patch.object(
        converter,
        "convert_case_to_attach_candidates",
        return_value=(
            [{"conversion_status": "converted"}],
            [{"source_id": 11, "markdown": "md"}],
        ),
    ), patch.object(
        UpstreamClient,
        "attach_markdown",
        return_value={"attached": 1, "skipped": 0, "failed": 0},
    ) as attach:
        call_command("reprocess_source_markdown")
    attach.assert_called_once()
    items, kwargs = attach.call_args.args[0], attach.call_args.kwargs
    assert items == [{"source_id": 11, "markdown": "md"}]
    assert kwargs.get("overwrite") is False


def test_explicit_slugs_skip_listing(settings):
    settings.CASEWORK_POLLER_TOKEN = "tok"
    case = _case_with_source()
    with patch.object(jds_client_in_cmd(), "iter_paginated") as it, patch.object(
        jds_client_in_cmd(), "get_case", return_value=case
    ) as gc, patch.object(
        converter, "convert_case_to_attach_candidates", return_value=([], [])
    ), patch.object(
        UpstreamClient,
        "attach_markdown",
        return_value={"attached": 0, "skipped": 0, "failed": 0},
    ):
        call_command("reprocess_source_markdown", "--slug", "a", "--slug", "b")
    it.assert_not_called()
    assert gc.call_count == 2


# ── UpstreamClient ───────────────────────────────────────────


def test_upstream_client_requires_token(settings):
    settings.CASEWORK_POLLER_TOKEN = ""
    with pytest.raises(UpstreamError):
        UpstreamClient()


def test_upstream_client_attach_markdown_summary():
    client = UpstreamClient(token="tok")
    created = MagicMock(status_code=200)
    created.json.return_value = {"created": True}
    skipped = MagicMock(status_code=200)
    skipped.json.return_value = {"created": False}
    with patch.object(client, "post", side_effect=[created, skipped]):
        summary = client.attach_markdown(
            [{"source_id": 1, "markdown": "a"}, {"source_id": 2, "markdown": "b"}]
        )
    assert summary == {"attached": 1, "skipped": 1, "failed": 0}


def jds_client_in_cmd():
    """The jds_client module as imported by the command (one shared instance)."""
    from review import jds_client

    return jds_client
