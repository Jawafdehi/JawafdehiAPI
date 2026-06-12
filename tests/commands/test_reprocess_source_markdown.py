"""Tests for the reprocess_source_markdown command + shared conversion core."""

import sys
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command, load_command_class

from review import converter, jds_client
from review.upstream_client import UpstreamClient, UpstreamError


@pytest.fixture(autouse=True)
def _stub_markitdown_if_missing(monkeypatch):
    if "markitdown" not in sys.modules:
        monkeypatch.setitem(sys.modules, "markitdown", MagicMock())


# ── Flag registration ───────────────────────────────────────


@pytest.mark.parametrize(
    "flag",
    ["--slug", "--limit", "--overwrite", "--dry-run", "--sleep", "--read-sleep"],
)
def test_cli_flags_registered(flag):
    cmd = load_command_class("review", "reprocess_source_markdown")
    parser = cmd.create_parser("manage.py", "reprocess_source_markdown")
    for action in parser._actions:
        if flag in action.option_strings:
            return
    pytest.fail(f"Flag {flag} not found in command arguments")


# ── jds_client retry / backoff on rate-limit (429) ───────────


def _resp(status, body=None, headers=None):
    m = MagicMock()
    m.status_code = status
    m.headers = headers or {}
    m.json.return_value = body or {}
    return m


def test_get_case_retries_on_429_then_succeeds():
    with patch.object(
        jds_client.requests,
        "get",
        side_effect=[
            _resp(429, headers={"Retry-After": "0"}),
            _resp(200, {"slug": "x"}),
        ],
    ) as get, patch.object(jds_client.time, "sleep") as slept:
        out = jds_client.get_case("x")
    assert out == {"slug": "x"}
    assert get.call_count == 2
    assert slept.called


def test_get_case_raises_after_exhausting_retries(settings):
    settings.JDS_MAX_RETRIES = 2
    with patch.object(
        jds_client.requests,
        "get",
        return_value=_resp(429, headers={"Retry-After": "0"}),
    ) as get, patch.object(jds_client.time, "sleep"):
        with pytest.raises(jds_client.JdsError) as exc:
            jds_client.get_case("y")
    assert "429" in str(exc.value)
    assert get.call_count == 3  # initial + 2 retries


def test_retry_after_header_is_honored():
    assert (
        jds_client._retry_after_seconds(
            _resp(429, headers={"Retry-After": "3"}), 0, 1.0
        )
        == 3.0
    )


def test_backoff_is_exponential_without_header():
    # base 1.0 * 2**attempt
    assert jds_client._retry_after_seconds(_resp(429, headers={}), 0, 1.0) == 1.0
    assert jds_client._retry_after_seconds(_resp(429, headers={}), 2, 1.0) == 4.0
    # capped at 60s
    assert jds_client._retry_after_seconds(_resp(429, headers={}), 10, 1.0) == 60.0


def test_non_retryable_status_returns_immediately():
    with patch.object(
        jds_client.requests, "get", return_value=_resp(404)
    ) as get, patch.object(jds_client.time, "sleep") as slept:
        with pytest.raises(jds_client.JdsError):
            jds_client.get_case("missing")
    assert get.call_count == 1  # 404 is not retried
    assert not slept.called


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
    # Output is frontmatter-wrapped; the converted body must be present and the
    # short-circuit bypassed (we re-converted "new md", not re-served "old").
    assert res["status"] == "converted"
    assert res["markdown"].startswith("---\n") and "processed_at:" in res["markdown"]
    assert res["markdown"].rstrip().endswith("new md")


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
    assert res["markdown"].rstrip().endswith("fresh")
    # The cache holds the bare, frontmatter-free body (timestamp is stamped fresh
    # on each emit, not cached).
    assert cache_path.read_text(encoding="utf-8") == "fresh"

    # Without overwrite, the cached body is re-served (no re-download) and
    # re-wrapped with fresh frontmatter.
    with patch.object(converter.jds_client, "download_source_file") as dl:
        res2 = converter.convert_source(src)
    assert res2["markdown"].rstrip().endswith("fresh") and dl.call_count == 0
    assert "processed_at:" in res2["markdown"]


