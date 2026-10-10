"""Tests for the one-off Auditor General corpus corrections.

The command edits published documents in a public archive, so the behaviour
worth pinning is not "does it change the title" — it is everything that stops it
changing the WRONG thing: the dry run writing nothing, a drifted row being left
alone, and a soft-delete refusing to fire when its survivor cannot be proven.

Run under the platform settings (DB-less: sqlite fallback) from the repo root::

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_fix_oag_corpus.py
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.management.commands import fix_oag_corpus
from materials.models import Material

OAG = fix_oag_corpus.OAG_SOURCE


def seed(ident: str, title_en: str, *, text: str = "transcript", **extra) -> Material:
    iri = f"https://jawafdehi.org/material/{OAG}/{ident}"
    doc = {
        "@context": MATERIAL_CONTEXT,
        "@type": "Report",
        "@id": iri,
        "name": {"en": title_en},
        "text": text,
        **extra,
    }
    return Material.objects.create(
        iri=iri, material_type="official_report", source=OAG, ident=ident, data=doc
    )


def run(*, apply: bool = False) -> str:
    out = StringIO()
    try:
        call_command("fix_oag_corpus", apply=apply, stdout=out, stderr=out)
    except SystemExit:
        # Raised when an ident is missing, which is the norm here: these tests
        # seed only the rows they care about, never the whole corpus.
        pass
    return out.getvalue()


class RetitleTests(TestCase):
    databases = "__all__"

    def test_dry_run_reports_the_change_but_writes_nothing(self):
        material = seed("oag-11320", "Audit Bulletin Issue 1, June/July 2017 test")

        output = run()

        self.assertIn("oag-11320", output)
        self.assertIn("Dry run", output)
        material.refresh_from_db()
        self.assertEqual(
            material.data["name"]["en"], "Audit Bulletin Issue 1, June/July 2017 test"
        )

    def test_apply_strips_the_stray_upstream_word(self):
        material = seed("oag-11320", "Audit Bulletin Issue 1, June/July 2017 test")

        run(apply=True)

        material.refresh_from_db()
        self.assertEqual(material.data["name"]["en"], "Audit Bulletin Issue 1, June/July 2017")

    def test_leaves_a_title_someone_else_already_changed(self):
        # The findings predate the run by a week. A caseworker correcting a title
        # by hand in between must not have their wording overwritten.
        material = seed("oag-11320", "Audit Bulletin, Issue 1 (Asar 2074)")

        output = run(apply=True)

        material.refresh_from_db()
        self.assertEqual(material.data["name"]["en"], "Audit Bulletin, Issue 1 (Asar 2074)")
        self.assertIn("skip", output)

    def test_is_idempotent(self):
        material = seed("oag-11320", "Audit Bulletin Issue 1, June/July 2017 test")
        run(apply=True)

        output = run(apply=True)

        material.refresh_from_db()
        self.assertEqual(material.data["name"]["en"], "Audit Bulletin Issue 1, June/July 2017")
        self.assertIn("already correct", output)


class FiscalYearTests(TestCase):
    databases = "__all__"

    def test_fills_in_the_absent_fiscal_year(self):
        material = seed("oag-11101", "Annual Report, 2021")

        run(apply=True)

        material.refresh_from_db()
        # BS 2021 is 1964 CE: without this the report sorts into the 2021 slot.
        self.assertEqual(material.data["jawafdehi:fiscalYearBS"], "2021")

    def test_never_overwrites_a_fiscal_year_that_is_already_set(self):
        material = seed("oag-11101", "Annual Report, 2021", **{"jawafdehi:fiscalYearBS": "2077"})

        output = run(apply=True)

        material.refresh_from_db()
        self.assertEqual(material.data["jawafdehi:fiscalYearBS"], "2077")
        self.assertIn("already set", output)


class RepeatedSuffixTests(TestCase):
    databases = "__all__"

    def test_collapses_a_doubled_province_name(self):
        material = seed(
            "oag-12484",
            "Seventh Annual Report of the Auditor General, 2025 - Koshi Province - Koshi Province",
        )

        run(apply=True)

        material.refresh_from_db()
        self.assertEqual(
            material.data["name"]["en"],
            "Seventh Annual Report of the Auditor General, 2025 - Koshi Province",
        )

    def test_collapses_a_tripled_province_name_to_one(self):
        material = seed(
            "oag-12485",
            "Seventh Annual Report of the Auditor General 2025 - Madhesh Province"
            " - Madhesh Province - Madhesh Province",
        )

        run(apply=True)

        material.refresh_from_db()
        self.assertEqual(
            material.data["name"]["en"],
            "Seventh Annual Report of the Auditor General 2025 - Madhesh Province",
        )

    def test_leaves_a_title_whose_trailing_segments_merely_look_alike(self):
        # Two DIFFERENT trailing segments are not a repeat, and collapsing them
        # would delete a real part of the title.
        material = seed(
            "oag-12486",
            "Seventh Annual Report of the Auditor General, 2025 - Bagmati Province - Volume II",
        )

        run(apply=True)

        material.refresh_from_db()
        self.assertEqual(
            material.data["name"]["en"],
            "Seventh Annual Report of the Auditor General, 2025 - Bagmati Province - Volume II",
        )


class DuplicateSoftDeleteTests(TestCase):
    databases = "__all__"

    def test_deletes_a_duplicate_once_its_survivor_is_proven(self):
        seed("oag-11141", "Special Audit Report on Management of COVID-19, 2021", text="same")
        duplicate = seed(
            "oag-11143", "Special Audit Report on Management of COVID-19, 2021", text="same"
        )

        run(apply=True)

        duplicate.refresh_from_db()
        self.assertTrue(duplicate.is_deleted)

    def test_keeps_the_survivor(self):
        survivor = seed(
            "oag-11141", "Special Audit Report on Management of COVID-19, 2021", text="same"
        )
        seed("oag-11143", "Special Audit Report on Management of COVID-19, 2021", text="same")

        run(apply=True)

        survivor.refresh_from_db()
        self.assertFalse(survivor.is_deleted)
        # It is the English copy of the four, and now says so.
        self.assertEqual(
            survivor.data["name"]["en"],
            "Special Audit Report on Management of COVID-19, 2021 (English Version)",
        )

    def test_refuses_when_the_survivor_is_absent(self):
        # Deleting a "duplicate" whose original is not there removes the document
        # from the archive outright.
        duplicate = seed(
            "oag-11143", "Special Audit Report on Management of COVID-19, 2021", text="same"
        )

        output = run(apply=True)

        duplicate.refresh_from_db()
        self.assertFalse(duplicate.is_deleted)
        self.assertIn("survivor oag-11141 not found", output)

    def test_refuses_when_the_transcripts_differ(self):
        seed("oag-11141", "Special Audit Report on Management of COVID-19, 2021", text="one")
        duplicate = seed(
            "oag-11143", "Special Audit Report on Management of COVID-19, 2021", text="two"
        )

        output = run(apply=True)

        duplicate.refresh_from_db()
        self.assertFalse(duplicate.is_deleted)
        self.assertIn("not a duplicate", output)

    def test_refuses_when_the_survivor_is_itself_deleted(self):
        survivor = seed("oag-11141", "Special Audit Report on Management of COVID-19, 2021")
        survivor.is_deleted = True
        survivor.save()
        duplicate = seed("oag-11143", "Special Audit Report on Management of COVID-19, 2021")

        output = run(apply=True)

        duplicate.refresh_from_db()
        self.assertFalse(duplicate.is_deleted)
        self.assertIn("itself deleted", output)


class MoveSourceTests(TestCase):
    databases = "__all__"

    def seed_aml(self) -> Material:
        return seed(
            "oag-11353",
            "National Risk Assessment Report",
            publisher={
                "@type": "GovernmentOrganization",
                "name": {"en": "Office of the Auditor General"},
            },
        )

    def test_republishes_the_document_off_the_auditor_general_shelves(self):
        self.seed_aml()

        run(apply=True)

        moved = Material.objects.get(iri="https://jawafdehi.org/material/document/oag-11353")
        self.assertFalse(moved.is_deleted)
        self.assertEqual(moved.source, "document")
        # The shelves scope on `source`, so this is what unshelves it.
        self.assertNotEqual(moved.source, OAG)

    def test_corrects_the_hardcoded_publisher(self):
        self.seed_aml()

        run(apply=True)

        moved = Material.objects.get(iri="https://jawafdehi.org/material/document/oag-11353")
        self.assertEqual(moved.data["publisher"]["name"]["en"], "Government of Nepal")

    def test_soft_deletes_the_row_at_the_old_iri(self):
        original = self.seed_aml()

        run(apply=True)

        original.refresh_from_db()
        self.assertTrue(original.is_deleted)

    def test_does_not_move_a_second_time(self):
        # The old row is soft-deleted, not removed, so a second run still finds
        # it and must recognise that the move has already happened rather than
        # inserting a second copy at the new IRI.
        self.seed_aml()
        run(apply=True)

        output = run(apply=True)

        self.assertEqual(
            Material.objects.filter(
                iri="https://jawafdehi.org/material/document/oag-11353"
            ).count(),
            1,
        )
        self.assertIn("already exists", output)

    def test_dry_run_leaves_no_row_behind(self):
        # move_source() INSERTs, so unlike every other action a dry run has
        # something real to unwind.
        self.seed_aml()

        run()

        self.assertFalse(
            Material.objects.filter(
                iri="https://jawafdehi.org/material/document/oag-11353"
            ).exists()
        )


class MissingIdentTests(TestCase):
    databases = "__all__"

    def test_exits_non_zero_when_a_target_is_absent(self):
        # An ident that is not there means the corpus has moved under this table,
        # and the rest of the run is equally suspect.
        with self.assertRaises(SystemExit):
            call_command("fix_oag_corpus", stdout=StringIO(), stderr=StringIO())
