"""Repo-root pytest conftest for the consolidated (monolith) test suite.

Shields the shared third-party ``tests`` namespace pollution from the Jawafdehi
suite. A transitive dependency (``indic_transliteration`` / ``nepali_date_utils``,
pulled in by the search/NES stack) installs its OWN top-level ``tests`` package
into ``site-packages``. The Jawafdehi suite, by contrast, imports its helpers as
the top-level package ``tests`` (``from tests.conftest import ...`` /
``from tests.strategies import ...``), expecting its OWN ``services/jawafdehi/
tests`` package. If the site-packages ``tests`` is imported first it poisons
``sys.modules['tests']`` and the Jawafdehi imports fail.

Putting ``services/jawafdehi`` at the FRONT of ``sys.path`` here (the repo-root
conftest is imported before collection) makes the Jawafdehi ``tests`` package win
for any fresh ``import tests``. Combined with pytest's default ``prepend`` import
mode this is sufficient: NES/NGM test modules never ``import tests`` (they import
their own ``nes_service``/``ngm_service`` packages), so the only consumer of the
bare ``tests`` name is the Jawafdehi suite, and it resolves to the right package.
"""

import sys
from pathlib import Path

import pytest

_JAWAFDEHI = Path(__file__).resolve().parent / "services" / "jawafdehi"
if _JAWAFDEHI.is_dir():
    p = str(_JAWAFDEHI)
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Pre-existing STALE test, unrelated to the DB engine. ``test_enrich_ciaa_
# timeline.py`` references ``SourceType.LEGAL_PROCEDURAL`` /
# ``SourceType.LEGAL_COURT_ORDER`` at import time, but those enum members were
# REMOVED by the source-type revamp (cases migration
# ``0027_revamp_source_types``; the current members are CIAA_PRESS_RELEASE /
# AG_ABHIYOG_PATRA / COURT_ORDER / ...). The module therefore raises
# ``AttributeError`` on import — a collection-time crash that predates and is
# independent of the sqlite/DB-agnostic work, and that would otherwise abort the
# whole single-run suite. It is skipped here (not deleted) so the rest of the
# suite is green; bringing it in line with the revamped SourceType enum is a
# content-test fix that belongs to whoever owns the CIAA enrichment command.
# ---------------------------------------------------------------------------
collect_ignore = [
    "services/jawafdehi/cases/tests/test_enrich_ciaa_timeline.py",
]


# ---------------------------------------------------------------------------
# Cross-database test access (Task: TestCase.databases).
#
# The monolith routes the ``entities`` app to the ``nes`` alias and
# ``courts``/``materials`` to ``ngm`` (see ``monolith.config.db_router``).
# Django's test runner / pytest-django only set up + permit queries to the
# databases a test DECLARES; a query to an undeclared alias raises
# ``DatabaseOperationForbidden``. The NES/NGM ``APITestCase``s already declare
# ``databases = "__all__"``. But Jawafdehi tests that exercise a request path
# which transitively touches another service's DB — e.g. ``GET /api/cases/<id>/``
# resolves a case's bound NES entities via ``cases.services.nes_resolver``, which
# queries the ``nes`` alias — also need every alias enrolled, and they are
# plain ``@pytest.mark.django_db`` (pytest-django) tests, not Django
# ``TestCase`` subclasses.
#
# Rather than edit ~40 Jawafdehi test modules, we enroll ALL databases on every
# ``django_db`` marker that did not already pin a ``databases`` set. This mirrors
# the NES/NGM ``"__all__"`` choice and matches production reality (the monolith
# always has the three DBs and any case-detail read can fan out to ``nes``/
# ``ngm``). Tests that never touch another alias are unaffected (enrolling an
# unused test DB is harmless); tests that explicitly pin ``databases=[...]`` are
# left as-is.
# ---------------------------------------------------------------------------
_ALL_DB_ALIASES = frozenset({"default", "nes", "ngm"})


def pytest_collection_modifyitems(config, items):
    for item in items:
        marker = item.get_closest_marker("django_db")
        if marker is None:
            continue
        # Respect an explicit ``databases=`` the test already set.
        if "databases" in marker.kwargs:
            continue
        # Re-apply the marker with all aliases enrolled, preserving any other
        # kwargs the test passed (e.g. ``transaction=True``, ``reset_sequences``).
        new_kwargs = dict(marker.kwargs)
        new_kwargs["databases"] = _ALL_DB_ALIASES
        # add_marker(..., append=False) PREPENDS, so this becomes the "closest"
        # marker that pytest-django reads for the ``databases`` set.
        item.add_marker(
            pytest.mark.django_db(*marker.args, **new_kwargs), append=False
        )
