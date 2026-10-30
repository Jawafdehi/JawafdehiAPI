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


def seed(source: str, ident: str, media, **extra) -> Material:
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
        seed("document", "a", [link(f"{OURS}/collide.md", "MARKDOWN")], text={"en": "A"})
        seed("document", "b", [link(f"{OURS}/collide.md", "MARKDOWN")], text={"en": "B"})
        out = run()
        self.assertIn("DAMAGED", out)
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
        self.assertIn("no damaged artifacts", out)

    def test_all_surfaces_the_external_class_separately(self):
        seed("document", "a", [link(f"{EXTERNAL}/special_1.doc")])
        seed("document", "b", [link(f"{EXTERNAL}/special_1.doc")])
        out = run("--all")
        self.assertIn("EXTERNAL urls", out)
        self.assertIn("special_1.doc", out)

    def test_a_url_used_once_is_not_reported(self):
        seed("document", "a", [link(f"{OURS}/alone.pdf")])
        self.assertIn("no damaged artifacts", run())

    def test_role_filter_narrows_to_the_likeliest_defect(self):
        # The same two materials share a RAW (plausible: one source PDF) and
        # NOT a transcript. Filtering to MARKDOWN must clear them.
        seed("document", "a", [link(f"{OURS}/src.pdf"), link(f"{OURS}/a.md", "MARKDOWN")])
        seed("document", "b", [link(f"{OURS}/src.pdf"), link(f"{OURS}/b.md", "MARKDOWN")])
        self.assertIn("src.pdf", run())
        self.assertIn("no damaged artifacts", run("--role", "MARKDOWN"))

    def test_empty_content_urls_do_not_bucket_together(self):
        # A blank contentUrl is its own data defect; grouping every one under ""
        # would report a single enormous collision that is not real.
        seed("document", "a", [link("", "RAW")])
        seed("document", "b", [link("", "RAW")])
        seed("document", "c", [link("   ", "RAW")])
        self.assertIn("no damaged artifacts", run())

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
        self.assertIn("no damaged artifacts", run())

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


class ReviewRegressionTests(TestCase):
    """Cases the review reproduced that the original command got wrong."""

    databases = "__all__"

    def test_one_material_holding_a_url_twice_is_not_shared(self):
        # It counted LINKS. A single document carrying the same file under two
        # roles was reported as "shared by 2" — a false positive on an entirely
        # ordinary row.
        seed(
            "document",
            "a",
            [link(f"{OURS}/f.pdf", "RAW"), link(f"{OURS}/f.pdf", "ALTERNATE")],
        )
        self.assertIn("no damaged artifacts", run())

    def test_an_archive_snapshot_of_our_file_is_not_ours(self):
        # A substring test on the host matched any url merely CONTAINING our
        # domain, so an archive.org snapshot of our own file counted as ours.
        snap = "https://web.archive.org/web/2026/https://s3.jawafdehi.org/case_uploads/x.pdf"
        seed("document", "a", [link(snap)])
        seed("document", "b", [link(snap)])
        out = run()
        self.assertIn("no damaged artifacts", out)
        self.assertIn("pass --all", out)

    def test_a_non_string_content_url_does_not_crash_the_sweep(self):
        seed("document", "a", [link(f"{OURS}/ok.pdf")], text={"en": "A"})
        broken = seed("document", "b", [])
        broken.data["associatedMedia"] = [{"contentUrl": ["not", "a", "string"]}]
        broken.save(update_fields=["data"])
        self.assertIn("scanned 2 materials", run())

    def test_a_single_object_associated_media_is_not_skipped(self):
        # associatedMedia may legitimately be ONE dict rather than a list;
        # those materials were silently invisible to the sweep.
        a = seed("document", "a", [], text={"en": "A"})
        a.data["associatedMedia"] = {"contentUrl": f"{OURS}/solo.md",
                                     "jawafdehi:linkRole": "MARKDOWN"}
        a.save(update_fields=["data"])
        b = seed("document", "b", [], text={"en": "B"})
        b.data["associatedMedia"] = {"contentUrl": f"{OURS}/solo.md",
                                     "jawafdehi:linkRole": "MARKDOWN"}
        b.save(update_fields=["data"])
        self.assertIn("DAMAGED", run())

    def test_a_role_given_as_a_list_does_not_crash(self):
        a = seed("document", "a", [])
        a.data["associatedMedia"] = [{"contentUrl": f"{OURS}/x.md",
                                      "jawafdehi:linkRole": ["MARKDOWN"]}]
        a.save(update_fields=["data"])
        self.assertIn("scanned 1 materials", run())

    def test_identical_text_sharing_one_transcript_is_correct_not_damage(self):
        # Transcripts are content-addressed now (materials/conversion.py), so
        # two documents whose text is byte-identical SHOULD share one object.
        # Calling that a defect would bury the real ones.
        seed("document", "a", [link(f"{OURS}/same.md", "MARKDOWN")], text={"en": "same"})
        seed("document", "b", [link(f"{OURS}/same.md", "MARKDOWN")], text={"en": "same"})
        out = run()
        self.assertIn("no damaged artifacts", out)
        self.assertIn("shared but consistent", out)

    def test_a_mistyped_source_errors_instead_of_reporting_nothing(self):
        # A confident empty report reads exactly like "nothing is wrong".
        import pytest
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            run("--source", "no_such_source")

    def test_the_report_order_is_deterministic(self):
        for i in range(4):
            seed("document", f"a{i}", [link(f"{OURS}/s{i}.md", "MARKDOWN")], text={"en": f"{i}"})
            seed("document", f"b{i}", [link(f"{OURS}/s{i}.md", "MARKDOWN")], text={"en": f"x{i}"})
        self.assertEqual(run(), run())


    def test_a_material_without_a_transcript_is_unknown_not_different(self):
        # Pairing a transcribed row with an untranscribed one is ordinary;
        # counting the gap as a distinct value reported it as damage.
        seed("document", "a", [link(f"{OURS}/s.pdf", "RAW")], text={"en": "A"})
        seed("document", "b", [link(f"{OURS}/s.pdf", "RAW")])
        out = run()
        self.assertIn("no damaged artifacts", out)
        self.assertIn("shared but consistent", out)

    def test_two_untranscribed_materials_are_not_claimed_to_be_fine(self):
        # The honest limit: with no text on either side there is nothing to
        # compare, so this is reported as consistent rather than damaged — and
        # the docstring says so rather than implying full coverage.
        seed("document", "a", [link(f"{OURS}/s.pdf", "RAW")])
        seed("document", "b", [link(f"{OURS}/s.pdf", "RAW")])
        self.assertIn("no damaged artifacts", run())

    def test_disagreeing_text_is_still_damage_when_a_third_row_has_none(self):
        seed("document", "a", [link(f"{OURS}/s.md", "MARKDOWN")], text={"en": "A"})
        seed("document", "b", [link(f"{OURS}/s.md", "MARKDOWN")], text={"en": "B"})
        seed("document", "c", [link(f"{OURS}/s.md", "MARKDOWN")])
        self.assertIn("DAMAGED", run())