# ── Role-aware link selection ────────────────────────────────


def _pick(urls=None, url=None):
    src = {}
    if urls is not None:
        src["urls"] = urls
    if url is not None:
        src["url"] = url
    return converter._pick_source_link(src)


def test_pick_raw_when_only_raw():
    assert _pick([{"link": "http://x/a.pdf", "role": "RAW"}]) == (
        "http://x/a.pdf",
        "RAW",
    )


def test_pick_prefers_alternate_when_better_format():
    # ALTERNATE .docx beats RAW .pdf (docx ranks higher).
    assert _pick(
        [
            {"link": "http://x/a.pdf", "role": "RAW"},
            {"link": "http://x/a.docx", "role": "ALTERNATE"},
        ]
    ) == ("http://x/a.docx", "ALTERNATE")


def test_pick_keeps_raw_when_alternate_not_better():
    # RAW .docx stays even though an ALTERNATE .pdf exists (pdf ranks lower).
    assert _pick(
        [
            {"link": "http://x/a.docx", "role": "RAW"},
            {"link": "http://x/a.pdf", "role": "ALTERNATE"},
        ]
    ) == ("http://x/a.docx", "RAW")
    # Equal rank -> keep RAW.
    assert _pick(
        [
            {"link": "http://x/raw.pdf", "role": "RAW"},
            {"link": "http://x/alt.pdf", "role": "ALTERNATE"},
        ]
    ) == ("http://x/raw.pdf", "RAW")


def test_pick_source_page_only_as_fallback():
    # RAW present -> SOURCE_PAGE ignored.
    assert _pick(
        [
            {"link": "http://x/page", "role": "SOURCE_PAGE"},
            {"link": "http://x/a.pdf", "role": "RAW"},
        ]
    ) == ("http://x/a.pdf", "RAW")
    # Only SOURCE_PAGE -> use it.
    assert _pick([{"link": "http://x/page", "role": "SOURCE_PAGE"}]) == (
        "http://x/page",
        "SOURCE_PAGE",
    )


def test_pick_skips_markdown_and_permalink():
    assert _pick(
        [
            {"link": "http://x/a.md", "role": "MARKDOWN"},
            {"link": "http://x/p", "role": "PERMALINK"},
        ]
    ) == (None, None)


def test_pick_falls_back_to_legacy_url_strings():
    assert _pick(url=["http://x/legacy.pdf"]) == ("http://x/legacy.pdf", "RAW")


# ── Internal R2 endpoint → public host normalization ─────────

_R2_BAD = (
    "https://4c96557d73194d4f245ba23bd6063ad5.r2.cloudflarestorage.com/"
    "jawafdehi/case_uploads/9121ea874182ef01e7eba0409168b8f60ae60c2881946c1b131d4054bb920c1c.pdf"
)
_R2_PUBLIC = (
    "https://s3.jawafdehi.org/case_uploads/"
    "9121ea874182ef01e7eba0409168b8f60ae60c2881946c1b131d4054bb920c1c.pdf"
)


def test_normalize_media_url_rewrites_internal_r2_endpoint():
    assert converter._normalize_media_url(_R2_BAD) == _R2_PUBLIC


def test_normalize_media_url_leaves_other_urls_untouched():
    assert converter._normalize_media_url(_R2_PUBLIC) == _R2_PUBLIC
    assert (
        converter._normalize_media_url("https://lawcommission.gov.np/content/13421/")
        == "https://lawcommission.gov.np/content/13421/"
    )
    assert converter._normalize_media_url(None) is None
    assert converter._normalize_media_url("") == ""


