"""Concurrency guarantees that ONLY Postgres can demonstrate.

SQLite sets ``has_select_for_update = False``, so Django silently drops the
``FOR UPDATE`` clause (``django/db/models/sql/compiler.py`` — the branch is
skipped entirely, no warning). Under the sqlite gate the material PATCH
therefore runs with no row lock at all, and these properties are unprovable:
concurrent writers there fail with sqlite's file-level ``database is locked``
instead of queueing.

So this module is skipped unless the ``ngm`` alias is actually Postgres. Run it
against a throwaway cluster:

    NGM_DATABASE_URL=postgres://... uv run pytest \\
        materials/tests/test_patch_concurrency_postgres.py -q
"""

from __future__ import annotations

import threading

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connections, transaction
from django.db.transaction import TransactionManagementError
from rest_framework.test import APIClient

from materials.models import Material, Visibility

User = get_user_model()

pytestmark = [
    pytest.mark.django_db(databases=["default", "ngm"], transaction=True),
    pytest.mark.skipif(
        "postgresql" not in connections["ngm"].settings_dict.get("ENGINE", ""),
        reason="row-lock behaviour is only observable on Postgres",
    ),
]

IRI = "https://jawafdehi.org/material/ag/pg-concurrency"


def _store():
    mat = Material(
        iri=IRI,
        material_type="charge_sheet",
        source="ag",
        ident="pg-concurrency",
        data={"@id": IRI, "@type": "DigitalDocument", "name": {"ne": "अभियोगपत्र"}},
        visibility=Visibility.LISTED,
    )
    mat.save()
    return mat


def _client(username):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
    c = APIClient()
    c.force_authenticate(u)
    return c


class TestTheRowLockPreventsLostUpdates:
    def test_concurrent_patches_to_different_keys_all_survive(self):
        """The property the whole endpoint exists for.

        Under the old GET→merge→PUT shape each writer would send a document built
        from its own stale snapshot and the last PUT would win, silently dropping
        the others. Here every writer adds a DISTINCT key concurrently; if the
        read-modify-write were not serialized under ``select_for_update``, some
        keys would be missing from the final document — with no error anywhere.
        """
        _store()
        writers = 8
        clients = [_client(f"cw-race-{i}") for i in range(writers)]
        start = threading.Barrier(writers)
        results: dict[int, int] = {}
        errors: list[BaseException] = []

        def patch(i: int):
            try:
                start.wait(timeout=30)
                resp = clients[i].patch(
                    f"/api/materials/?iri={IRI}",
                    {"patch_ops": [{"op": "add", "path": f"/k{i}", "value": i}]},
                    format="json",
                )
                results[i] = resp.status_code
            except BaseException as exc:  # noqa: BLE001 - surfaced in the assert
                errors.append(exc)
            finally:
                # Each thread gets its own connections; close them or the test
                # DB teardown blocks on the open sessions.
                connections.close_all()

        threads = [threading.Thread(target=patch, args=(i,)) for i in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, errors
        assert set(results.values()) == {200}, results

        row = Material.objects.using("ngm").get(pk=IRI)
        missing = [f"k{i}" for i in range(writers) if f"k{i}" not in row.data]
        assert not missing, f"lost updates — these writes vanished: {missing}"
        # The untouched original content must also survive every merge.
        assert row.data["name"] == {"ne": "अभियोगपत्र"}

    def test_a_second_writer_waits_rather_than_failing(self):
        """No writer is rejected under contention.

        On sqlite the same scenario produces ``OperationalError('database is
        locked')`` 500s (measured: 6 of 40). On Postgres the second writer must
        BLOCK on the row lock and then succeed.
        """
        _store()
        held = threading.Event()
        release = threading.Event()
        second_status: list[int] = []

        def hold_the_lock():
            try:
                with transaction.atomic(using="ngm"):
                    Material.objects.using("ngm").select_for_update().filter(
                        pk=IRI
                    ).first()
                    held.set()
                    release.wait(timeout=30)
            finally:
                connections.close_all()

        def second_writer():
            try:
                held.wait(timeout=30)
                client = _client("cw-blocked")
                second_status.append(
                    client.patch(
                        f"/api/materials/?iri={IRI}",
                        {"patch_ops": [{"op": "add", "path": "/late", "value": 1}]},
                        format="json",
                    ).status_code
                )
            finally:
                connections.close_all()

        holder = threading.Thread(target=hold_the_lock)
        writer = threading.Thread(target=second_writer)
        holder.start()
        writer.start()
        assert held.wait(timeout=30), "lock was never taken"
        # The writer must still be blocked while the lock is held.
        writer.join(timeout=2)
        assert writer.is_alive(), "second writer was NOT blocked by the row lock"
        release.set()
        holder.join(timeout=30)
        writer.join(timeout=30)

        assert second_status == [200], second_status
        assert Material.objects.using("ngm").get(pk=IRI).data["late"] == 1


class TestTheAliasMustMatchTheLock:
    """Why ``materials/conversion.py`` needed ``using="ngm"``.

    ``Material`` routes to ``ngm``. A bare ``atomic()`` opens a transaction on
    ``default`` and leaves ``ngm`` in autocommit, so ``select_for_update()``
    raises — a crash that the sqlite gate cannot see, because SQLite skips the
    check along with the clause.
    """

    def test_a_mismatched_alias_raises(self):
        _store()
        with pytest.raises(TransactionManagementError):
            with transaction.atomic():  # default alias — the bug
                list(
                    Material.objects.using("ngm").select_for_update().filter(pk=IRI)
                )

    def test_the_matching_alias_works(self):
        _store()
        with transaction.atomic(using="ngm"):  # the fix
            rows = list(
                Material.objects.using("ngm").select_for_update().filter(pk=IRI)
            )
        assert len(rows) == 1
