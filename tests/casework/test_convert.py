"""Tests for the RAW -> MARKDOWN convert stage.

The brief's five tests are the floor, not the ceiling. Several of them pass
against implementations that do nothing useful, so each is paired here with a
test that pins the behaviour the name claims:

- "already" must ALSO mean `extract_markdown` was never called (otherwise the
  stage still pays the download/OCR cost it exists to avoid).
- idempotency must ALSO be exercised through the REAL writer, not only through
  a test double that fakes the MARKDOWN append.
- the run summary must ALSO show unmet counts -- a run that converted nothing
  because every material was unconvertible must not read as a clean run.
"""
import io
import json
import logging
from pathlib import Path

import pytest

from casework.convert import (
    convert_material,
    iri_to_source_ident,
    main,
    upload_markdown,
)

MD = {"material_type": "press_release", "urls": [
    {"link": "https://x/a.pdf", "role": "RAW"},
    {"link": "https://x/a.md", "role": "MARKDOWN"}]}
RAW_ONLY = {"material_type": "press_release", "urls": [
    {"link": "https://x/b.pdf", "role": "RAW"}]}
NOTHING = {"material_type": "press_release", "urls": []}
TWO_LINKS = {"material_type": "press_release", "urls": [
    {"link": "https://x/c.pdf", "role": "RAW"},
    {"link": "https://x/c.doc", "role": "ALTERNATE"}]}


# --------------------------------------------------------------------------
# convert_material -- the brief's five
# --------------------------------------------------------------------------

def test_already_converted_is_left_untouched():
    written = []
    assert convert_material(MD, writer=lambda *a: written.append(a)) == "already"
    assert written == []


def test_raw_only_is_converted_once(monkeypatch):
    import casework.convert as c
    monkeypatch.setattr(c, "extract_markdown", lambda link: "# निकाय\n\nपाठ")
    written = []
    assert convert_material(RAW_ONLY, writer=lambda *a: written.append(a)) == "converted"
    assert len(written) == 1


def test_conversion_is_idempotent(monkeypatch):
    import casework.convert as c
    monkeypatch.setattr(c, "extract_markdown", lambda link: "# निकाय")
    mat = dict(RAW_ONLY, urls=list(RAW_ONLY["urls"]))

    def writer(material, text):
        material["urls"].append({"link": "https://x/b.md", "role": "MARKDOWN"})

    assert convert_material(mat, writer=writer) == "converted"
    assert convert_material(mat, writer=writer) == "already"
    assert sum(1 for u in mat["urls"] if u["role"] == "MARKDOWN") == 1


def test_no_convertible_link_fails_loudly():
    assert convert_material(NOTHING, writer=lambda *a: None) == "failed"


def test_empty_extraction_does_not_write(monkeypatch):
    import casework.convert as c
    monkeypatch.setattr(c, "extract_markdown", lambda link: "   ")
    written = []
    assert convert_material(RAW_ONLY, writer=lambda *a: written.append(a)) == "failed"
    assert written == []


# --------------------------------------------------------------------------
# convert_material -- the gaps the brief's five leave open
# --------------------------------------------------------------------------

def test_already_converted_never_downloads(monkeypatch):
    """"already" must short-circuit BEFORE extract_markdown.

    `test_already_converted_is_left_untouched` passes just as happily against
    an implementation that downloads and OCRs the RAW artefact and then throws
    the text away -- which is the entire cost this stage exists to skip.
    """
    import casework.convert as c
    calls = []

    def boom(link):
        calls.append(link)
        raise AssertionError("extract_markdown must not run for a converted material")

    monkeypatch.setattr(c, "extract_markdown", boom)
    assert convert_material(MD, writer=lambda *a: None) == "already"
    assert calls == []


