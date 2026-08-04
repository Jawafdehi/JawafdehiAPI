"""End-to-end smoke tests for material_convert.

Unlike ``test_material_convert.py`` (which unit-tests each seam in isolation with
a fake job), these drive the FULL real path through the actual queue engine and
the real upload endpoint, mocking ONLY the true external boundaries:

- the document conversion network call (``review.converter.convert_source`` —
  would hit the source URL + Bedrock OCR),
- object storage (``store_file_as_link`` — would write to R2),
- the OpenSearch index upsert (``materials.search_index.index``).

So the queue's real enqueue → claim (build_payload) → worker handler →
finalize (on_result) chain and the real signal-driven reindex are all exercised.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from jobs import queue as jobs_queue
from jobs.models import Job
from materials.models import Material

User = get_user_model()


def _run_one_convert_job():
    """Claim the next material_convert job, run its worker handler, finalize.

    This is what ``review_poller --apply --kinds material_convert`` does per job,
    but in-process (no HTTP), so the smoke test needs no live server. Returns the
    finalized Job, or None if the queue was empty.
    """
    from materials.job_handlers import handle_material_convert

    job = jobs_queue.claim_next(["material_convert"])
    if job is None:
        return None
    try:
        result = handle_material_convert(job.payload, on_stage=lambda s: None)
        return jobs_queue.finalize(job, status=Job.DONE, result=result)
    except Exception as exc:  # noqa: BLE001 - mirror the poller: report a retryable failure
        return jobs_queue.finalize(
            job, status=Job.FAILED, error=str(exc), retryable=True
        )


class MaterialConvertSmokeTests(APITestCase):
    databases = "__all__"

    URL = "/api/materials/nkp/smoke-order-1/file"
    IRI = "https://jawafdehi.org/material/nkp/smoke-order-1"

    @classmethod
    def setUpTestData(cls):
        g, _ = Group.objects.get_or_create(name="Caseworker")
        cls.user = User.objects.create(username="oidc-writer")
        cls.user.groups.add(g)

    def _pdf(self, name="order.pdf"):
        return SimpleUploadedFile(name, b"%PDF-1.4 data", content_type="application/pdf")

    def test_upload_enqueues_and_convert_populates_text_and_search(self):
        """The whole feed: upload → job enqueued → run → data['text'] + reindex."""
        self.client.force_authenticate(user=self.user)

        indexed: list[str] = []

        def _record_index(obj, **kwargs):
            # Capture what got (re)indexed + its body, proving the search feed.
            indexed.append(obj.data.get("text", {}).get("ne", ""))

        # Two distinct storage bindings: the upload endpoint binds
        # store_file_as_link at module import (materials.views); conversion.py
        # imports it locally. Patch both so RAW (upload) and MARKDOWN (on_result)
        # land at deterministic URLs. In prod both call the same real R2 helper.
        with patch(
            "materials.views.store_file_as_link",
            return_value={"link": "https://cdn/raw.pdf", "role": "RAW"},
        ), patch(
            "jawafdehi_shared.storage.store_file_as_link",
            return_value={"link": "https://cdn/extracted.md", "role": "MARKDOWN"},
        ), patch(
            "materials.search_index.index", side_effect=_record_index
        ):
            # 1) Upload the source PDF.
            resp = self.client.post(
                self.URL,
                {"file": self._pdf(), "material_type": "court_order"},
                format="multipart",
            )
            self.assertEqual(resp.status_code, 201, resp.data)

            # 2) A material_convert job was enqueued (transactionally, deduped).
            job = Job.objects.get(kind="material_convert")
            self.assertEqual(job.status, Job.QUEUED)
            self.assertEqual(job.dedup_key, f"material_convert:{self.IRI}")
            self.assertEqual(job.payload["material_iri"], self.IRI)

            # 3) Run the job through the REAL queue engine, with the conversion
            #    network/OCR call mocked to return markdown. captureOnCommitCallbacks
            #    must name `ngm` — that is where Material lives, and therefore the
            #    connection its reindex on_commit hook is registered against.
            #    forces the Material post_save→reindex on_commit hook to actually
            #    run (APITestCase wraps each test in a rolled-back transaction, so
            #    on_commit callbacks would otherwise never fire).
            with patch(
                "review.converter.convert_source",
                return_value={
                    "markdown": "अदालतको आदेश — पूर्ण पाठ",
                    "status": "converted",
                    "url": "https://cdn/raw.pdf",
                    "note": "",
                },
            ), self.captureOnCommitCallbacks(using="ngm", execute=True):
                done = _run_one_convert_job()

        # 4) Job finished cleanly.
        self.assertIsNotNone(done)
        self.assertEqual(done.status, Job.DONE)
        self.assertEqual(done.result["text"], "अदालतको आदेश — पूर्ण पाठ")

        # 5) The Material now carries searchable full text + a MARKDOWN link.
        row = Material.objects.get(pk=self.IRI)
        self.assertEqual(row.data["text"], {"ne": "अदालतको आदेश — पूर्ण पाठ"})
        roles = [m["jawafdehi:linkRole"] for m in row.data["associatedMedia"]]
        self.assertIn("RAW", roles)
        self.assertIn("MARKDOWN", roles)
        md = next(
            m for m in row.data["associatedMedia"]
            if m["jawafdehi:linkRole"] == "MARKDOWN"
        )
        self.assertEqual(md["contentUrl"], "https://cdn/extracted.md")
        # The MARKDOWN link carries OCR provenance (archive-plane record).
        md_prov = md["jawafdehi:provenance"]
        self.assertEqual(md_prov["fetch_method"], "ocr")
        self.assertEqual(md_prov["ocr_engine"], "likhit")
        self.assertEqual(len(md_prov["sha256"]), 64)

        # 6) The reindex actually fired with the extracted body (the search feed).
        self.assertIn("अदालतको आदेश — पूर्ण पाठ", indexed)

    def test_reupload_while_queued_does_not_double_enqueue(self):
        """Dedup on the IRI: a second upload before the convert runs is a no-op."""
        self.client.force_authenticate(user=self.user)
        with patch(
            "jawafdehi_shared.storage.store_file_as_link",
            return_value={"link": "https://cdn/raw.pdf", "role": "RAW"},
        ), patch("materials.search_index.index"):
            self.client.post(
                self.URL,
                {"file": self._pdf(), "material_type": "court_order"},
                format="multipart",
            )
            self.client.post(
                self.URL, {"file": self._pdf()}, format="multipart"
            )
        self.assertEqual(Job.objects.filter(kind="material_convert").count(), 1)

    def test_convert_failure_leaves_material_served_without_text(self):
        """A conversion failure dead-letters/retries the job but never corrupts
        the Material: it stays served, just without full text."""
        self.client.force_authenticate(user=self.user)
        with patch(
            "jawafdehi_shared.storage.store_file_as_link",
            return_value={"link": "https://cdn/raw.pdf", "role": "RAW"},
        ), patch("materials.search_index.index"):
            self.client.post(
                self.URL,
                {"file": self._pdf(), "material_type": "court_order"},
                format="multipart",
            )
            with patch(
                "review.converter.convert_source",
                return_value={"markdown": "", "status": "error",
                              "url": "x", "note": "download 404"},
            ):
                done = _run_one_convert_job()

        # Job did not succeed (retry re-queued since retryable + attempts remain).
        self.assertIsNotNone(done)
        self.assertNotEqual(done.status, Job.DONE)
        # Material still exists and is served; just no text yet.
        row = Material.objects.get(pk=self.IRI)
        self.assertNotIn("text", row.data)
