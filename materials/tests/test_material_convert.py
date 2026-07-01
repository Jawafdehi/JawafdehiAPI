"""Tests for the ``material_convert`` job kind — the data-plane FTS feed.

Covers the server-side seams (no OCR, no network):
- source_urls role preference + MARKDOWN/SOURCE_PAGE exclusion,
- enqueue_material_convert dedup on the IRI,
- build_convert_payload resolves URLs / fails on missing material or no source,
- apply_convert_result writes data["text"] + a single MARKDOWN MediaObject and
  is idempotent on re-run,
- the kind is registered with its policy + hooks,
and the worker handler with the OCR helper mocked.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jawafdehi_shared.entities.ids import build_material_iri
from materials.conversion import (
    CONVERT_KIND,
    apply_convert_result,
    build_convert_payload,
    enqueue_material_convert,
    source_urls,
)
from materials.models import Material


def _doc(iri, media):
    return {
        "@context": "https://schema.org",
        "@type": "DigitalDocument",
        "@id": iri,
        "name": {"ne": "परीक्षण"},
        "associatedMedia": media,
    }


def _mo(url, role):
    return {"@type": "MediaObject", "contentUrl": url, "jawafdehi:linkRole": role}


def _store(iri, media):
    mat = Material.from_jsonld(_doc(iri, media), material_type="document")
    mat.save()
    return mat


# --- source_urls (pure) ------------------------------------------------------


def test_source_urls_prefers_raw_then_alternate_then_permalink():
    data = _doc(
        "x",
        [
            _mo("https://a/permalink.pdf", "PERMALINK"),
            _mo("https://a/raw.pdf", "RAW"),
            _mo("https://a/alt.pdf", "ALTERNATE"),
        ],
    )
    assert source_urls(data) == [
        "https://a/raw.pdf",
        "https://a/alt.pdf",
        "https://a/permalink.pdf",
    ]


def test_source_urls_excludes_markdown_and_source_page():
    data = _doc(
        "x",
        [
            _mo("https://a/out.md", "MARKDOWN"),
            _mo("https://a/page.html", "SOURCE_PAGE"),
            _mo("https://a/raw.pdf", "RAW"),
        ],
    )
    assert source_urls(data) == ["https://a/raw.pdf"]


def test_source_urls_handles_single_dict_and_empty():
    single = {"associatedMedia": _mo("https://a/raw.pdf", "RAW")}
    assert source_urls(single) == ["https://a/raw.pdf"]
    assert source_urls({}) == []
    assert source_urls(_doc("x", [_mo("", "RAW")])) == []


def test_source_urls_skips_non_string_content_url():
    # Malformed JSONB: contentUrl is a list/int/dict, not a string. Must be
    # skipped, not crash with AttributeError on .strip().
    data = _doc(
        "x",
        [
            {"@type": "MediaObject", "contentUrl": ["not", "a", "str"],
             "jawafdehi:linkRole": "RAW"},
            {"@type": "MediaObject", "contentUrl": 123, "jawafdehi:linkRole": "RAW"},
            _mo("https://a/raw.pdf", "RAW"),
        ],
    )
    assert source_urls(data) == ["https://a/raw.pdf"]


# --- build_convert_payload ---------------------------------------------------


@pytest.mark.django_db
def test_build_payload_resolves_source_urls():
    iri = build_material_iri("ciaa", "report-1")
    _store(iri, [_mo("https://a/raw.pdf", "RAW")])
    job = _FakeJob(payload={"material_iri": iri})
    assert build_convert_payload(job) == {"source_urls": ["https://a/raw.pdf"]}


@pytest.mark.django_db
def test_build_payload_raises_on_missing_material():
    job = _FakeJob(payload={"material_iri": build_material_iri("ciaa", "gone")})
    with pytest.raises(ValueError, match="no live Material"):
        build_convert_payload(job)


@pytest.mark.django_db
def test_build_payload_raises_when_no_ocrable_source():
    iri = build_material_iri("ciaa", "html-only")
    _store(iri, [_mo("https://a/page.html", "SOURCE_PAGE")])
    job = _FakeJob(payload={"material_iri": iri})
    with pytest.raises(ValueError, match="no OCR-able source"):
        build_convert_payload(job)


# --- apply_convert_result ----------------------------------------------------


@pytest.mark.django_db
def test_apply_result_sets_text_and_markdown_media():
    iri = build_material_iri("ciaa", "report-2")
    _store(iri, [_mo("https://a/raw.pdf", "RAW")])
    job = _FakeJob(payload={"material_iri": iri})

    with patch(
        "jawafdehi_shared.storage.store_file_as_link",
        return_value={"link": "https://cdn/x.md", "role": "MARKDOWN"},
    ):
        apply_convert_result(job, {"text": "पूर्ण पाठ", "source_url": "https://a/raw.pdf"})

    row = Material.objects.get(pk=iri)
    assert row.data["text"] == {"ne": "पूर्ण पाठ"}
    md = [
        m
        for m in row.data["associatedMedia"]
        if m.get("jawafdehi:linkRole") == "MARKDOWN"
    ]
    assert len(md) == 1 and md[0]["contentUrl"] == "https://cdn/x.md"
    # The original RAW link is preserved.
    assert any(m.get("jawafdehi:linkRole") == "RAW" for m in row.data["associatedMedia"])


@pytest.mark.django_db
def test_apply_result_is_idempotent_on_rerun():
    iri = build_material_iri("ciaa", "report-3")
    _store(iri, [_mo("https://a/raw.pdf", "RAW")])
    job = _FakeJob(payload={"material_iri": iri})

    with patch(
        "jawafdehi_shared.storage.store_file_as_link",
        side_effect=[
            {"link": "https://cdn/v1.md", "role": "MARKDOWN"},
            {"link": "https://cdn/v2.md", "role": "MARKDOWN"},
        ],
    ):
        apply_convert_result(job, {"text": "v1", "source_url": "https://a/raw.pdf"})
        apply_convert_result(job, {"text": "v2", "source_url": "https://a/raw.pdf"})

    row = Material.objects.get(pk=iri)
    md = [
        m
        for m in row.data["associatedMedia"]
        if m.get("jawafdehi:linkRole") == "MARKDOWN"
    ]
    assert len(md) == 1 and md[0]["contentUrl"] == "https://cdn/v2.md"
    assert row.data["text"] == {"ne": "v2"}


@pytest.mark.django_db
def test_apply_result_noop_on_empty_text():
    iri = build_material_iri("ciaa", "report-4")
    _store(iri, [_mo("https://a/raw.pdf", "RAW")])
    job = _FakeJob(payload={"material_iri": iri})
    apply_convert_result(job, {"text": "   ", "source_url": "https://a/raw.pdf"})
    assert "text" not in Material.objects.get(pk=iri).data


# --- enqueue dedup -----------------------------------------------------------


@pytest.mark.django_db
def test_enqueue_dedups_on_iri():
    iri = build_material_iri("ciaa", "report-5")
    j1 = enqueue_material_convert(iri)
    j2 = enqueue_material_convert(iri)
    assert j1.pk == j2.pk
    assert j1.dedup_key == f"{CONVERT_KIND}:{iri}"


# --- registration ------------------------------------------------------------


def test_kind_is_registered_with_hooks():
    from jobs import registry

    spec = registry.get(CONVERT_KIND)
    assert spec.kind == CONVERT_KIND
    assert spec.build_payload is not None and spec.on_result is not None
    assert spec.lease_seconds == 1800 and spec.max_attempts == 2


# --- worker handler (OCR mocked) ---------------------------------------------


def test_worker_handler_converts_first_working_source():
    from materials.job_handlers import handle_material_convert

    stages: list[str] = []
    # convert_source (in-repo review.converter) returns a status dict.
    with patch(
        "review.converter.convert_source",
        return_value={"markdown": "पूर्ण पाठ", "status": "converted",
                      "url": "https://a/raw.pdf", "note": ""},
    ):
        out = handle_material_convert(
            {"source_urls": ["https://a/raw.pdf"]}, on_stage=stages.append
        )
    assert out == {"text": "पूर्ण पाठ", "source_url": "https://a/raw.pdf"}
    assert any(s.startswith("convert:done") for s in stages)


def test_worker_handler_falls_back_to_next_source_when_empty():
    from materials.job_handlers import handle_material_convert

    def _convert(source):
        url = source["url"][0]
        if url == "https://a/raw.pdf":
            return {"markdown": "", "status": "error", "url": url, "note": "boom"}
        return {"markdown": "ok", "status": "converted", "url": url, "note": ""}

    with patch("review.converter.convert_source", side_effect=_convert):
        out = handle_material_convert(
            {"source_urls": ["https://a/raw.pdf", "https://a/alt.pdf"]},
            on_stage=lambda s: None,
        )
    assert out == {"text": "ok", "source_url": "https://a/alt.pdf"}


def test_worker_handler_raises_when_all_sources_empty():
    from materials.job_handlers import handle_material_convert

    with patch(
        "review.converter.convert_source",
        return_value={"markdown": "", "status": "error",
                      "url": "https://a/raw.pdf", "note": "no text"},
    ):
        with pytest.raises(RuntimeError, match="no text"):
            handle_material_convert(
                {"source_urls": ["https://a/raw.pdf"]}, on_stage=lambda s: None
            )


def test_worker_handler_falls_back_when_convert_source_raises():
    """If convert_source raises (not just returns error), _convert_with_timeout
    degrades to an error result so the loop tries the next alternate URL."""
    from materials.job_handlers import handle_material_convert

    def _convert(source):
        if source["url"][0] == "https://a/raw.pdf":
            raise RuntimeError("library crash")
        return {"markdown": "ok", "status": "converted",
                "url": source["url"][0], "note": ""}

    with patch("review.converter.convert_source", side_effect=_convert):
        out = handle_material_convert(
            {"source_urls": ["https://a/raw.pdf", "https://a/alt.pdf"]},
            on_stage=lambda s: None,
        )
    assert out == {"text": "ok", "source_url": "https://a/alt.pdf"}


def test_worker_handler_times_out_a_stalled_conversion(settings):
    """A conversion that never returns is abandoned at CONVERT_SOURCE_TIMEOUT
    (not left to pin the consumer until the job lease lapses)."""
    import time

    from materials.job_handlers import handle_material_convert

    settings.CONVERT_SOURCE_TIMEOUT = 1  # keep the test fast

    def _hang(_source):
        time.sleep(30)  # far longer than the timeout
        return {"markdown": "never", "status": "converted", "url": "x", "note": ""}

    with patch("review.converter.convert_source", side_effect=_hang):
        # Single stalled source → all sources produced no text → RuntimeError,
        # but it returns in ~1s (the timeout), not 30s.
        with pytest.raises(RuntimeError, match="timed out"):
            handle_material_convert(
                {"source_urls": ["https://a/raw.pdf"]}, on_stage=lambda s: None
            )


# --- test doubles ------------------------------------------------------------


class _FakeJob:
    def __init__(self, payload):
        self.payload = payload
        self.pk = 1