def test_extraction_falls_back_to_the_next_convertible_link(monkeypatch):
    """A dud first artefact must not strand a material that has an ALTERNATE.

    Real data: every press release carries RAW(.pdf) + ALTERNATE(.doc). If the
    pdf text layer is empty, the .doc is usually fine -- giving up on links[0]
    would silently forfeit those materials.
    """
    import casework.convert as c
    seen = []

    def extract(link):
        seen.append(link)
        return "" if link.endswith(".pdf") else "# अदालत"

    monkeypatch.setattr(c, "extract_markdown", extract)
    written = []
    assert convert_material(
        TWO_LINKS, writer=lambda *a: written.append(a)) == "converted"
    assert seen == ["https://x/c.pdf", "https://x/c.doc"]
    assert written[0][1] == "# अदालत"


def test_extraction_error_on_one_link_does_not_abort_the_material(monkeypatch):
    import casework.convert as c

    def extract(link):
        if link.endswith(".pdf"):
            raise OSError("HTTP 500")
        return "# अदालत"

    monkeypatch.setattr(c, "extract_markdown", extract)
    written = []
    assert convert_material(
        TWO_LINKS, writer=lambda *a: written.append(a)) == "converted"
    assert len(written) == 1


def test_every_link_failing_is_failed_not_converted(monkeypatch):
    import casework.convert as c
    monkeypatch.setattr(c, "extract_markdown", lambda link: "")
    written = []
    assert convert_material(
        TWO_LINKS, writer=lambda *a: written.append(a)) == "failed"
    assert written == []


# --------------------------------------------------------------------------
# The real writer
# --------------------------------------------------------------------------

def test_iri_to_source_ident_keeps_multi_segment_sources():
    assert iri_to_source_ident(
        "https://jawafdehi.org/material/ciaa/press_releases/2572"
    ) == ("ciaa/press_releases", "2572")
    assert iri_to_source_ident(
        "https://jawafdehi.org/material/jawafdehi/abc"
    ) == ("jawafdehi", "abc")
    with pytest.raises(ValueError):
        iri_to_source_ident("https://jawafdehi.org/entity/ciaa/2572")


class _FakeApi:
    """Captures the request `upload_markdown` would put on the wire."""

    base_url = "http://127.0.0.1:48010/api"

    def __init__(self, response=None):
        self.calls = []
        self._response = response if response is not None else {
            "associatedMedia": [
                {"contentUrl": "http://127.0.0.1:48010/media/x.md",
                 "jawafdehi:linkRole": "MARKDOWN"}]}

    def _headers(self, content_type=None):
        return {"Authorization": "Basic redacted", "Content-Type": content_type}

    def _request(self, method, url, data=None, headers=None, timeout=60):
        self.calls.append({"method": method, "url": url, "data": data,
                           "headers": headers})
        return io.BytesIO(json.dumps(self._response).encode())


def test_upload_markdown_posts_a_markdown_role_multipart_to_the_file_endpoint():
    """The real writer must POST role=MARKDOWN to the local file endpoint.

    Everything above drives `convert_material` with a lambda writer, so nothing
    else in this file would notice if the production writer uploaded under the
    wrong role, to the wrong path, or with the wrong verb -- the run would
    report "converted" for every material and coverage would not move.
    """
    api = _FakeApi()
    link = upload_markdown(
        api, "https://jawafdehi.org/material/ciaa/press_releases/2572", "# निकाय")

    assert len(api.calls) == 1
    call = api.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/materials/ciaa/press_releases/2572/file")
    body = call["data"]
    assert b'name="role"' in body and b"MARKDOWN" in body
    # MARKDOWN is our own extraction output; re-OCR-ing it server-side would be
    # both wasted work and a way to clobber the text we just extracted.
    assert b'name="skip_convert"' in body
    assert "निकाय".encode() in body
    assert call["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert link == "http://127.0.0.1:48010/media/x.md"


def test_upload_markdown_returns_none_when_no_markdown_role_in_response():
    """`upload_markdown`'s documented contract for "the server accepted the
    request but attached no MARKDOWN role": return None, not a URL and not a
    raise. The caller (main()'s `writer` closure) is what must act on this --
    exercised end to end below."""
    api = _FakeApi(response={"associatedMedia": [
        {"contentUrl": "http://127.0.0.1:48010/media/x.pdf",
         "jawafdehi:linkRole": "RAW"}]})
    link = upload_markdown(
        api, "https://jawafdehi.org/material/ciaa/press_releases/2572", "# x")
    assert link is None


def test_upload_markdown_refuses_a_non_local_api():
    """Belt-and-braces against the one catastrophic failure mode: a prod write."""
    api = _FakeApi()
    api.base_url = "https://api.jawafdehi.org/api"
    with pytest.raises(ValueError, match="loopback"):
        upload_markdown(api, "https://jawafdehi.org/material/ciaa/x/1", "# x")


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------

CASE = {
    "slug": "test-case",
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/1",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/1.pdf", "role": "RAW"}]}},
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/2",
         "material": {"material_type": "press_release", "urls": []}},
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/3",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/3.md", "role": "MARKDOWN"}]}},
    ],
}


