"""Router-isolation guard (F13).

The repo-root ``conftest.py`` auto-enrolls ``databases="__all__"`` on every
``django_db`` marker that did not pin its own set — a convenience so request-path
tests that fan out across ``default``/``nes``/``ngm`` don't each have to declare
it. But that convenience DEFEATS the DB router as a test signal: with all three
aliases always enrolled, a cross-alias read/write that would raise
``DatabaseOperationForbidden`` in a faithfully-isolated setup silently passes.

This module pins the isolation explicitly. Each test PINS its own ``databases``
set (so the conftest auto-enrollment is skipped — it only fills in a set the test
did NOT provide) and asserts:

* the router maps each app to the expected alias (unit-level, no DB),
* ``allow_relation`` rejects a cross-DB object pair (the "no cross-service FK"
  invariant), and
* querying an UNDECLARED alias raises ``DatabaseOperationForbidden`` — i.e. the
  router boundary is real, not papered over by ``__all__``.
"""

from __future__ import annotations

import pytest
from django.test.testcases import DatabaseOperationForbidden
from django.test.utils import override_settings

from config.db_router import ServiceDatabaseRouter, _db_for_label


def test_router_maps_apps_to_expected_aliases():
    # Pure unit check — no DB access, so no django_db marker.
    assert _db_for_label("entities") == "nes"
    assert _db_for_label("courts") == "ngm"
    assert _db_for_label("materials") == "ngm"
    assert _db_for_label("cases") == "default"
    assert _db_for_label("review") == "default"
    assert _db_for_label("jobs") == "default"


def test_allow_relation_rejects_cross_db_pair():
    from cases.models import Case
    from entities.models import StoredEntity

    router = ServiceDatabaseRouter()
    case = Case()  # default alias
    entity = StoredEntity()  # nes alias
    # Same-DB pair → True (allowed); cross-DB pair → False (forbidden).
    assert router.allow_relation(case, case) is True
    assert router.allow_relation(entity, entity) is True
    assert router.allow_relation(case, entity) is False
    assert router.allow_relation(entity, case) is False


# IMPORTANT: this test PINS databases to ONLY the default alias, so the conftest
# does NOT widen it to "__all__". A query to the (undeclared) nes alias must then
# raise DatabaseOperationForbidden — proving the router boundary is enforced.
@pytest.mark.django_db(databases=["default"])
def test_query_to_undeclared_alias_is_forbidden():
    from entities.models import StoredEntity

    with pytest.raises(DatabaseOperationForbidden):
        # StoredEntity routes to ``nes``, which this test did not enroll — the
        # test-DB isolation blocker must forbid the query. This ONLY fires because
        # the test pinned databases=["default"] (so conftest didn't widen it to
        # "__all__"); it proves the router boundary is real.
        list(StoredEntity.objects.all())


@pytest.mark.django_db(databases=["default"])
def test_declared_alias_query_is_allowed():
    from cases.models import Case

    # Case routes to ``default``, which IS enrolled — this must NOT raise.
    assert Case.objects.count() == 0


@override_settings(REPLICA_ALIASES={})
def test_db_for_read_without_replica_returns_primary():
    router = ServiceDatabaseRouter()
    from entities.models import StoredEntity

    # No replica configured → reads go to the primary alias.
    assert router.db_for_read(StoredEntity) == "nes"
    from cases.models import Case

    assert router.db_for_write(Case) == "default"