def test_pick_normalizes_internal_r2_raw_link():
    # The real prod data shape: a broken internal-R2 RAW link listed first,
    # with the public sibling second. The picker must return a fetchable url.
    url, role = _pick(
        [
            {"link": _R2_BAD, "role": "RAW"},
            {"link": _R2_PUBLIC, "role": "RAW"},
        ]
    )
    assert url == _R2_PUBLIC and role == "RAW"
    # Even if the internal-R2 link is the ONLY RAW link, it is normalized.
    assert _pick([{"link": _R2_BAD, "role": "RAW"}]) == (_R2_PUBLIC, "RAW")


# ── HTML main-content extraction (chrome removal) ────────────


def test_source_page_uses_html_extraction(settings, tmp_path):
    """A SOURCE_PAGE (HTML) link is run through main-content extraction, not the
    plain MarkItDown PDF/doc path."""
    settings.SOURCE_MARKDOWN_DIR = tmp_path
    src = {
        "source_id": "p1",
        "title": "Press release",
        "urls": [{"link": "http://x/page", "role": "SOURCE_PAGE"}],
    }
    with patch.object(
        converter.jds_client,
        "download_source_file",
        return_value=(
            b"<html><body><nav>MENU</nav><article>Real body</article></body></html>",
            "text/html",
        ),
    ), patch.object(
        converter, "_html_to_markdown", return_value="Real body"
    ) as h2m, patch.object(
        converter, "_markitdown"
    ) as md:
        res = converter.convert_source(src, overwrite=True)
    h2m.assert_called_once()
    md.assert_not_called()  # HTML path used; MarkItDown not invoked
    assert "trafilatura" in res["note"]
    assert res["markdown"].rstrip().endswith("Real body")


def test_source_page_falls_back_to_markitdown_when_extraction_empty(settings, tmp_path):
    """If main-content extraction finds nothing, fall back to MarkItDown."""
    settings.SOURCE_MARKDOWN_DIR = tmp_path
    src = {
        "source_id": "p2",
        "title": "Empty page",
        "urls": [{"link": "http://x/page.html", "role": "SOURCE_PAGE"}],
    }
    with patch.object(
        converter.jds_client,
        "download_source_file",
        return_value=(b"<html></html>", "text/html"),
    ), patch.object(converter, "_html_to_markdown", return_value=""), patch.object(
        converter, "_markitdown"
    ) as md, patch.object(
        converter, "_patch_likhit_ocr_dpi"
    ):
        md.return_value.convert.return_value = MagicMock(text_content="fallback body")
        res = converter.convert_source(src, overwrite=True)
    md.assert_called_once()
    assert res["markdown"].rstrip().endswith("fallback body")


# ── Mislabeled-file detection (magic bytes) ──────────────────


def test_ext_from_magic_detects_office_containers():
    assert converter._ext_from_magic(b"PK\x03\x04rest") == ".docx"  # OOXML/ZIP
    assert converter._ext_from_magic(b"\xd0\xcf\x11\xe0rest") == ".doc"  # OLE2
    assert converter._ext_from_magic(b"%PDF-1.7") is None  # not disambiguated
    assert converter._ext_from_magic(b"") is None
    assert converter._ext_from_magic(b"<html>") is None


def test_docx_mislabeled_as_doc_is_routed_by_magic(settings, tmp_path):
    """A .docx saved with a .doc name (ZIP magic) must be converted as .docx,
    not handed to the legacy .doc path that would reject it."""
    settings.SOURCE_MARKDOWN_DIR = tmp_path
    src = {
        "source_id": "d1",
        "title": "Mislabeled",
        "urls": [{"link": "http://x/file.doc", "role": "RAW"}],
    }
    captured = {}

    def _fake_convert(path):
        captured["suffix"] = path[-5:]
        return MagicMock(text_content="docx body")

    with patch.object(
        converter.jds_client,
        "download_source_file",
        # ZIP magic but a .doc url
        return_value=(b"PK\x03\x04 zipbytes...", "application/msword"),
    ), patch.object(converter, "_markitdown") as md, patch.object(
        converter, "_patch_likhit_ocr_dpi"
    ):
        md.return_value.convert.side_effect = _fake_convert
        res = converter.convert_source(src, overwrite=True)
    assert res["status"] == "converted"
    assert "(.docx)" in res["note"]  # routed as docx, not .doc
    assert captured["suffix"] == ".docx"  # temp file given the right suffix