class _StubApi:
    def __init__(self):
        self.uploads = []

    def iter_cases(self, params=None, timeout=60):
        yield {"slug": "test-case"}

    def get_case(self, slug, timeout=60):
        return CASE


@pytest.fixture
def stub_run(monkeypatch):
    import casework.convert as c
    api = _StubApi()
    monkeypatch.setattr(c, "extract_markdown", lambda link: "# निकाय")
    monkeypatch.setattr(c, "build_api", lambda args: api)
    monkeypatch.setattr(
        c, "upload_markdown",
        lambda a, iri, text: api.uploads.append(iri) or "http://local/x.md")
    return api


def test_main_summary_shows_unmet_separately_from_converted(stub_run, capsys):
    """A material with no convertible link is UNMET, and it must be visible.

    Collapsing unmet into "skipped" (or omitting it) is how this pipeline would
    report false parity in the Task 16 A/B: a run that converted nothing would
    print the same summary as a run with nothing left to do.
    """
    main(["--apply"])
    out = capsys.readouterr().out
    assert "converted: 1" in out
    assert "already: 1" in out
    assert "unmet: 1" in out
    # ...and the REASON, not just the count.
    assert "no convertible link" in out


def test_main_prints_every_unmet_reason_not_just_the_count(stub_run, capsys):
    """Deleting the reasons block must break a test.

    `test_main_summary_shows_unmet_separately_from_converted` asserts the reason
    text appears, but it survived a mutation that removed the block's header --
    this one pins the block itself by asserting the count-prefixed reason line.
    """
    main(["--apply"])
    out = capsys.readouterr().out
    assert f"1 x {c_unmet_no_link()}" in out


def test_main_converts_a_shared_material_only_once(monkeypatch, capsys):
    """One material bound to several cases must be downloaded+uploaded once.

    The snapshot has 691 evidence entries over 630 distinct materials, so ~60
    materials are shared. Without IRI dedup, main() re-converts each of them per
    binding: wasted downloads, a duplicate upload, and an inflated "converted"
    count that would overstate the gate result.
    """
    import casework.convert as c
    uploads = []

    shared = {"material_type": "press_release",
              "urls": [{"link": "https://x/s.pdf", "role": "RAW"}]}
    cases = {
        "case-a": {"slug": "case-a", "evidence": [
            {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/9",
             "material": shared}]},
        "case-b": {"slug": "case-b", "evidence": [
            {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/9",
             "material": dict(shared)}]},
    }

    class _Api:
        def iter_cases(self, params=None, timeout=60):
            yield {"slug": "case-a"}
            yield {"slug": "case-b"}

        def get_case(self, slug, timeout=60):
            return cases[slug]

    monkeypatch.setattr(c, "extract_markdown", lambda link: "# निकाय")
    monkeypatch.setattr(c, "build_api", lambda args: _Api())
    monkeypatch.setattr(c, "upload_markdown",
                        lambda a, iri, text: uploads.append(iri) or "http://local/x.md")
    main(["--apply"])
    assert uploads == ["https://jawafdehi.org/material/ciaa/press_releases/9"]
    assert "materials_seen: 1" in capsys.readouterr().out


