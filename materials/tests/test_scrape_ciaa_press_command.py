"""ciaa_press_releases port: the command client (fake source + fake material API).

Injects a fake CIAA transport AND a fake material client via the ``build_*`` seams
and asserts the command's control flow: skip ids that already exist (resume +
reconciliation), PUT the doc then upload attachments for new ids, honour dry-run,
and stop after N consecutive 302s. The server-side upsert is exercised elsewhere
(the material API tests); here nothing hits the DB or network.
"""

from unittest.mock import patch

import pytest

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from materials.management.commands import scrape_ciaa_press_releases as C

CMD = "materials.management.commands.scrape_ciaa_press_releases"

_PDF_URL = "https://ciaa.gov.np/uploads//pressRelease/abc.pdf"
_PAGE_HTML = f"""
<div class="col-sm-8">
  <h4><strong>प्रेस विज्ञप्ति</strong></h4>
  <p>मिति २०८१।०९।२८ गते मुद्दा दायर ।</p>
  <a class="badge badge-info" href="{_PDF_URL}">डाउनलोड</a>
</div>
"""


class _FakeSource:
    """press_id → (status, html); unknown ids fall back to ``default``. Downloads
    by URL. ``default`` lets a test simulate the end-of-range (302) or a source
    outage (None transient)."""

    def __init__(self, pages, downloads=None, default=(302, "")):
        self.pages = pages
        self.downloads = downloads or {}
        self.default = default

    def get_press_release(self, press_id):
        return self.pages.get(press_id, self.default)

    def page_url(self, press_id):
        return f"https://ciaa.gov.np/pressrelease/{press_id}"

    def download(self, url):
        return self.downloads.get(url, (404, b"", ""))


class _FakeMaterial:
    def __init__(self, existing=()):
        self.existing = set(existing)   # idents already stored
        self.puts = []                  # (ident, doc, material_type)
        self.uploads = []               # (ident, filename, role, skip_convert)

    def exists(self, source, ident):
        return ident in self.existing

    def put_document(self, source, ident, doc, material_type):
        self.puts.append((ident, doc, material_type))
        return 201, {"@id": doc["@id"]}

    def upload_file(self, source, ident, *, filename, content, content_type,
                    role, source_url, skip_convert, material_type):
        self.uploads.append((ident, filename, role, skip_convert))
        return 201, {}


class CiaaCommandTests(SimpleTestCase):
    def _run(self, source, material, *extra):
        with patch(f"{CMD}.build_source_client", return_value=source), patch(
            f"{CMD}.build_material_client", return_value=material
        ):
            call_command(
                "scrape_ciaa_press_releases",
                "--api-token", "t", "--api-base", "http://api",
                "--start-id", "1", "--max-consecutive-missing", "2",
                *extra,
            )

    def test_write_ingests_new_id_and_uploads_pdf_as_raw(self):
        src = _FakeSource(
            pages={1: (200, _PAGE_HTML)},
            downloads={_PDF_URL: (200, b"%PDF-1.4 ...", "application/pdf")},
        )
        mat = _FakeMaterial()
        self._run(src, mat, "--write")

        assert [p[0] for p in mat.puts] == ["1"]
        _ident, doc, mtype = mat.puts[0]
        assert doc["@id"].endswith("/material/ciaa_press_release/1")
        assert mtype == "press_release"
        # one attachment, PDF → RAW, skip_convert True (body text was scraped).
        assert mat.uploads == [("1", "abc.pdf", "RAW", True)]

    def test_existing_ids_are_skipped(self):
        src = _FakeSource(pages={1: (200, _PAGE_HTML), 2: (200, _PAGE_HTML)})
        mat = _FakeMaterial(existing={"1"})
        self._run(src, mat, "--write")
        # id 1 already stored → skipped (no PUT); id 2 ingested.
        assert [p[0] for p in mat.puts] == ["2"]

    def test_dry_run_writes_nothing(self):
        src = _FakeSource(pages={1: (200, _PAGE_HTML)})
        mat = _FakeMaterial()
        self._run(src, mat)  # no --write
        assert mat.puts == []
        assert mat.uploads == []

    def test_stops_after_consecutive_missing(self):
        # Every id 302s; --max-consecutive-missing 2 → stop, nothing ingested.
        src = _FakeSource(pages={})
        mat = _FakeMaterial()
        self._run(src, mat, "--write")
        assert mat.puts == []

    def test_stops_after_consecutive_transient_no_infinite_loop(self):
        # Every fetch is a transient (status None) — this never yields a 302 to
        # trip the missing-stop, so the transient bound must halt the run. It
        # also now FAILS the run: see the outage tests below.
        src = _FakeSource(pages={}, default=(None, ""))
        mat = _FakeMaterial()
        with pytest.raises(CommandError):
            self._run(src, mat, "--write", "--max-consecutive-transient", "3")
        assert mat.puts == []

    def test_an_unreachable_source_fails_the_run(self):
        """A cron that fetched nothing because the site is down must go red.

        ciaa.gov.np was unreachable from at least 25 to 27 October 2026 and all
        three nightly Jobs reported success, because exiting 0 is what "scanned
        the id range, ingested nothing" looked like. The Job's own status could
        not tell that from a quiet day.
        """
        src = _FakeSource(pages={}, default=(None, ""))
        mat = _FakeMaterial()

        with pytest.raises(CommandError) as caught:
            self._run(src, mat, "--write", "--max-consecutive-transient", "3")

        assert "unreachable" in str(caught.value)

    def test_allow_source_down_keeps_a_probe_run_green(self):
        src = _FakeSource(pages={}, default=(None, ""))
        mat = _FakeMaterial()

        # No raise: an operator probing by hand asked for the no-op.
        self._run(
            src, mat, "--write", "--max-consecutive-transient", "3",
            "--allow-source-down",
        )

    def test_a_partial_outage_after_real_work_does_not_fail(self):
        """Half a shelf is still work; failing the Job would hide it behind a red."""
        src = _FakeSource(pages={1: (200, _PAGE_HTML)}, default=(None, ""))
        mat = _FakeMaterial()

        self._run(src, mat, "--write", "--max-consecutive-transient", "3")

        assert [p[0] for p in mat.puts] == ["1"]

    def test_limit_caps_new_ingests(self):
        src = _FakeSource(pages={1: (200, _PAGE_HTML), 2: (200, _PAGE_HTML), 3: (200, _PAGE_HTML)})
        mat = _FakeMaterial()
        self._run(src, mat, "--write", "--limit", "2")
        assert [p[0] for p in mat.puts] == ["1", "2"]


def test_attachment_roles_promotes_pdf_to_raw():
    urls = ["https://x/img.jpg", "https://x/doc.pdf", "https://x/extra.docx"]
    assert C.attachment_roles(urls) == [
        ("https://x/img.jpg", "ALTERNATE"),
        ("https://x/doc.pdf", "RAW"),
        ("https://x/extra.docx", "ALTERNATE"),
    ]


def test_attachment_roles_no_pdf_first_is_raw():
    urls = ["https://x/a.jpg", "https://x/b.png"]
    assert C.attachment_roles(urls) == [
        ("https://x/a.jpg", "RAW"),
        ("https://x/b.png", "ALTERNATE"),
    ]
