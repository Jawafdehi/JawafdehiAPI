"""Tests for the shared-media detector.

The job this command has to get right is not "find duplicates" — a group-by does
that. It is to tell the reader WHICH duplicates mean something: one external
document legitimately backing several materials, versus one of our own stored
artifacts being shared, which means a material may be serving another
document's text. A report that mixes the two trains people to skim past it.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

OURS = "https://s3.jawafdehi.org/case_uploads"
EXTERNAL = "https://supremecourt.gov.np/cp/assets/uploads"


def seed(source: str, ident: str, media: list[dict], **extra) -> Material:
    iri = f"https://jawafdehi.org/material/{source}/{ident}"
    data = {
        "@context": MATERIAL_CONTEXT,
        "@id": iri,
        "@type": "DigitalDocument",
        "name": {"en": ident},
        "associatedMedia": media,
        **extra,
    }
    return Material.objects.create(
        iri=iri, source=source, ident=ident, material_type="document", data=data
    )


def link(url: str, role: str = "RAW") -> dict:
    return {"@type": "MediaObject", "contentUrl": url, "jawafdehi:linkRole": role}


def run(*args) -> str:
    out = StringIO()
    call_command("find_shared_media", *args, stdout=out)
    return out.getvalue()


class SharedMediaTests(TestCase):
    databases = "__all__"

    def test_an_artifact_of_ours_shared_by_two_materials_is_reported(self):
        seed("document", "a", [link(f"{OURS}/collide.md", "MARKDOWN")])
        seed("document", "b", [link(f"{OURS}/collide.md", "MARKDOWN")])
        out = run()
        self.assertIn("OUR OWN ARTIFACTS", out)
        self.assertIn("collide.md", out)
        self.assertIn("/material/document/a", out)
        self.assertIn("/material/document/b", out)

    def test_a_shared_external_url_is_hidden_by_default(self):
        # One upstream PDF backing two materials is normal provenance, not a
        # defect. Reporting it next to a real collision is how a report stops
        # being read.
        seed("document", "a", [link(f"{EXTERNAL}/special_1.doc")])
        seed("document", "b", [link(f"{EXTERNAL}/special_1.doc")])
        out = run()
        self.assertNotIn("special_1.doc", out)
        self.assertIn("pass --all", out)
        self.assertIn("no shared artifacts of our own", out)

    def test_all_surfaces_the_external_class_separately(self):
        seed("document", "a", [link(f"{EXTERNAL}/special_1.doc")])
        seed("document", "b", [link(f"{EXTERNAL}/special_1.doc")])
        out = run("--all")
        self.assertIn("EXTERNAL urls", out)
        self.assertIn("special_1.doc", out)

    def test_a_url_used_once_is_not_reported(self):
        seed("document", "a", [link(f"{OURS}/alone.pdf")])
        self.assertIn("no shared artifacts of our own", run())

    def test_role_filter_narrows_to_the_likeliest_defect(self):
        # The same two materials share a RAW (plausible: one source PDF) and
        # NOT a transcript. Filtering to MARKDOWN must clear them.
        seed("document", "a", [link(f"{OURS}/src.pdf"), link(f"{OURS}/a.md", "MARKDOWN")])
        seed("document", "b", [link(f"{OURS}/src.pdf"), link(f"{OURS}/b.md", "MARKDOWN")])
        self.assertIn("src.pdf", run())
        self.assertIn("no shared artifacts of our own", run("--role", "MARKDOWN"))

    def test_empty_content_urls_do_not_bucket_together(self):
        # A blank contentUrl is its own data defect; grouping every one under ""
        # would report a single enormous collision that is not real.
        seed("document", "a", [link("", "RAW")])
        seed("document", "b", [link("", "RAW")])
        seed("document", "c", [link("   ", "RAW")])
        self.assertIn("no shared artifacts of our own", run())

    def test_a_malformed_associatedMedia_does_not_crash_the_sweep(self):
        # A sweep whose whole purpose is finding bad data must survive bad data.
        seed("document", "a", [link(f"{OURS}/x.pdf")])
        broken = seed("document", "b", [])
        broken.data["associatedMedia"] = "not-a-list"
        broken.save(update_fields=["data"])
        third = seed("document", "c", [])
        third.data["associatedMedia"] = [None, "str", {"contentUrl": f"{OURS}/x.pdf"}]
        third.save(update_fields=["data"])
        out = run()
        self.assertIn("x.pdf", out)
        self.assertIn("scanned 3 materials", out)

    def test_soft_deleted_rows_are_excluded(self):
        # A tombstone sharing an artifact with its survivor is the EXPECTED
        # outcome of a move, not a collision to chase.
        seed("document", "a", [link(f"{OURS}/shared.pdf")])
        dead = seed("document", "b", [link(f"{OURS}/shared.pdf")])
        dead.is_deleted = True
        dead.save(update_fields=["is_deleted"])
        self.assertIn("no shared artifacts of our own", run())

    def test_source_filter_limits_the_scan(self):
        seed("charge_sheet", "a", [link(f"{OURS}/one.pdf")])
        seed("document", "b", [link(f"{OURS}/one.pdf")])
        self.assertIn("scanned 1 materials", run("--source", "charge_sheet"))

    def test_the_command_writes_nothing(self):
        a = seed("document", "a", [link(f"{OURS}/collide.md", "MARKDOWN")])
        b = seed("document", "b", [link(f"{OURS}/collide.md", "MARKDOWN")])
        before = (a.data, b.data, Material.objects.count())
        run()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.data, b.data, Material.objects.count()), before)
