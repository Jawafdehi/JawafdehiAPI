"""The row lock's precondition, asserted on the sqlite gate.

SQLite drops ``FOR UPDATE`` silently, so the lock itself cannot be observed here
(see ``test_patch_concurrency_postgres``). But the failure mode that actually
bites is not "the lock is subtly wrong" — it is "the lock was taken on the wrong
connection", and THAT is engine-independent and checkable right here.

Django refuses ``select_for_update()`` when the queryset's connection is in
autocommit::

    if self.query.select_for_update and features.has_select_for_update:
        if self.connection.get_autocommit() and features.supports_transactions:
            raise TransactionManagementError(...)

On Postgres that raises and the request 500s. On SQLite ``has_select_for_update``
is False, so the whole branch — including the autocommit check — is skipped and
the mistake is invisible. That is exactly how ``materials/conversion.py`` shipped
a bare ``atomic()`` against an ``ngm``-routed ``select_for_update`` to main and
nobody noticed.

``connections[alias].in_atomic_block`` is the same condition, readable on any
backend. These tests pin it so a future edit that changes the alias — or wraps
the write in the wrong ``atomic()`` — fails in CI instead of in production.
"""

from __future__ import annotations

from unittest.mock import patch as mock_patch

import jsonpatch
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connections, transaction
from rest_framework.test import APIClient

from materials.models import Material, Visibility

User = get_user_model()
# transaction=True is load-bearing: the default django_db fixture wraps each
# test in an atomic block on EVERY declared alias, so `in_atomic_block` would be
# True no matter what the view did and every assertion below would be vacuous.
pytestmark = pytest.mark.django_db(databases=["default", "ngm"], transaction=True)

#: The alias Material is routed to. If this ever changes, every ``using=`` in the
#: PATCH/conversion write paths has to change with it — which is the point.
MATERIAL_DB = "ngm"


def _store(ident="lockcheck"):
    iri = f"https://jawafdehi.org/material/ag/{ident}"
    mat = Material(
        iri=iri,
        material_type="charge_sheet",
        source="ag",
        ident=ident,
        data={"@id": iri, "@type": "DigitalDocument", "name": {"ne": "अभियोगपत्र"}},
        visibility=Visibility.LISTED,
    )
    mat.save()
    return mat


def _client(username="cw-lock"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
    c = APIClient()
    c.force_authenticate(u)
    return c


def test_material_is_routed_to_the_alias_the_write_paths_name():
    from django.db import router

    assert router.db_for_write(Material) == MATERIAL_DB


class TestThePatchHoldsATransactionOnTheRightConnection:
    def test_the_ngm_connection_is_in_a_transaction_while_the_patch_applies(self):
        # Observed from INSIDE the locked block: apply_patch runs between the
        # select_for_update and the save, so whatever it sees is the state
        # Postgres would have evaluated its autocommit check against.
        seen = {}
        real = jsonpatch.apply_patch

        def spy(*args, **kwargs):
            seen["ngm"] = connections[MATERIAL_DB].in_atomic_block
            seen["default"] = connections["default"].in_atomic_block
            return real(*args, **kwargs)

        mat = _store("lock-001")
        with mock_patch.object(jsonpatch, "apply_patch", side_effect=spy):
            resp = _client().patch(
                f"/api/materials/?iri={mat.iri}",
                {"patch_ops": [{"op": "add", "path": "/x", "value": 1}]},
                format="json",
            )
        assert resp.status_code == 200
        assert seen["ngm"] is True, (
            "the PATCH took its row lock on a connection in autocommit — on "
            "Postgres this raises TransactionManagementError and 500s"
        )


class TestTheAliasMismatchIsWhatBreaks:
    """Engine-independent demonstration of the conversion.py bug and its fix."""

    def test_a_bare_atomic_leaves_the_material_connection_in_autocommit(self):
        with transaction.atomic():  # the bug: opens `default`
            assert connections["default"].in_atomic_block is True
            assert connections[MATERIAL_DB].in_atomic_block is False

    def test_naming_the_alias_opens_the_material_connection(self):
        with transaction.atomic(using=MATERIAL_DB):  # the fix
            assert connections[MATERIAL_DB].in_atomic_block is True


class TestTheConversionWriterTakesItsLockCorrectly:
    def test_conversion_opens_a_transaction_on_the_material_alias(self):
        # materials/conversion.py is the other writer this PATCH serializes
        # against; it had the mismatched-alias bug. Pin its fix the same way.
        import materials.conversion as conversion
        import inspect

        src = inspect.getsource(conversion.apply_convert_result)
        assert 'transaction.atomic(using="ngm")' in src, (
            "conversion.py must open its transaction on the alias its "
            "select_for_update is routed to"
        )
        assert 'Material.objects.using("ngm")' in src