def c_unmet_no_link():
    from casework.convert import UNMET_NO_LINK

    return UNMET_NO_LINK


def test_main_dry_run_is_the_default_and_writes_nothing(stub_run, capsys):
    main([])
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert stub_run.uploads == []


def test_main_apply_writes_through_upload_markdown(stub_run, capsys):
    main(["--apply"])
    assert stub_run.uploads == ["https://jawafdehi.org/material/ciaa/press_releases/1"]
    assert "APPLIED" in capsys.readouterr().out


def test_main_does_not_report_converted_when_upload_finds_no_markdown_role(
    monkeypatch, capsys
):
    """Finding 3: `upload_markdown`'s return value was discarded in main()'s
    `writer` closure, so a server response with no MARKDOWN role still
    reported "converted" -- a false success this branch had zero coverage
    of. Offline only: `upload_markdown` itself is monkeypatched, no network.
    """
    import casework.convert as c

    api = _StubApi()
    monkeypatch.setattr(c, "extract_markdown", lambda link: "# निकाय")
    monkeypatch.setattr(c, "build_api", lambda args: api)
    # Simulates a real upload_markdown() call whose server response carried
    # no MARKDOWN-role media object: returns None, exactly like the real
    # function does in that case (see test_upload_markdown_returns_none_
    # when_no_markdown_role_in_response above).
    monkeypatch.setattr(c, "upload_markdown", lambda a, iri, text: None)

    report = main(["--apply"])
    statuses = {r["status"] for r in report.rows}
    assert "converted" not in statuses
    # The material that would have falsely reported "converted" is now
    # visibly an error, not silently absorbed into "already"/"unmet".
    assert any(
        r["status"] == "error" and "no MARKDOWN-role link" in r["reason"]
        for r in report.rows
    )


def test_main_does_not_run_any_other_stage(stub_run):
    """Settled sequencing decision: convert does not expand into the pipeline."""
    import casework.convert as c
    assert not hasattr(c, "order_stages")
    main(["--apply"])
    assert stub_run.uploads  # it did its own work...
    # ...and nothing else got invoked: the stub api exposes only the two read
    # methods convert needs, so any downstream stage call would AttributeError.


# --------------------------------------------------------------------------
# Task PP2 -- run-logging events file (see test_enrich_missing_bigo.py's
# identical block for the rationale; `conftest.py`'s autouse
# `_isolate_casework_run_logs` fixture keeps these out of the real repo
# `work/enricher-runs/`). `convert` has no LLM `extract` step of its own --
# its per-material outcome IS the `convert` step.
# --------------------------------------------------------------------------


def _events_path():
    logger = logging.getLogger("casework.convert")
    return logger._casework_run_paths["events"]


def _read_events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_events_file_covers_start_and_converted_on_apply(stub_run, tmp_path):
    main(["--apply"])

    rows = _read_events(_events_path())
    assert rows

    required_keys = {"ts", "run_id", "stage", "slug", "step", "status", "detail", "elapsed_ms"}
    for row in rows:
        assert required_keys <= set(row.keys())
        assert row["stage"] == "convert"

    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("start", "start") in steps_and_statuses
    assert ("convert", "converted") in steps_and_statuses
    assert ("convert", "already") in steps_and_statuses
    assert ("convert", "unmet") in steps_and_statuses


def test_events_file_records_would_convert_under_dry_run(stub_run, tmp_path):
    main([])  # default is dry-run

    rows = _read_events(_events_path())
    steps_and_statuses = {(r["step"], r["status"]) for r in rows}
    assert ("convert", "would-convert") in steps_and_statuses
    assert ("convert", "converted") not in steps_and_statuses
