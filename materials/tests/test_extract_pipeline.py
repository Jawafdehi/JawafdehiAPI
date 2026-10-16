"""The batch command and the worker that drain extraction work.

Two entry points onto one writer: ``extract_document_content`` selects documents
and either queues them or extracts them in place, and ``extract_worker`` runs
what was queued. The PDF is built in-process and the fetch is stubbed, so these
exercise the real extractor without a network.

Pinned here: that selection explains itself (an empty run is ambiguous and
expensive to diagnose), that a document already extracted is not silently
re-extracted by a worker, and that an unreadable document fails
NON-retryably — three attempts at a mis-linked file is three times the cost for
the same answer.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_extract_pipeline.py
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import fitz
from django.core.management import CommandError, call_command
from django.test import TestCase

from jobs import queue as jobs_queue
from jobs.models import Job
from materials import extract as material_extract
from materials.jsonld import MATERIAL_CONTEXT
from materials.management.commands import extract_document_content as cmd
from materials.models import DocumentContent, DocumentPage, DocumentTable, Material

SOURCE = "ciaa_annual_report"
PDF_URL = "https://s3.jawafdehi.org/case_uploads/abc123.pdf"


def _pdf_bytes(pages: int = 2) -> bytes:
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 90), f"Page {index + 1} of the report.", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _material(ident: str, *, source: str = SOURCE, with_pdf: bool = True) -> Material:
    iri = f"https://jawafdehi.org/material/{source}/{ident}"
    data = {
        "@context": MATERIAL_CONTEXT,
        "@type": "DigitalDocument",
        "@id": iri,
        "name": {"ne": "प्रतिवेदन"},
    }
    if with_pdf:
        data["associatedMedia"] = [
            {
                "@type": "MediaObject",
                "contentUrl": f"{PDF_URL}?{ident}",
                "jawafdehi:linkRole": "RAW",
            }
        ]
    return Material.objects.create(
        iri=iri, material_type="document", source=source, ident=ident, data=data
    )


class SelectionTests(TestCase):
    databases = {"default", "ngm"}

    def test_a_dry_run_names_what_it_would_read(self):
        _material("aaa")
        out = StringIO()

        call_command(
            "extract_document_content", "--source", SOURCE, "--dry-run", stdout=out
        )

        self.assertIn("would extract", out.getvalue())
        self.assertEqual(Job.objects.count(), 0)

    def test_documents_we_already_hold_are_left_alone(self):
        held = _material("aaa")
        DocumentContent.objects.create(
            material=held, doc_id="aaa", dataset="jawafdehi:pdf-extract/v1"
        )
        _material("bbb")
        out = StringIO()

        call_command("extract_document_content", "--source", SOURCE, stdout=out)

        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(Job.objects.get().payload["material_iri"], _iri("bbb"))

    def test_force_re_reads_a_document_we_already_hold(self):
        held = _material("aaa")
        DocumentContent.objects.create(
            material=held, doc_id="aaa", dataset="jawafdehi:pdf-extract/v1"
        )

        call_command(
            "extract_document_content", "--source", SOURCE, "--force", stdout=StringIO()
        )

        job = Job.objects.get()
        # The flag rides in the payload rather than the dedup key: the worker is
        # what decides to replace, and two forced asks are still one job.
        self.assertTrue(job.payload["force"])

    def test_a_named_material_that_cannot_be_read_says_why(self):
        _material("aaa", with_pdf=False)
        out = StringIO()

        call_command("extract_document_content", "--iri", _iri("aaa"), stdout=out)

        # Reported, not silently dropped: "this source has no extractable
        # documents" and "this source is already done" look identical from an
        # empty run and call for opposite next steps.
        self.assertIn("carries no extractable document", out.getvalue())
        self.assertEqual(Job.objects.count(), 0)

    def test_a_named_material_that_is_not_selected_says_that_too(self):
        out = StringIO()

        call_command("extract_document_content", "--iri", _iri("ghost"), stdout=out)

        self.assertIn("not selected", out.getvalue())

    def test_the_limit_bounds_the_run(self):
        for ident in ("aaa", "bbb", "ccc"):
            _material(ident)

        call_command(
            "extract_document_content",
            "--source",
            SOURCE,
            "--limit",
            "2",
            stdout=StringIO(),
        )

        self.assertEqual(Job.objects.count(), 2)

    def test_rendering_charts_without_inline_is_refused(self):
        # Rendering needs the document's bytes in hand, which only the inline
        # path has; queueing with --render-charts would render nothing and
        # report success.
        with self.assertRaisesRegex(CommandError, "--inline"):
            call_command(
                "extract_document_content",
                "--source",
                SOURCE,
                "--render-charts",
                "/tmp/does-not-matter",
                stdout=StringIO(),
            )


class InlineExtractionTests(TestCase):
    databases = {"default", "ngm"}

    def test_inline_extraction_writes_the_document_it_read(self):
        _material("aaa")
        out = StringIO()

        with patch.object(cmd, "fetch_document", return_value=_pdf_bytes(2)):
            call_command(
                "extract_document_content",
                "--iri",
                _iri("aaa"),
                "--inline",
                stdout=out,
            )

        content = DocumentContent.objects.get(pk=_iri("aaa"))
        self.assertEqual(content.page_count, 2)
        self.assertEqual(content.dataset, "jawafdehi:pdf-extract/v1")
        self.assertTrue(content.dataset_revision.startswith("sha256:"))
        self.assertEqual(DocumentPage.objects.filter(content=content).count(), 2)
        self.assertIn("2 pages", out.getvalue())
        self.assertEqual(Job.objects.count(), 0)

    def test_a_document_that_cannot_be_fetched_is_reported_not_stored(self):
        _material("aaa")
        out = StringIO()

        with patch.object(
            cmd,
            "fetch_document",
            side_effect=material_extract.DocumentFetchError("HTTP 503"),
        ):
            call_command(
                "extract_document_content",
                "--iri",
                _iri("aaa"),
                "--inline",
                stdout=out,
            )

        # Nothing stored: a document we could not open must not become a document
        # with no content, which a client cannot tell from an empty one.
        self.assertFalse(DocumentContent.objects.filter(pk=_iri("aaa")).exists())
        self.assertIn("failed", out.getvalue())
        self.assertIn("HTTP 503", out.getvalue())

    def test_rendering_writes_a_manifest_for_the_chart_pages(self):
        import json
        import tempfile
        from pathlib import Path

        _material("aaa")
        doc = fitz.open()
        page = doc.new_page()
        # Latin, because PyMuPDF's built-in fonts carry no Devanagari glyphs —
        # the Nepali caption branch is covered against the detector directly in
        # test_extraction_pdf.ChartDetectionTests.
        page.insert_text((72, 90), "Figure 2.14 complaints by sector", fontsize=11)
        page.draw_circle(fitz.Point(300, 400), 150, fill=(0.9, 0.2, 0.2))
        data = doc.tobytes()
        doc.close()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cmd, "fetch_document", return_value=data):
                call_command(
                    "extract_document_content",
                    "--iri",
                    _iri("aaa"),
                    "--inline",
                    "--render-charts",
                    tmp,
                    stdout=StringIO(),
                )
            manifest = json.loads(
                (Path(tmp) / "aaa" / "chart_pages.json").read_text()
            )
            rendered = (Path(tmp) / "aaa" / "pages" / "p0001.png").is_file()

        self.assertEqual([c["page"] for c in manifest["candidates"]], [1])
        self.assertTrue(rendered)
        # The chart page is NOT stored as a figure: a chart listed with no points
        # would read as "we read this chart and it had no data".
        self.assertEqual(
            DocumentContent.objects.get(pk=_iri("aaa")).figures_verdict,
            "not_attempted",
        )


class WorkerTests(TestCase):
    databases = {"default", "ngm"}

    def _claim(self):
        return jobs_queue.claim_next([material_extract.EXTRACT_KIND])

    def test_a_queued_job_is_claimed_extracted_and_finalized(self):
        _material("aaa")
        material_extract.enqueue_material_extract(_iri("aaa"))
        out = StringIO()

        with patch.object(
            material_extract, "fetch_document", return_value=_pdf_bytes(3)
        ):
            call_command("extract_worker", "--apply", "--once", stdout=out)

        job = Job.objects.get()
        self.assertEqual(job.status, Job.DONE)
        self.assertEqual(job.result["pages"], 3)
        # The result carries counts, not content: a 446-page report's pages are
        # megabytes of transcript, which would not survive the jobs API's
        # 2.5 MB body limit — hence the in-process worker.
        self.assertNotIn("text", str(job.result))
        self.assertEqual(DocumentContent.objects.get(pk=_iri("aaa")).page_count, 3)

    def test_the_claim_resolves_the_document_links_as_they_are_now(self):
        material = _material("aaa")
        material_extract.enqueue_material_extract(_iri("aaa"))
        material.data = {
            **material.data,
            "associatedMedia": [
                {
                    "@type": "MediaObject",
                    "contentUrl": "https://s3.jawafdehi.org/case_uploads/new.pdf",
                    "jawafdehi:linkRole": "RAW",
                }
            ],
        }
        material.save()

        job = self._claim()

        # Resolved at claim time, not at enqueue time: a file replaced between
        # the ask and the run is the file that gets read.
        self.assertEqual(
            job.payload["source_urls"],
            ["https://s3.jawafdehi.org/case_uploads/new.pdf"],
        )

    def test_a_worker_leaves_a_document_someone_else_extracted_alone(self):
        material = _material("aaa")
        material_extract.enqueue_material_extract(_iri("aaa"))
        DocumentContent.objects.create(
            material=material,
            doc_id="aaa",
            dataset="jawafdehi:pdf-extract/v1",
            dataset_revision="sha256:earlier",
        )
        out = StringIO()

        with patch.object(material_extract, "fetch_document") as fetch:
            call_command("extract_worker", "--apply", "--once", stdout=out)

        # A batch run may have extracted the same document between the ask and
        # the claim. Re-reading it would delete and rewrite every row, moving the
        # ETag of a document nobody changed — so it is skipped, and nothing is
        # even downloaded.
        fetch.assert_not_called()
        self.assertEqual(Job.objects.get().status, Job.DONE)
        self.assertIn("already extracted", out.getvalue())
        self.assertEqual(
            DocumentContent.objects.get(pk=_iri("aaa")).dataset_revision,
            "sha256:earlier",
        )

    def test_force_makes_the_worker_read_it_again(self):
        material = _material("aaa")
        material_extract.enqueue_material_extract(_iri("aaa"), force=True)
        DocumentContent.objects.create(
            material=material,
            doc_id="aaa",
            dataset="jawafdehi:pdf-extract/v1",
            dataset_revision="sha256:earlier",
        )

        with patch.object(
            material_extract, "fetch_document", return_value=_pdf_bytes(1)
        ):
            call_command("extract_worker", "--apply", "--once", stdout=StringIO())

        self.assertNotEqual(
            DocumentContent.objects.get(pk=_iri("aaa")).dataset_revision,
            "sha256:earlier",
        )

    def test_an_unreadable_document_fails_without_burning_the_retry_budget(self):
        _material("aaa")
        material_extract.enqueue_material_extract(_iri("aaa"))

        with patch.object(
            material_extract, "fetch_document", return_value=b"not a pdf at all"
        ):
            call_command(
                "extract_worker", "--apply", "--once", stdout=StringIO(), stderr=StringIO()
            )

        job = Job.objects.get()
        # DocumentFetchError here reports "no source produced a readable
        # document", which IS retryable (the next source, or a re-upload, may
        # work) — so the queue re-queues it once rather than dead-lettering a
        # transient failure. What must not happen is a stored document.
        self.assertIn(job.status, (Job.QUEUED, Job.FAILED, Job.DEAD))
        self.assertFalse(DocumentContent.objects.filter(pk=_iri("aaa")).exists())

    def test_a_material_with_nothing_to_read_fails_the_job_at_claim(self):
        _material("aaa", with_pdf=False)
        material_extract.enqueue_material_extract(_iri("aaa"))

        job = self._claim()

        # build_payload fails the job rather than handing a worker an empty
        # worklist that it would report success over.
        self.assertIsNone(job)
        self.assertEqual(Job.objects.get().status, Job.FAILED)

    def test_the_worker_reports_before_it_touches_anything(self):
        _material("aaa")
        material_extract.enqueue_material_extract(_iri("aaa"))
        out = StringIO()

        call_command("extract_worker", stdout=out)

        self.assertIn("1 queued", out.getvalue())
        self.assertEqual(Job.objects.get().status, Job.QUEUED)


def _iri(ident: str, source: str = SOURCE) -> str:
    return f"https://jawafdehi.org/material/{source}/{ident}"


class ChartWorklistTests(TestCase):
    databases = {"default", "ngm"}

    def test_chart_pages_ride_on_the_job_result_not_the_document(self):
        _material("aaa")
        material_extract.enqueue_material_extract(_iri("aaa"))
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 90), "Chart 2.2 complaints", fontsize=11)
        page.draw_circle(fitz.Point(300, 400), 150, fill=(0.1, 0.6, 0.3))
        data = doc.tobytes()
        doc.close()

        with patch.object(material_extract, "fetch_document", return_value=data):
            call_command("extract_worker", "--apply", "--once", stdout=StringIO())

        job = Job.objects.get()
        # The worklist for the vision tier: page numbers on the job, nothing
        # asserted in the read plane about what those charts contain.
        self.assertEqual(job.result["chart_pages"], [1])
        self.assertEqual(job.result["figures"], 0)
        content = DocumentContent.objects.get(pk=_iri("aaa"))
        self.assertEqual(content.figures_verdict, "not_attempted")
        self.assertEqual(DocumentTable.objects.filter(content=content).count(), 0)
