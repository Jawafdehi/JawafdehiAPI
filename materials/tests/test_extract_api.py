"""Asking for a document to be opened: ``POST …/content``.

The on-demand half of the extraction pipeline. What matters here is that each
answer is distinguishable, because they call for different next steps:

- ``200`` we already have it — do not queue, do not re-extract;
- ``202`` queued — poll the GET;
- ``409`` this material has no document we can open — asking again will not help;
- ``429`` the queue is full — asking later will;
- ``404`` no such material (or not visible to you), with whether asking would
  have helped had it existed.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_extract_api.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from jobs.models import Job
from materials import extract as material_extract
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import DocumentContent, Material

SOURCE = "ciaa_annual_report"
IDENT = "ee0b4f80b24b8665"
IRI = f"https://jawafdehi.org/material/{SOURCE}/{IDENT}"
URL = f"/api/materials/{SOURCE}/{IDENT}/content"
PDF_URL = "https://s3.jawafdehi.org/case_uploads/abc123.pdf"


def _seed_material(media: list | None = None) -> Material:
    data = {
        "@context": MATERIAL_CONTEXT,
        "@type": "DigitalDocument",
        "@id": IRI,
        "name": {"ne": "पैँतिसौँ वार्षिक प्रतिवेदन"},
    }
    if media is not None:
        data["associatedMedia"] = media
    return Material.objects.create(
        iri=IRI, material_type="document", source=SOURCE, ident=IDENT, data=data
    )


def _pdf_media(url: str = PDF_URL, **extra) -> dict:
    return {
        "@type": "MediaObject",
        "contentUrl": url,
        "jawafdehi:linkRole": "RAW",
        **extra,
    }


class RequestExtractionTests(APITestCase):
    databases = {"default", "ngm"}

    def setUp(self):
        self.material = _seed_material([_pdf_media()])
        self.user = get_user_model().objects.create_user("caseworker", password="x")
        self.user.groups.add(Group.objects.get_or_create(name="Caseworker")[0])

    def _authenticate(self):
        self.client.force_authenticate(user=self.user)

    def test_anonymous_cannot_spend_the_cluster_on_a_guess(self):
        response = self.client.post(URL)

        # Extraction is minutes of CPU per document over an archive of 346k of
        # them. 401 rather than 404: the route exists, the caller is not known.
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(Job.objects.count(), 0)

    def test_an_authenticated_reader_without_the_role_is_refused(self):
        plain = get_user_model().objects.create_user("reader", password="x")
        self.client.force_authenticate(user=plain)

        response = self.client.post(URL)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Job.objects.count(), 0)

    def test_a_request_queues_one_job_and_says_how_to_wait(self):
        self._authenticate()

        response = self.client.post(URL)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "queued")
        self.assertEqual(response["Retry-After"], "30")
        job = Job.objects.get()
        self.assertEqual(job.kind, "material_extract")
        self.assertEqual(job.payload["material_iri"], IRI)
        self.assertEqual(response.data["job"]["id"], job.pk)
        # Whether this URL resolves depends on the caller, so a shared cache must
        # not serve one answer to another.
        self.assertIn("Authorization", response.get("Vary", ""))

    def test_asking_twice_joins_the_same_job(self):
        self._authenticate()

        first = self.client.post(URL)
        second = self.client.post(URL)

        self.assertEqual(second.status_code, status.HTTP_202_ACCEPTED)
        # Deduped on the IRI: two workers extracting one document would each
        # replace the other's rows, and a reader mid-read would see them vanish.
        self.assertEqual(Job.objects.count(), 1)
        self.assertEqual(first.data["job"]["id"], second.data["job"]["id"])

    def test_a_document_we_already_hold_is_not_re_extracted(self):
        DocumentContent.objects.create(
            material=self.material,
            doc_id=IDENT,
            dataset="jawafdehi:pdf-extract/v1",
            page_count=446,
            n_tables=145,
            pages_with_text=441,
        )
        self._authenticate()

        response = self.client.post(URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ready")
        self.assertEqual(response.data["content"]["tables"], 145)
        # Re-extraction replaces every row and moves the ETag of a document
        # nobody changed, so it stays an operator's explicit call.
        self.assertEqual(Job.objects.count(), 0)
        self.assertTrue(response.has_header("ETag"))

    def test_a_material_with_no_document_is_a_conflict_not_a_bad_request(self):
        self.material.data = {**self.material.data, "associatedMedia": []}
        self.material.save()
        self._authenticate()

        response = self.client.post(URL)

        # The request is well formed and the URL exists; it is the material that
        # has nothing to open. And 409 rather than 404 because the material is
        # right here — everything else about it still reads.
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["status"], "unextractable")
        self.assertEqual(Job.objects.count(), 0)

    def test_an_html_source_page_is_not_a_document_this_pipeline_opens(self):
        self.material.data = {
            **self.material.data,
            "associatedMedia": [
                {
                    "@type": "MediaObject",
                    "contentUrl": "https://ciaa.gov.np/news/12",
                    "jawafdehi:linkRole": "SOURCE_PAGE",
                }
            ],
        }
        self.material.save()
        self._authenticate()

        response = self.client.post(URL)

        # There is no page in a scraped HTML page to anchor a citation to, so
        # this is refused rather than extracted into something weaker.
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_pdf_without_an_extension_is_recognised_by_its_encoding_format(self):
        # R2 keys are hashed, so a stored PDF frequently has no extension at all
        # and the MediaObject is the only statement about what the bytes are.
        self.material.data = {
            **self.material.data,
            "associatedMedia": [
                _pdf_media(
                    "https://s3.jawafdehi.org/case_uploads/9f8e7d",
                    encodingFormat="application/pdf",
                )
            ],
        }
        self.material.save()
        self._authenticate()

        response = self.client.post(URL)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_full_queue_refuses_rather_than_growing(self):
        self._authenticate()
        original = material_extract.MAX_QUEUED_ON_DEMAND
        material_extract.MAX_QUEUED_ON_DEMAND = 1
        self.addCleanup(
            setattr, material_extract, "MAX_QUEUED_ON_DEMAND", original
        )
        Job.objects.create(kind="material_extract", payload={"material_iri": "other"})

        response = self.client.post(URL)

        # A request-triggered queue with no ceiling is a way for one caller's loop
        # over the archive to spend the whole worker budget.
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response["Retry-After"], "300")
        self.assertEqual(Job.objects.filter(payload__material_iri=IRI).count(), 0)

    def test_a_missing_material_is_still_a_404(self):
        self._authenticate()

        response = self.client.post(f"/api/materials/{SOURCE}/nosuchident/content")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Job.objects.count(), 0)


class ExtractionHintTests(APITestCase):
    databases = {"default", "ngm"}

    def test_a_404_says_whether_asking_would_help(self):
        _seed_material([_pdf_media()])

        response = self.client.get(URL, {"type": "page"})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Without this a client cannot tell "nobody has opened this document yet"
        # from "there is no document here to open", and the two have opposite
        # next steps.
        self.assertTrue(response.data["extractable"])

    def test_a_material_with_nothing_to_open_says_so(self):
        _seed_material([])

        response = self.client.get(URL, {"type": "table"})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(response.data["extractable"])
