"""The write-path source-token allowlist.

Nothing used to constrain a material's ``source``: it is derived from the
``@id`` the caller builds, and the IRI validator only checks a regex, the host
and a length cap. That is how `chargesheet` (a misspelling of `charge_sheet`)
and `court_order_0133` (a case number glued onto a token) became permanent
public shelves.

The behaviour worth pinning is not "it rejects junk" — it is the asymmetry that
makes rejecting junk safe: a NEW token is gated, an EXISTING row never is. Get
that backwards and tightening the list would make live materials unwritable.
"""

from __future__ import annotations

import pytest
from django.test import TestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material
from materials.single_source_ingest import upsert_single_source_material
from materials.sources import KNOWN_SOURCES, SOURCE_LABELS, is_known_source


def doc(source: str, ident: str, **extra) -> dict:
    return {
        "@context": MATERIAL_CONTEXT,
        "@id": f"https://jawafdehi.org/material/{source}/{ident}",
        "@type": "DigitalDocument",
        "name": {"en": ident},
        **extra,
    }


def write(source: str, ident: str, **extra) -> Material:
    return upsert_single_source_material(doc(source, ident, **extra), material_type="document")


class NewTokenTests(TestCase):
    databases = "__all__"

    def test_a_known_source_is_accepted(self):
        self.assertEqual(write("ag", "1").source, "ag")

    def test_an_unknown_source_is_rejected(self):
        with pytest.raises(ValueError) as exc:
            write("ministry_of_vibes", "1")
        self.assertIn("Unknown material source", str(exc.value))
        self.assertFalse(Material.objects.filter(source="ministry_of_vibes").exists())

    def test_the_error_says_what_to_do_about_it(self):
        # A bare "invalid source" sends the reader hunting for a payload bug
        # when the answer is a one-line reviewed addition.
        with pytest.raises(ValueError) as exc:
            write("brand_new_corpus", "1")
        self.assertIn("KNOWN_SOURCES", str(exc.value))
        self.assertIn("materials/sources.py", str(exc.value))

    def test_a_near_miss_is_named_in_the_error(self):
        # Both real-world defects were near misses. Saying so turns a rejection
        # into a fix.
        with pytest.raises(ValueError) as exc:
            write("courtorder", "1")
        self.assertIn("court_order", str(exc.value))

    def test_a_token_with_an_identifier_glued_on_names_the_SPECIFIC_near_miss(self):
        # The `court_order_0133` shape, caught prospectively. It matches both
        # `court` and `court_order`; the hint must name the longer one.
        # Asserting "court_order appears in the message" was not enough — the
        # message repeats the REJECTED token, so that passed while the hint
        # itself said "Did you mean 'court'?".
        with pytest.raises(ValueError) as exc:
            write("court_order_0191", "2081")
        self.assertIn("Did you mean 'court_order'?", str(exc.value))


class ExistingRowTests(TestCase):
    """The asymmetry. Tightening the allowlist must never strand live data."""

    databases = "__all__"

    def test_an_existing_row_on_a_bad_token_can_still_be_updated(self):
        # `court_order_0133` is in the list precisely so its row stays writable
        # until the merge lands. Were it gated, the archive would hold a
        # material nobody could correct.
        write("court_order_0133", "2081")
        again = write("court_order_0133", "2081", name={"en": "edited"})
        self.assertEqual(again.data["name"], {"en": "edited"})

    def test_a_row_whose_token_is_later_removed_from_the_list_stays_writable(self):
        # The real scenario after a merge lands and the token is deleted from
        # KNOWN_SOURCES: the surviving row must not become read-only.
        write("chargesheet", "melamchi")
        with self.settings():
            import materials.sources as src

            original = src.KNOWN_SOURCES
            src.KNOWN_SOURCES = frozenset(original - {"chargesheet"})
            try:
                self.assertFalse(is_known_source("chargesheet"))
                updated = write("chargesheet", "melamchi", name={"en": "still editable"})
                self.assertEqual(updated.data["name"], {"en": "still editable"})
                # Still writable: `chargesheet` has LIVE rows, so it is an
                # existing shelf rather than a new one. It stops being
                # writable when its merge soft-deletes them — see
                # InvisibleSourceTests.
                self.assertEqual(
                    write("chargesheet", "a-brand-new-one").source, "chargesheet"
                )
            finally:
                src.KNOWN_SOURCES = original

    def test_reviving_a_soft_deleted_row_is_not_gated(self):
        # The existence lookup is deliberately not filtered on is_deleted, so a
        # tombstone still counts as "this row exists". A revive is an update.
        m = write("chargesheet", "revive-me")
        Material.objects.filter(pk=m.iri).update(is_deleted=True)
        revived = write("chargesheet", "revive-me")
        self.assertFalse(revived.is_deleted)


