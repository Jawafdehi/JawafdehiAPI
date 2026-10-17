"""The edge cases a pass over the pipeline turned up, each pinned.

Every test here corresponds to a defect that was in the code and is not any
more. They are collected rather than scattered because they share a shape: each
is something the happy path cannot see — a malformed row, a Postgres-only
rejection, a region of the archive with nothing in it, a race with the worker.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_extract_edge_cases.py
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from jobs.models import Job
from materials import artifacts
from materials import extract as material_extract
from materials.document_content import _clean, store_document_content
from materials.extraction.bundle import ContentBundle, PageRecord, TableRecord
from materials.redaction import REDACTED_SUFFIX, redact_bundle
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import DocumentContent, ExtractionScan, Material
from materials.provenance import PROVENANCE_KEY

SOURCE = "ciaa_annual_report"
PDF = "https://s3.jawafdehi.org/case_uploads/abc.pdf"
DIGEST = "a" * 64


def _iri(ident: str) -> str:
    return f"https://jawafdehi.org/material/{SOURCE}/{ident}"


def _material(ident: str, media: list | None = None) -> Material:
    data = {
        "@context": MATERIAL_CONTEXT,
        "@type": "DigitalDocument",
        "@id": _iri(ident),
        "name": {"ne": "प्रतिवेदन"},
    }
    if media is not None:
        data["associatedMedia"] = media
    return Material.objects.create(
        iri=_iri(ident), material_type="report", source=SOURCE, ident=ident, data=data
    )


def _pdf_media(sha: str | None = DIGEST) -> dict:
    media = {"@type": "MediaObject", "contentUrl": PDF, "jawafdehi:linkRole": "RAW"}
    if sha is not None:
        media[PROVENANCE_KEY] = {"fetch_method": "upload", "sha256": sha}
    return media


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
class MalformedJsonbTests(TestCase):
    """``associatedMedia`` is free-form JSONB and not everything in it is a string.

    This runs on the material detail payload — the hottest public read in the
    archive — so a type error here is a 500 on a document a reader can otherwise
    see perfectly well.
    """

    def test_a_non_string_url_does_not_raise(self):
        for value in (12345, None, ["https://x"], {"@id": "y"}):
            with self.subTest(value=value):
                media = {"contentUrl": value, "jawafdehi:linkRole": "RAW"}
                self.assertFalse(artifacts.is_withheld(media))

    def test_a_non_string_role_is_treated_as_raw_and_withheld(self):
        # The SAFE default, not the lenient one: a link of ours whose role we
        # cannot read is withheld rather than published.
        media = {"contentUrl": PDF, "jawafdehi:linkRole": 7}
        self.assertTrue(artifacts.is_withheld(media))

    def test_a_whole_payload_of_junk_still_renders(self):
        data = {"associatedMedia": [{"contentUrl": 1}, "not-a-dict", None, {}]}
        self.assertEqual(artifacts.public_media(data), [{"contentUrl": 1}, {}])


class PostgresOnlyRejectionTests(TestCase):
    """NUL in a text value is a production-only failure.

    A PDF's text layer can carry ``\\x00`` — a broken encoder, a padded font
    table, a damaged stream — and psycopg rejects the whole INSERT. sqlite,
    which is the entire test gate, stores it happily. So this is exactly the
    class of defect that passes every test and dies on the first real document.
    """

    databases = {"default", "ngm"}

    def test_a_nul_byte_never_reaches_the_column(self):
        material = _material("aaa", [_pdf_media()])
        bundle = ContentBundle(
            doc_id="aaa",
            dataset="jawafdehi:pdf-extract/v1",
            page_count=1,
            pages=[PageRecord(page_no=1, text="before\x00after", chars=12)],
            tables=[
                TableRecord(
                    page_no=1,
                    markdown="| a\x00b |",
                    caption="cap\x00tion",
                    header=["h\x00dr"],
                )
            ],
        )

        store_document_content(material.pk, bundle)

        # Queried rather than via material.content: the reverse one-to-one is
        # cached on the instance from before the write.
        content = DocumentContent.objects.using("ngm").get(pk=material.pk)
        page = content.pages.get()
        table = content.tables.get()
        self.assertNotIn("\x00", page.text)
        self.assertNotIn("\x00", table.markdown)
        self.assertNotIn("\x00", table.caption)
        self.assertNotIn("\x00", table.header[0])
        # And nothing readable was lost on the way.
        self.assertEqual(page.text, "beforeafter")

    def test_cleaning_leaves_real_text_alone(self):
        self.assertEqual(_clean("नागरिकता नं. १२-०१"), "नागरिकता नं. १२-०१")


class RedactionMarkerTests(TestCase):
    """The marker has to survive the column it is stored in."""

    def test_a_long_text_source_keeps_its_marker_whole(self):
        bundle = ContentBundle(doc_id="d", dataset="x", text_source="a" * 35)

        redact_bundle(bundle)

        # text_source is 40 characters. Appending past that cut the marker
        # mid-word, which both lost the claim and stopped the idempotence check
        # recognising its own mark — so a re-extract appended again, every time.
        self.assertLessEqual(len(bundle.text_source), 40)
        self.assertTrue(bundle.text_source.endswith(REDACTED_SUFFIX))

    def test_redacting_a_long_source_twice_is_still_idempotent(self):
        bundle = ContentBundle(doc_id="d", dataset="x", text_source="a" * 35)

        redact_bundle(bundle)
        once = bundle.text_source
        redact_bundle(bundle)

        self.assertEqual(bundle.text_source, once)


class GivingUpTests(TestCase):
    """A document we already failed on must not be retried forever.

    With a PDF, no content and a dead job, a material is indistinguishable from
    one nothing has reached yet — so both the on-write receiver and the weekly
    sweep re-queue it: the same encrypted PDF fetched and failed every week
    until somebody reads the job table.
    """

    databases = {"default", "ngm"}

    def _dead_job(self, ident: str, sha: str) -> Job:
        return Job.objects.create(
            kind=material_extract.EXTRACT_KIND,
            status=Job.DEAD,
            payload={"material_iri": _iri(ident), "sha256": sha},
        )

    def test_the_sweep_skips_and_counts_a_document_it_gave_up_on(self):
        _material("aaa", [_pdf_media()])
        Job.objects.all().delete()
        self._dead_job("aaa", DIGEST)
        out = StringIO()

        call_command("extract_document_content", stdout=out)

        self.assertEqual(
            Job.objects.filter(status=Job.QUEUED).count(), 0, "re-queued a dead document"
        )
        # Counted, not silently skipped: a source whose documents all fail looks
        # identical to one that is fully read.
        self.assertIn("1 failed before on these same bytes", out.getvalue())

    def test_a_replaced_file_earns_a_fresh_attempt(self):
        _material("aaa", [_pdf_media()])
        Job.objects.all().delete()
        self._dead_job("aaa", "b" * 64)  # we failed on DIFFERENT bytes

        call_command("extract_document_content", stdout=StringIO())

        self.assertEqual(Job.objects.filter(status=Job.QUEUED).count(), 1)

    def test_force_overrides_having_given_up(self):
        _material("aaa", [_pdf_media()])
        Job.objects.all().delete()
        self._dead_job("aaa", DIGEST)

        call_command("extract_document_content", "--force", stdout=StringIO())

        self.assertEqual(Job.objects.filter(status=Job.QUEUED).count(), 1)


class ScanFrontierTests(TestCase):
    """The sweep has to be able to reach the end of the archive.

    ~94% of materials carry no PDF. A cursor derived from the job history only
    advances past rows that produced a job, so a contiguous region of
    non-extractable materials bigger than one tick's budget stalls the walk
    forever — the same rows re-read every week, the same nothing found, and a
    report that says everything is fine.
    """

    databases = {"default", "ngm"}

    def test_the_scan_advances_over_materials_it_queues_nothing_for(self):
        for ident in ("aaa", "bbb"):
            _material(ident, [])  # no PDF: nothing to queue, ever

        call_command("extract_document_content", stdout=StringIO())

        scan = ExtractionScan.objects.using("ngm").get(pk=1)
        # The walk moved even though the queue did not, which is the entire
        # point: position is what was LOOKED AT, not what was queued.
        self.assertEqual(scan.laps, 1)

    def test_a_second_pass_starts_after_the_first(self):
        _material("aaa", [_pdf_media()])
        _material("bbb", [_pdf_media()])

        call_command("extract_document_content", "--limit", "1", stdout=StringIO())
        out = StringIO()
        call_command("extract_document_content", "--limit", "1", stdout=out)

        self.assertIn("resuming after", out.getvalue())
        self.assertEqual(
            sorted(j.payload["material_iri"] for j in Job.objects.all()),
            [_iri("aaa"), _iri("bbb")],
        )

    def test_reaching_the_end_wraps_rather_than_parking(self):
        _material("aaa", [])

        call_command("extract_document_content", stdout=StringIO())
        call_command("extract_document_content", stdout=StringIO())

        # A finished archive still gains documents; a sweep parked at the end
        # would never see them.
        scan = ExtractionScan.objects.using("ngm").get(pk=1)
        self.assertEqual(scan.laps, 2)
        self.assertEqual(scan.last_iri, "")

    def test_naming_an_iri_ignores_the_frontier(self):
        _material("aaa", [_pdf_media()])
        ExtractionScan.objects.using("ngm").create(pk=1, last_iri=_iri("zzz"))

        call_command("extract_document_content", "--iri", _iri("aaa"), stdout=StringIO())

        # An operator asking for one document means that document, wherever the
        # walk happens to be.
        self.assertEqual(Job.objects.count(), 1)


class ForceUpgradeRaceTests(TestCase):
    """Upgrading a waiting job must not rewrite one a worker has claimed."""

    databases = {"default", "ngm"}

    def test_a_running_job_is_not_rewritten(self):
        _material("aaa", [_pdf_media()])
        Job.objects.all().delete()
        job = Job.objects.create(
            kind=material_extract.EXTRACT_KIND,
            status=Job.RUNNING,
            payload={"material_iri": _iri("aaa"), "force": False, "stage": "claimed"},
            dedup_key=f"{material_extract.EXTRACT_KIND}:{_iri('aaa')}",
        )

        material_extract.enqueue_material_extract(_iri("aaa"), force=True)

        job.refresh_from_db()
        # The worker has already read this payload, so writing to it changes
        # nothing it will act on — and clobbers what the claim recorded.
        self.assertFalse(job.payload["force"])
        self.assertEqual(job.payload["stage"], "claimed")

    def test_a_queued_job_is_upgraded(self):
        _material("aaa", [_pdf_media()])
        Job.objects.all().delete()
        material_extract.enqueue_material_extract(_iri("aaa"), force=False)

        material_extract.enqueue_material_extract(_iri("aaa"), force=True)

        self.assertEqual(Job.objects.count(), 1)
        self.assertTrue(Job.objects.get().payload["force"])


class WorkerStalenessTests(TestCase):
    """The worker must not defeat the thing that detected the change.

    A replaced file is queued by the on-write receiver WITHOUT force — the file
    changed, nobody asked for anything — so a worker that skips on "content
    exists" skips exactly the case staleness detection exists to catch, and the
    document goes on describing bytes nobody can fetch.
    """

    databases = {"default", "ngm"}

    def _with_content(self, ident: str, stored_digest: str) -> Material:
        material = _material(ident, [_pdf_media()])
        store_document_content(
            material.pk,
            ContentBundle(
                doc_id=ident,
                dataset="jawafdehi:pdf-extract/v1",
                dataset_revision=f"sha256:{stored_digest}",
                pages=[PageRecord(page_no=1, text="पुरानो")],
            ),
        )
        return material

    def test_matching_bytes_are_skipped(self):
        self._with_content("aaa", DIGEST)

        result = material_extract.handle_material_extract(
            {"material_iri": _iri("aaa"), "source_urls": [PDF], "sha256": DIGEST}
        )

        self.assertEqual(result["skipped"], "already extracted")

    def test_different_bytes_are_read_again(self):
        self._with_content("aaa", "b" * 64)
        seen = {}

        def _fake_fetch(url, **kwargs):
            seen["url"] = url
            raise material_extract.DocumentFetchError("stop here, the point is made")

        with patch.object(material_extract, "fetch_document", _fake_fetch):
            with self.assertRaises(material_extract.DocumentFetchError):
                material_extract.handle_material_extract(
                    {
                        "material_iri": _iri("aaa"),
                        "source_urls": [PDF],
                        "sha256": DIGEST,
                    }
                )

        # It got as far as fetching, which is the assertion: the old worker
        # returned "already extracted" without opening anything.
        self.assertEqual(seen["url"], PDF)

    def test_an_unknown_digest_leaves_stored_content_alone(self):
        self._with_content("aaa", DIGEST)

        result = material_extract.handle_material_extract(
            {"material_iri": _iri("aaa"), "source_urls": [PDF], "sha256": ""}
        )

        # The scraped-link case. Treating unknown as changed would re-read the
        # archive on every save.
        self.assertEqual(result["skipped"], "already extracted")


class RetryFailedTests(TestCase):
    """Giving up has to be reversible without hand-editing the job table."""

    databases = {"default", "ngm"}

    def test_retry_failed_re_attempts_a_document_we_gave_up_on(self):
        _material("aaa", [_pdf_media()])
        Job.objects.all().delete()
        Job.objects.create(
            kind=material_extract.EXTRACT_KIND,
            status=Job.DEAD,
            payload={"material_iri": _iri("aaa"), "sha256": DIGEST},
        )

        call_command("extract_document_content", "--retry-failed", stdout=StringIO())

        # A failure can be a portal outage or a 503 from storage. Skipping
        # forever is the right default and the wrong permanent answer.
        self.assertEqual(Job.objects.filter(status=Job.QUEUED).count(), 1)
