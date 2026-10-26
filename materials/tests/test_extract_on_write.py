"""A document is queued for reading the moment its material is written.

Before this, exactly one write path asked for a document to be read — the upload
endpoint. Five scrapers, the court importer, the seed commands and every PATCH
that attaches a file did not, which is a large part of why ~94% of the archive
has never been read. All of them funnel through ``upsert_single_source_material``
and therefore through ``post_save``, so one receiver covers them and the next
path nobody has written yet.

What is pinned here is the receiver's guards, because each of them is something
that would otherwise be paid on every material write in the platform.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_extract_on_write.py
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from jobs.models import Job
from materials import extract as material_extract
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import DocumentContent, Material
from materials.provenance import PROVENANCE_KEY

SOURCE = "ciaa_annual_report"
PDF = "https://s3.jawafdehi.org/case_uploads/abc.pdf"
DIGEST = "a" * 64


def _media(url: str = PDF, *, sha: str | None = DIGEST, role: str = "RAW") -> dict:
    media = {"@type": "MediaObject", "contentUrl": url, "jawafdehi:linkRole": role}
    if sha is not None:
        media[PROVENANCE_KEY] = {"fetch_method": "upload", "sha256": sha}
    return media


def _write(ident: str, media: list[dict] | None = None, **kwargs) -> Material:
    iri = f"https://jawafdehi.org/material/{SOURCE}/{ident}"
    data = {
        "@context": MATERIAL_CONTEXT,
        "@type": "DigitalDocument",
        "@id": iri,
        "name": {"ne": "प्रतिवेदन"},
    }
    if media is not None:
        data["associatedMedia"] = media
    return Material.objects.create(
        iri=iri,
        material_type="report",
        source=SOURCE,
        ident=ident,
        data=data,
        **kwargs,
    )


class OnWriteTriggerTests(TestCase):
    databases = {"default", "ngm"}

    def _commit(self):
        """Run the receiver's deferred work, on the alias it was deferred to.

        The trigger defers to ``transaction.on_commit`` so a queue round-trip
        never happens while the row lock is held — and ``using="ngm"`` is
        load-bearing in both the receiver and here: Material lives on ``ngm``,
        and a callback registered there is invisible to a capture on
        ``default``.
        """
        return self.captureOnCommitCallbacks(using="ngm", execute=True)

    def test_writing_a_material_with_a_pdf_queues_it(self):
        with self._commit():
            material = _write("aaa", [_media()])

        job = Job.objects.get()
        self.assertEqual(job.kind, material_extract.EXTRACT_KIND)
        self.assertEqual(job.payload["material_iri"], material.pk)
        # Ahead of the sweep's backfill: somebody writing a document is the best
        # signal available that somebody is about to read it.
        self.assertEqual(job.priority, material_extract.PRIORITY_ON_WRITE)

    def test_a_material_with_no_document_queues_nothing(self):
        # The common case by far — most materials are a scraped page, not a PDF
        # — so this guard is what keeps the receiver cheap enough to sit on every
        # material write in the platform.
        with self._commit():
            _write("aaa", [])
            _write("bbb", [_media("https://ciaa.gov.np/news/12", role="SOURCE_PAGE")])

        self.assertEqual(Job.objects.count(), 0)

    def test_a_soft_deleted_material_is_not_read(self):
        with self._commit():
            _write("aaa", [_media()], is_deleted=True)

        # A takedown is a deliberate editorial act. Reading the document we were
        # asked to withdraw is the opposite of honouring it.
        self.assertEqual(Job.objects.count(), 0)

    def test_saving_again_does_not_queue_a_second_job(self):
        with self._commit():
            material = _write("aaa", [_media()])

        with self._commit():
            material.data = {**material.data, "name": {"ne": "सच्याइएको"}}
            material.save()

        # Dedup on the IRI: two workers reading one document would each replace
        # the other's rows, and a reader mid-read would see them vanish.
        self.assertEqual(Job.objects.count(), 1)

    def test_a_document_we_have_already_read_is_not_re_read(self):
        with self._commit():
            material = _write("aaa", [_media()])
        Job.objects.all().delete()
        DocumentContent.objects.using("ngm").create(
            material=material,
            doc_id="aaa",
            dataset="jawafdehi:pdf-extract/v1",
            dataset_revision=f"sha256:{DIGEST}",
        )

        with self._commit():
            material.save()

        self.assertEqual(Job.objects.count(), 0)

    def test_a_replaced_file_is_read_again(self):
        with self._commit():
            material = _write("aaa", [_media()])
        Job.objects.all().delete()
        DocumentContent.objects.using("ngm").create(
            material=material,
            doc_id="aaa",
            dataset="jawafdehi:pdf-extract/v1",
            dataset_revision=f"sha256:{DIGEST}",
        )

        # A corrected scan, a re-upload, a publisher reissuing the report.
        with self._commit():
            material.data = {**material.data, "associatedMedia": [_media(sha="b" * 64)]}
            material.save()

        # Without this the document keeps content describing bytes that no
        # longer exist, and a page citation points into a file nobody can get.
        self.assertEqual(Job.objects.count(), 1)

    def test_an_unknown_digest_is_not_treated_as_a_changed_one(self):
        with self._commit():
            material = _write("aaa", [_media(sha=None)])
        Job.objects.all().delete()
        DocumentContent.objects.using("ngm").create(
            material=material,
            doc_id="aaa",
            dataset="jawafdehi:pdf-extract/v1",
            dataset_revision="sha256:whatever",
        )

        with self._commit():
            material.save()

        # Scraped links routinely carry no provenance. Treating "unknown" as
        # "changed" would re-read the entire scraped archive on every save.
        self.assertEqual(Job.objects.count(), 0)

    def test_a_queue_outage_does_not_fail_the_write(self):
        with patch.object(
            material_extract,
            "enqueue_material_extract",
            side_effect=RuntimeError("queue down"),
        ):
            with self._commit():
                material = _write("aaa", [_media()])

        # The receiver runs inside every material write in the platform. A queue
        # that is down must slow nothing and break nothing; the sweep picks up
        # what was missed.
        self.assertTrue(Material.objects.filter(pk=material.pk).exists())
        self.assertEqual(Job.objects.count(), 0)