# ── Frontmatter ──────────────────────────────────────────────


def _convert_with_body(src, body, *, content_type="application/pdf", ext=".pdf"):
    """Run convert_source for `src`, mocking the download + MarkItDown output."""
    with patch.object(
        converter.jds_client,
        "download_source_file",
        return_value=(b"raw", content_type),
    ), patch.object(converter, "_ext_from_url", return_value=ext), patch.object(
        converter, "_markitdown"
    ) as md, patch.object(
        converter, "_patch_likhit_ocr_dpi"
    ):
        md.return_value.convert.return_value = MagicMock(text_content=body)
        return converter.convert_source(src, overwrite=True)


def test_converted_markdown_is_passed_through_unmodified(settings, tmp_path):
    """MarkItDown already emits clean markdown; convert_source must add only the
    frontmatter and leave the body verbatim (no re-escaping of * or _)."""
    settings.SOURCE_MARKDOWN_DIR = tmp_path
    src = {"source_id": "s1", "title": "Verdict", "url": ["http://x/a.pdf"]}
    body = "# शीर्षक\n\n- **बुँदा** and *italic*\n\n[link](http://x)"
    md = _convert_with_body(src, body)["markdown"]
    assert "\\*" not in md
    # The body appears verbatim after the frontmatter block.
    assert md.split("---\n", 2)[-1].strip().endswith(body)


def test_frontmatter_has_processed_at_and_source_metadata(settings, tmp_path):
    settings.SOURCE_MARKDOWN_DIR = tmp_path
    src = {
        "source_id": "src:123",
        "title": "My: Title",  # colon — must be safely quoted
        "source_type": "MEDIA_NEWS",
        "url": ["http://x/a.pdf"],
    }
    md = _convert_with_body(src, "body", content_type="application/pdf", ext=".pdf")[
        "markdown"
    ]
    assert md.startswith("---\n")
    assert "processed_at:" in md
    assert 'source_id: "src:123"' in md
    assert 'title: "My: Title"' in md
    assert 'source_type: "MEDIA_NEWS"' in md
    assert 'source_url: "http://x/a.pdf"' in md


def test_with_frontmatter_does_not_stack_or_emit_for_empty():
    # Empty body stays empty (so it is not treated as an attachable candidate).
    assert converter._with_frontmatter("", {"source_id": "s"}) == ""
    assert converter._with_frontmatter("  \n\n", {"source_id": "s"}) == ""
    # Pre-existing frontmatter is stripped before re-adding (no stacking).
    pre = '---\nprocessed_at: "old"\nsource_id: "s"\n---\n\nReal body'
    out = converter._with_frontmatter(pre, {"source_id": "s9"})
    assert out.count("processed_at:") == 1
    assert '"old"' not in out
    assert "Real body" in out


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


def test_per_source_errors_and_skips_are_surfaced():
    """Errored/skipped sources are logged with their reason, not just counted."""
    import io

    case = _case_with_source()
    converted = [
        {
            "source_id": "s-err",
            "conversion_status": "error",
            "conversion_note": "dead url",
        },
        {
            "source_id": "s-skip",
            "conversion_status": "skipped",
            "conversion_note": "Source has no convertible url",
        },
        {"source_id": "s-ok", "conversion_status": "converted"},
    ]
    out, err = io.StringIO(), io.StringIO()
    with patch.object(
        jds_client_in_cmd(), "iter_paginated", return_value=[{"slug": "c1"}]
    ), patch.object(jds_client_in_cmd(), "get_case", return_value=case), patch.object(
        converter,
        "convert_case_to_attach_candidates",
        return_value=(converted, []),
    ):
        call_command("reprocess_source_markdown", "--dry-run", stdout=out, stderr=err)
    err_text, out_text = err.getvalue(), out.getvalue()
    assert "s-err" in err_text and "dead url" in err_text
    assert "s-skip" in out_text and "no convertible url" in out_text


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
