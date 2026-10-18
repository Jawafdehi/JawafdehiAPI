"""Four artifacts per document, and which of them a payload publishes.

A material used to carry two things a reader could open — the PDF and the
transcript — and published both as-is, identifiers included. There are now four:
original and redacted, for each. What is pinned here is the publication rule and
the two distinctions it rests on.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_artifacts.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from materials import artifacts
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

SOURCE = "official_report"
IDENT = "oag-13463"
IRI = f"https://jawafdehi.org/material/{SOURCE}/{IDENT}"
URL = f"/api/materials/{SOURCE}/{IDENT}"

OURS = "https://s3.jawafdehi.org/case_uploads/abc123.pdf"
OURS_REDACTED = "https://s3.jawafdehi.org/case_uploads/def456.pdf"
OURS_MD = "https://s3.jawafdehi.org/case_uploads/aaa.md"
OURS_MD_REDACTED = "https://s3.jawafdehi.org/case_uploads/bbb.md"
PUBLISHERS = "https://oag.gov.np/reports/63rd-annual-report.pdf"


def _mo(url: str, role: str) -> dict:
    return {"@type": "MediaObject", "contentUrl": url, "jawafdehi:linkRole": role}


FOUR = [
    _mo(OURS, "RAW"),
    _mo(OURS_REDACTED, "REDACTED_PDF"),
    _mo(OURS_MD, "MARKDOWN"),
    _mo(OURS_MD_REDACTED, "REDACTED_MARKDOWN"),
]


def _seed(media: list[dict]) -> Material:
    return Material.objects.create(
        iri=IRI,
        material_type="report",
        source=SOURCE,
        ident=IDENT,
        data={
            "@context": MATERIAL_CONTEXT,
            "@type": "DigitalDocument",
            "@id": IRI,
            "name": {"ne": "महालेखापरीक्षकको वार्षिक प्रतिवेदन"},
            "associatedMedia": media,
        },
    )


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
class WhoseCopyTests(TestCase):
    """The rule is not redacted-vs-original. It is whose copy it is."""

    def test_our_unredacted_copies_are_withheld(self):
        self.assertTrue(artifacts.is_withheld(_mo(OURS, "RAW")))
        self.assertTrue(artifacts.is_withheld(_mo(OURS_MD, "MARKDOWN")))

    def test_the_publishers_own_link_is_never_withheld(self):
        # Hiding this would break the citation chain and protect nothing: the
        # government published that file, and a reader has to be able to check a
        # quote against it.
        self.assertFalse(artifacts.is_withheld(_mo(PUBLISHERS, "RAW")))

    def test_a_redacted_rendering_is_published_whoever_hosts_it(self):
        self.assertFalse(artifacts.is_withheld(_mo(OURS_REDACTED, "REDACTED_PDF")))
        self.assertFalse(
            artifacts.is_withheld(_mo(OURS_MD_REDACTED, "REDACTED_MARKDOWN"))
        )

    def test_the_publishers_web_page_is_a_citation_not_a_copy(self):
        self.assertFalse(
            artifacts.is_withheld(_mo("https://oag.gov.np/news/12", "SOURCE_PAGE"))
        )


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
class ArtifactSummaryTests(TestCase):
    def test_it_reports_all_four_when_all_four_exist(self):
        summary = artifacts.artifact_summary({"associatedMedia": FOUR})

        self.assertEqual(summary["pdf"], {"original": True, "redacted": True})
        self.assertEqual(summary["transcript"], {"original": True, "redacted": True})
        self.assertEqual(summary["withheld_from_this_payload"], 2)

    def test_holding_an_original_is_reported_even_though_it_is_withheld(self):
        # "We hold an original and are not publishing it" and "no original
        # exists" are different facts about a document. A reader comparing our
        # redacted copy against the record needs to know which they are looking
        # at — and saying THAT we hold it is not handing it over.
        summary = artifacts.artifact_summary({"associatedMedia": FOUR})
        public = artifacts.public_media({"associatedMedia": FOUR})

        self.assertTrue(summary["pdf"]["original"])
        self.assertNotIn(OURS, [m["contentUrl"] for m in public])

    def test_a_document_with_only_an_original_says_the_redaction_is_absent(self):
        summary = artifacts.artifact_summary({"associatedMedia": [_mo(OURS, "RAW")]})

        # Not a zero, not a silence: there is no redacted PDF for this document,
        # which is why the public payload carries no PDF at all.
        self.assertEqual(summary["pdf"], {"original": True, "redacted": False})
        self.assertEqual(artifacts.public_media({"associatedMedia": [_mo(OURS, "RAW")]}), [])


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
class DetailPayloadTests(APITestCase):
    databases = {"default", "ngm"}

    def setUp(self):
        _seed(FOUR)

    def test_a_signed_out_reader_gets_only_the_redacted_artifacts(self):
        response = self.client.get(URL)

        urls = [m["contentUrl"] for m in response.data["associatedMedia"]]
        self.assertEqual(sorted(urls), sorted([OURS_REDACTED, OURS_MD_REDACTED]))
        self.assertEqual(response.data["jawafdehi:artifacts"]["withheld_from_this_payload"], 2)

    def test_a_caseworker_does_not_get_the_unredacted_copies(self):
        user = get_user_model().objects.create_user("cw", password="x")
        user.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
        self.client.force_authenticate(user)

        response = self.client.get(URL)

        # The content role is the largest staff group on the platform and exists
        # to author and moderate cases — which needs the documents, not the
        # identifiers printed in them. Attaching unredacted access to it would
        # make "can edit a case" and "can read someone's citizenship number" one
        # grant, which is not a decision anyone would make on purpose.
        urls = [m["contentUrl"] for m in response.data["associatedMedia"]]
        self.assertEqual(sorted(urls), sorted([OURS_REDACTED, OURS_MD_REDACTED]))

    def test_an_admin_gets_all_four(self):
        admin = get_user_model().objects.create_superuser("root", password="x")
        self.client.force_authenticate(admin)

        response = self.client.get(URL)

        # The Zitadel `admin` role maps to is_superuser rather than to a group,
        # and until a dedicated role exists these are the only principals who
        # should see an unredacted copy.
        urls = [m["contentUrl"] for m in response.data["associatedMedia"]]
        self.assertEqual(len(urls), 4)
        self.assertIn(OURS, urls)
        self.assertIn(OURS_MD, urls)

    def test_a_role_an_admin_granted_the_permission_gets_all_four(self):
        # The point of a permission rather than a role check: an administrator
        # creates a role, maps it to a group, ticks this box in /django-admin/,
        # and nobody deploys anything.
        group = Group.objects.create(name="PrivacyReviewer")
        group.permissions.add(
            Permission.objects.get(
                codename="view_unredacted",
                content_type=ContentType.objects.get_for_model(Material),
            )
        )
        user = get_user_model().objects.create_user("reviewer", password="x")
        user.groups.add(group)
        self.client.force_authenticate(user)

        response = self.client.get(URL)

        urls = [m["contentUrl"] for m in response.data["associatedMedia"]]
        self.assertEqual(len(urls), 4)

    def test_one_url_two_representations_so_the_response_varies(self):
        response = self.client.get(URL)

        # Without this a shared cache can hand the caseworker's body — which
        # lists our unredacted copies — to the next anonymous caller.
        self.assertIn("Authorization", response.get("Vary", ""))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_publishers_link_survives_for_everyone(self):
        Material.objects.filter(pk=IRI).delete()
        _seed([_mo(PUBLISHERS, "RAW"), _mo(OURS_MD, "MARKDOWN")])

        response = self.client.get(URL)

        urls = [m["contentUrl"] for m in response.data["associatedMedia"]]
        self.assertEqual(urls, [PUBLISHERS])


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
class MalformedJsonbTests(TestCase):
    """``associatedMedia`` is free-form JSONB and not everything in it is a string.

    This gate runs on the material detail payload — the hottest public read in
    the archive — so a type error here is a 500 on a document a reader can
    otherwise see perfectly well. The codebase already guards the same field in
    ``materials.conversion``; this is the second place it has to hold.
    """

    def test_a_non_string_url_does_not_raise(self):
        for value in (12345, None, ["https://x"], {"@id": "y"}):
            with self.subTest(value=value):
                media = {"contentUrl": value, "jawafdehi:linkRole": "RAW"}
                self.assertFalse(artifacts.is_withheld(media))

    def test_a_non_string_role_is_treated_as_raw_and_withheld(self):
        # The SAFE default, not the lenient one: a link of ours whose role we
        # cannot read is withheld rather than published.
        media = {"contentUrl": OURS, "jawafdehi:linkRole": 7}
        self.assertTrue(artifacts.is_withheld(media))

    def test_a_whole_payload_of_junk_still_renders(self):
        data = {"associatedMedia": [{"contentUrl": 1}, "not-a-dict", None, {}]}
        self.assertEqual(artifacts.public_media(data), [{"contentUrl": 1}, {}])
