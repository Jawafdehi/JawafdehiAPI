"""Retired material IRIs: the redirect table that makes a rename reversible.

A material's ``@id`` is both its primary key and its public URL, so correcting
a source token retires an address. Three things have to hold or the table is
worse than not having it:

  * a LIVE material always wins — history may never shadow a row that exists;
  * a redirect is an EXISTENCE CLAIM, so the target's visibility is re-checked
    with the requester's own permissions;
  * a write arriving at a retired ``@id`` is rewritten, not revived — otherwise
    the next scrape republishes the tombstone and the archive serves both
    addresses.
"""

from __future__ import annotations

from django.test import TestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import MaterialIdentHistory, Material, Policy, Visibility
from materials.single_source_ingest import upsert_single_source_material

OLD = "https://jawafdehi.org/material/court_order_0133/2081"
NEW = "https://jawafdehi.org/material/court_order/0133"


def doc(iri: str, **extra) -> dict:
    return {
        "@context": MATERIAL_CONTEXT,
        "@id": iri,
        "@type": "DigitalDocument",
        "name": {"en": "judgement"},
        **extra,
    }


def seed(iri: str, *, visibility=Visibility.LISTED, policy=Policy.PUBLIC) -> Material:
    source, ident = iri.split("/material/")[1].split("/", 1)
    return Material.objects.create(
        iri=iri,
        source=source,
        ident=ident,
        material_type="court_order",
        data=doc(iri),
        visibility=visibility,
        visibility_policy=policy,
    )


class RecordTests(TestCase):
    databases = "__all__"

    def test_record_maps_the_old_address_to_the_material(self):
        m = seed(NEW)
        MaterialIdentHistory.record(m, OLD, reason="token malformed")
        self.assertEqual(MaterialIdentHistory.resolve(OLD), m)

    def test_recording_the_same_address_twice_repoints_rather_than_collides(self):
        first = seed(NEW)
        second = seed("https://jawafdehi.org/material/court_order/9999")
        MaterialIdentHistory.record(first, OLD)
        MaterialIdentHistory.record(second, OLD)
        self.assertEqual(MaterialIdentHistory.resolve(OLD), second)
        self.assertEqual(MaterialIdentHistory.objects.filter(retired_iri=OLD).count(), 1)

    def test_a_no_op_rename_records_nothing(self):
        m = seed(NEW)
        MaterialIdentHistory.record(m, NEW)
        MaterialIdentHistory.record(m, "")
        self.assertEqual(MaterialIdentHistory.objects.count(), 0)

    def test_a_newly_live_address_drops_any_history_row_shadowing_it(self):
        # The "live wins" invariant, enforced at write time as well as read
        # time: if NEW was previously retired and is now claimed by a real
        # material, the stale redirect must not survive to shadow it.
        old_owner = seed("https://jawafdehi.org/material/court_order/1111")
        MaterialIdentHistory.record(old_owner, NEW)
        self.assertIsNotNone(MaterialIdentHistory.resolve(NEW))
        revived = seed(NEW)
        MaterialIdentHistory.record(revived, OLD)
        self.assertIsNone(MaterialIdentHistory.resolve(NEW))

    def test_resolve_ignores_a_soft_deleted_target(self):
        # Redirecting to a deleted material sends the caller somewhere that
        # also fails, which is worse than a 404 here.
        m = seed(NEW)
        MaterialIdentHistory.record(m, OLD)
        Material.objects.filter(pk=NEW).update(is_deleted=True)
        self.assertIsNone(MaterialIdentHistory.resolve(OLD))


class ReadPathTests(TestCase):
    databases = "__all__"

    def url(self, iri: str) -> str:
        source, ident = iri.split("/material/")[1].split("/", 1)
        return f"/api/materials/{source}/{ident}"

    def test_a_retired_address_301s_to_the_current_one(self):
        m = seed(NEW)
        MaterialIdentHistory.record(m, OLD)
        resp = self.client.get(self.url(OLD))
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], "/api/materials/court_order/0133")

    def test_the_query_string_survives_the_redirect(self):
        # A retired address is usually reached from a citation or an index;
        # dropping its parameters would change the request, not just move it.
        m = seed(NEW)
        MaterialIdentHistory.record(m, OLD)
        resp = self.client.get(self.url(OLD), {"lang": "ne"})
        self.assertEqual(resp.status_code, 301)
        self.assertIn("lang=ne", resp["Location"])

    def test_a_live_material_is_never_redirected(self):
        # Both addresses exist: the live one must serve its own document rather
        # than 301 to the other.
        live = seed(OLD)
        other = seed(NEW)
        MaterialIdentHistory.objects.create(retired_iri=OLD, material=other)
        resp = self.client.get(self.url(OLD))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["@id"], live.iri)

    def test_an_unknown_address_still_404s(self):
        resp = self.client.get("/api/materials/court_order/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_a_private_target_does_not_301_for_the_public(self):
        # A redirect is an existence claim. 301-ing here would tell anyone who
        # guessed a former address that a non-public document exists — exactly
        # the leak the visibility gate prevents on the direct URL.
        m = seed(NEW, visibility=Visibility.PRIVATE, policy=Policy.PRIVATE)
        MaterialIdentHistory.record(m, OLD)
        resp = self.client.get(self.url(OLD))
        self.assertEqual(resp.status_code, 404)

    def test_the_redirect_varies_on_authorization(self):
        # One URL, two outcomes (404 for anon, 301 for a caseworker), so a
        # shared cache must not serve one to the other.
        m = seed(NEW)
        MaterialIdentHistory.record(m, OLD)
        resp = self.client.get(self.url(OLD))
        self.assertIn("Authorization", resp.get("Vary", ""))


class WritePathTests(TestCase):
    databases = "__all__"

    def test_a_write_to_a_retired_address_lands_on_the_current_one(self):
        # Without this the next scrape revives the tombstone and the archive
        # serves BOTH addresses — worse than never having renamed it.
        m = seed(NEW)
        MaterialIdentHistory.record(m, OLD)
        written = upsert_single_source_material(
            doc(OLD, name={"en": "re-ingested"}), material_type="court_order"
        )
        self.assertEqual(written.iri, NEW)
        self.assertEqual(written.data["@id"], NEW)
        self.assertFalse(Material.objects.filter(pk=OLD).exists())

    def test_a_write_to_a_live_address_is_untouched(self):
        live = seed(OLD)
        other = seed(NEW)
        MaterialIdentHistory.objects.create(retired_iri=OLD, material=other)
        written = upsert_single_source_material(
            doc(OLD, name={"en": "edited"}), material_type="court_order"
        )
        self.assertEqual(written.iri, live.iri)

    def test_a_write_to_an_unknown_address_is_untouched(self):
        fresh = "https://jawafdehi.org/material/court_order/brand-new"
        written = upsert_single_source_material(doc(fresh), material_type="court_order")
        self.assertEqual(written.iri, fresh)