class NamespaceTests(TestCase):
    databases = "__all__"

    def test_a_province_namespace_token_is_allowed_without_enumeration(self):
        # Koshi is live and the other six provinces are a mechanical expansion;
        # the editorial decision was made once, for the namespace.
        self.assertEqual(write("province/bagmati", "fy2083-84-1").source, "province/bagmati")

    def test_the_namespace_does_not_open_everything(self):
        with pytest.raises(ValueError):
            write("provinces/koshi", "1")

    def test_the_namespace_admits_exactly_one_segment(self):
        # `province/koshi/fy2083` is the same unreviewed-shelf mistake this
        # gate exists to stop, one level down. A bare startswith accepted it.
        with pytest.raises(ValueError):
            write("province/koshi/fy2083", "1")

    def test_the_namespace_rejects_an_empty_tail(self):
        with pytest.raises(ValueError):
            write("province/", "1")


class ConsistencyTests(TestCase):
    databases = "__all__"

    def test_every_labelled_source_is_writable(self):
        # A token the catalogue names but the write path rejects would be an
        # archive that describes a shelf it cannot fill.
        for token in SOURCE_LABELS:
            self.assertTrue(is_known_source(token), f"{token} is labelled but not writable")

    def test_constants_with_no_public_rows_are_still_writable(self):
        # The trap in seeding a list from the live catalogue: both of these are
        # live constants in the codebase with ZERO public rows, so they are
        # invisible to /api/document-sources/. Omitting one would surface as a
        # CronJob failure in production rather than a test failure here.
        from jawafdehi_shared.entities.ids import JAWAF_SOURCE
        from materials.jsonld import COURT_ORDER_SOURCE, COURT_SOURCE

        for token in (COURT_SOURCE, COURT_ORDER_SOURCE, JAWAF_SOURCE):
            self.assertTrue(is_known_source(token), f"{token} is a live constant")

    def test_the_paths_that_bypass_the_upsert_are_covered_too(self):
        # Two writers create materials WITHOUT the upsert, so the gate never
        # sees them: `move_source` (checks the token itself now) and
        # `seed_dev` (whose fixture tokens must therefore be real).
        from cases.management.commands.seed_dev import MATERIALS

        for source, *_ in MATERIALS:
            self.assertTrue(is_known_source(source), f"seed_dev mints unknown {source!r}")

    def test_the_allowlist_covers_the_scrapers(self):
        # If a scraper's token were dropped, ingestion would fail at 3am on a
        # CronJob rather than in CI. Pin the five that have a sourcing package.
        for token in ("ag", "nkp", "ciaa_press_release", "bolpatra", "ppmo"):
            self.assertIn(token, KNOWN_SOURCES)


class InvisibleSourceTests(TestCase):
    """The risk review could not check from code, closed in code instead."""

    databases = "__all__"

    def test_a_source_with_existing_rows_is_permitted_even_if_unlisted(self):
        # KNOWN_SOURCES was seeded from the PUBLIC catalogue, which cannot see
        # a source whose rows are all private or unlisted. Such a token would
        # have started failing writes in production with nothing in review able
        # to catch it. A source that already has rows is not a new shelf.
        Material.objects.create(
            iri="https://jawafdehi.org/material/invisible_corpus/1",
            source="invisible_corpus",
            ident="1",
            material_type="document",
            data=doc("invisible_corpus", "1"),
        )
        self.assertFalse(is_known_source("invisible_corpus"))
        written = write("invisible_corpus", "2")
        self.assertEqual(written.source, "invisible_corpus")

    def test_a_source_with_no_rows_at_all_is_still_refused(self):
        # The gate still does its job: a genuinely novel token is the one case
        # a human should look at.
        with pytest.raises(ValueError):
            write("never_seen_before", "1")

    def test_a_shelf_whose_rows_are_all_tombstones_is_refused(self):
        # The other direction, and it is what makes the cleanup self-closing:
        # once a merge has moved a shelf away and soft-deleted the originals,
        # the retired token stops being writable on its own — nobody has to
        # remember to delete it from KNOWN_SOURCES.
        m = Material.objects.create(
            iri="https://jawafdehi.org/material/retired_corpus/1",
            source="retired_corpus",
            ident="1",
            material_type="document",
            data=doc("retired_corpus", "1"),
        )
        Material.objects.filter(pk=m.iri).update(is_deleted=True)
        with pytest.raises(ValueError):
            write("retired_corpus", "2")
