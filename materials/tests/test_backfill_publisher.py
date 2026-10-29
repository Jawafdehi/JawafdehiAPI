"""Tests for the publisher backfill.

The command writes an attribution into documents in a public archive, so what
needs pinning is everything that stops it writing the WRONG one — above all the
SHAPE. An earlier version composed the value from ``SOURCE_LABELS``, whose
publisher is bilingual display text, while a material's is a schema.org node
that consumers read ``publisher.name`` from. Those rows would have looked fixed
and stayed unattributed. Copying a real sibling's value cannot get that wrong,
and these tests hold the command to it.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

TARGET = "press_release"
DONOR = "ciaa_press_release"
#: The shape a scraped row actually stores — a schema.org node, not a
#: bilingual string pair.
CIAA = {
    "@type": "GovernmentOrganization",
    "name": {"ne": "अख्तियार दुरुपयोग अनुसन्धान आयोग"},
}


def seed(ident: str, source: str = TARGET, **extra) -> Material:
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


def backfill(*extra):
    return run("--source", TARGET, "--from", DONOR, *extra)


class ShapeTests(TestCase):
    databases = "__all__"

    def test_the_copied_value_is_the_donors_schema_org_node(self):
        # THE BLOCKER. A consumer reads publisher.name; a bilingual
        # {"en","ne"} pair has no `name`, so the row looks fixed and is still
        # unattributed.
        seed("donor-1", source=DONOR, publisher=CIAA)
        m = seed("a")
        backfill("--write")
        m.refresh_from_db()
        self.assertEqual(m.data["publisher"], CIAA)
        self.assertIn("name", m.data["publisher"])
        self.assertEqual(m.data["publisher"]["@type"], "GovernmentOrganization")

    def test_the_dry_run_shows_the_exact_node(self):
        seed("donor-1", source=DONOR, publisher=CIAA)
        seed("a")
        self.assertIn("GovernmentOrganization", backfill())


class DonorTests(TestCase):
    databases = "__all__"

    def test_a_donor_with_no_publisher_is_refused(self):
        # `ag` has none either. The label-driven version would have stamped one
        # generic name onto 99,750 charge sheets.
        seed("x", source="ag")
        seed("a", source="charge_sheet")
        with pytest.raises(CommandError) as exc:
            run("--source", "charge_sheet", "--from", "ag", "--write")
        self.assertIn("nothing to copy", str(exc.value))

    def test_a_donor_that_disagrees_with_itself_is_refused(self):
        seed("d1", source=DONOR, publisher=CIAA)
        seed("d2", source=DONOR, publisher={"@type": "Organization", "name": {"en": "Other"}})
        seed("a")
        with pytest.raises(CommandError) as exc:
            backfill("--write")
        self.assertIn("different publishers", str(exc.value))

    def test_source_and_donor_must_differ(self):
        with pytest.raises(CommandError):
            run("--source", TARGET, "--from", TARGET, "--write")

    def test_both_arguments_are_required(self):
        with pytest.raises(CommandError):
            run("--source", TARGET, "--write")


class WriteTests(TestCase):
    databases = "__all__"

    def setUp(self):
        seed("donor-1", source=DONOR, publisher=CIAA)

    def test_the_dry_run_writes_nothing(self):
        m = seed("a")
        self.assertIn("DRY RUN", backfill())
        m.refresh_from_db()
        self.assertNotIn("publisher", m.data)

    def test_an_existing_publisher_is_never_overwritten(self):
        theirs = {"@type": "Organization", "name": {"en": "Someone else"}}
        m = seed("a", publisher=theirs)
        backfill("--write")
        m.refresh_from_db()
        self.assertEqual(m.data["publisher"], theirs)

    def test_an_empty_publisher_counts_as_absent(self):
        m = seed("a", publisher={})
        backfill("--write")
        m.refresh_from_db()
        self.assertEqual(m.data["publisher"], CIAA)

    def test_other_fields_survive(self):
        m = seed("a", description={"en": "keep me"})
        backfill("--write")
        m.refresh_from_db()
        self.assertEqual(m.data["description"], {"en": "keep me"})

    def test_a_concurrent_edit_is_not_silently_undone(self):
        # The scan runs outside the transaction. Writing back the stale
        # document we had in hand would erase an edit that landed in between;
        # the command re-reads under a lock and adds one key instead.
        m = seed("a")
        Material.objects.filter(pk=m.iri).update(
            data={**m.data, "description": {"en": "landed meanwhile"}}
        )
        backfill("--write")
        m.refresh_from_db()
        self.assertEqual(m.data["description"], {"en": "landed meanwhile"})
        self.assertEqual(m.data["publisher"], CIAA)

    def test_only_the_named_source_is_touched(self):
        target, other = seed("a"), seed("b", source="charge_sheet")
        backfill("--write")
        target.refresh_from_db()
        other.refresh_from_db()
        self.assertIn("publisher", target.data)
        self.assertNotIn("publisher", other.data)

    def test_soft_deleted_rows_are_skipped(self):
        m = seed("a")
        Material.objects.filter(pk=m.iri).update(is_deleted=True)
        backfill("--write")
        m.refresh_from_db()
        self.assertNotIn("publisher", m.data)

    def test_every_affected_iri_is_printed(self):
        # An operator approving a write to a public archive should see each row
        # it will touch; a truncated list is where an unintended one hides.
        for i in range(14):
            seed(f"r{i}")
        out = backfill()
        for i in range(14):
            self.assertIn(f"/material/press_release/r{i}", out)

    def test_limit_caps_the_write(self):
        for i in range(4):
            seed(f"r{i}")
        backfill("--write", "--limit", "2")
        done = [m for m in Material.objects.filter(source=TARGET) if m.data.get("publisher")]
        self.assertEqual(len(done), 2)

    def test_a_negative_limit_is_refused(self):
        with pytest.raises(CommandError):
            backfill("--write", "--limit", "-1")
