"""Tests for the reviewed-publisher backfill.

The command writes an attribution onto documents in a public archive, so what
needs pinning is not "does it set the field" — it is everything that stops it
setting the WRONG one: the dry run writing nothing, a row that already has an
attribution being left alone, and a source whose publisher varies per record
being refused outright rather than stamped with a plausible single value.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material
from materials.sources import SOURCE_LABELS

SOURCE = "press_release"
EXPECTED = SOURCE_LABELS[SOURCE]["publisher"]


def seed(ident: str, source: str = SOURCE, **extra) -> Material:
    iri = f"https://jawafdehi.org/material/{source}/{ident}"
    data = {
        "@context": MATERIAL_CONTEXT,
        "@id": iri,
        "@type": "CreativeWork",
        "name": {"en": ident},
        **extra,
    }
    return Material.objects.create(
        iri=iri, source=source, ident=ident, material_type="press_release", data=data
    )


def run(*args) -> str:
    out = StringIO()
    call_command("backfill_publisher", *args, stdout=out)
    return out.getvalue()


class DryRunTests(TestCase):
    databases = "__all__"

    def test_the_dry_run_writes_nothing(self):
        m = seed("a")
        out = run("--source", SOURCE)
        m.refresh_from_db()
        self.assertNotIn("publisher", m.data)
        self.assertIn("DRY RUN", out)

    def test_it_names_the_value_it_would_write(self):
        # An operator about to stamp a public archive should see the exact value
        # before running it, not just a count.
        seed("a")
        self.assertIn("अख्तियार दुरुपयोग अनुसन्धान आयोग", run("--source", SOURCE))


class WriteTests(TestCase):
    databases = "__all__"

    def test_write_sets_the_reviewed_publisher(self):
        m = seed("a")
        run("--source", SOURCE, "--write")
        m.refresh_from_db()
        self.assertEqual(m.data["publisher"], EXPECTED)

    def test_an_existing_publisher_is_never_overwritten(self):
        # Whatever it says, it is an attribution somebody established. This
        # command closes a GAP; it does not arbitrate between two claims.
        theirs = {"@type": "Organization", "name": {"en": "Someone else"}}
        m = seed("a", publisher=theirs)
        run("--source", SOURCE, "--write")
        m.refresh_from_db()
        self.assertEqual(m.data["publisher"], theirs)

    def test_an_empty_publisher_counts_as_absent(self):
        # A falsy value is not an attribution; treating it as present would mean
        # the gap never closes.
        m = seed("a", publisher={})
        run("--source", SOURCE, "--write")
        m.refresh_from_db()
        self.assertEqual(m.data["publisher"], EXPECTED)

    def test_other_fields_survive(self):
        m = seed("a", description={"en": "keep me"})
        run("--source", SOURCE, "--write")
        m.refresh_from_db()
        self.assertEqual(m.data["description"], {"en": "keep me"})
        self.assertEqual(m.data["name"], {"en": "a"})

    def test_only_the_named_source_is_touched(self):
        target = seed("a")
        other = seed("b", source="charge_sheet")
        run("--source", SOURCE, "--write")
        target.refresh_from_db()
        other.refresh_from_db()
        self.assertIn("publisher", target.data)
        self.assertNotIn("publisher", other.data)

    def test_soft_deleted_rows_are_skipped(self):
        m = seed("a")
        Material.objects.filter(pk=m.iri).update(is_deleted=True)
        run("--source", SOURCE, "--write")
        m.refresh_from_db()
        self.assertNotIn("publisher", m.data)

    def test_limit_caps_the_write(self):
        for i in range(4):
            seed(f"r{i}")
        run("--source", SOURCE, "--write", "--limit", "2")
        done = [m for m in Material.objects.filter(source=SOURCE) if m.data.get("publisher")]
        self.assertEqual(len(done), 2)


class RefusalTests(TestCase):
    databases = "__all__"

    def test_a_source_whose_publisher_varies_per_record_is_refused(self):
        # `news` is labelled but deliberately carries NO publisher: the outlet
        # differs per article, so one value would be false for most of the
        # shelf. The command must refuse rather than stamp it.
        seed("a", source="news")
        with pytest.raises(CommandError) as exc:
            run("--source", "news", "--write")
        self.assertIn("varies per record", str(exc.value))
        self.assertFalse(
            any(m.data.get("publisher") for m in Material.objects.filter(source="news"))
        )

    def test_the_refusal_set_is_the_labels_not_a_second_list(self):
        # The guard must stay true as labels change, so assert the RULE rather
        # than restating which sources are refused.
        for token, label in SOURCE_LABELS.items():
            if label.get("publisher"):
                continue
            with pytest.raises(CommandError):
                run("--source", token, "--write")

    def test_an_unlabelled_source_is_refused(self):
        with pytest.raises(CommandError) as exc:
            run("--source", "cib", "--write")
        self.assertIn("SOURCE_LABELS", str(exc.value))

    def test_source_is_required(self):
        with pytest.raises(CommandError):
            run("--write")
