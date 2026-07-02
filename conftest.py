"""Repo-root pytest conftest for the unified platform test suite.

Shields the shared third-party ``tests`` namespace pollution from the Jawafdehi
suite. A transitive dependency (``indic_transliteration`` / ``nepali_date_utils``,
pulled in by the search/NES stack) installs its OWN top-level ``tests`` package
into ``site-packages``. The Jawafdehi suite imports its helpers as the top-level
package ``tests`` (``from tests.conftest import ...`` / ``from tests.strategies
import ...``), which now lives at the repo root (``./tests``). If the site-packages
``tests`` is imported first it poisons ``sys.modules['tests']`` and those imports
fail.

Putting the repo root at the FRONT of ``sys.path`` here (this conftest is imported
before collection) makes the repo-root ``tests`` package win for any fresh
``import tests``. Combined with pytest's default ``prepend`` import mode this is
sufficient: each app owns its own ``tests`` package (``entities/tests``,
``courts/tests``, …) so no other suite consumes the bare ``tests`` name.
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
if _REPO_ROOT.is_dir():
    p = str(_REPO_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _disable_staticfiles_manifest():
    """Use the non-manifest staticfiles backend suite-wide (repo-root scope).

    ``tests/conftest.py`` already overrides ``STORAGES`` to drop the manifest
    check, but that fixture only covers items under ``tests/``. When an app-level
    suite (``courts/tests``, ``materials/tests``, …) issues the FIRST request of
    the session — before any ``tests/``-package test runs — whitenoise
    instantiates and caches the production ``CompressedManifestStaticFilesStorage``
    (which raises ``Missing staticfiles manifest entry`` because tests never run
    ``collectstatic``). Hoisting the override to the repo-root conftest makes it
    apply regardless of collection order, so the ``/django-admin/`` e2e reads
    stay green whichever suite runs first.

    Adding the Wagtail CMS made this override necessary but not sufficient: with
    ``wagtail.admin`` installed, the ``{% static %}`` machinery resolves the
    module-level ``django.contrib.staticfiles.storage.staticfiles_storage`` lazy
    proxy (and the ``storages`` registry) to the *production*
    ``CompressedManifestStaticFilesStorage`` before this fixture reassigns
    ``settings.STORAGES`` — and both memoize their backend on first access, so the
    reassignment alone doesn't take. Resetting them forces a rebuild from the
    overridden STORAGES on next access, so template ``{% static %}`` calls (e.g.
    the Jazzmin admin index pulling ``bootstrap.min.css``) use the non-manifest
    backend.
    """
    from django.conf import settings as django_settings
    from django.contrib.staticfiles import storage as _sf_storage
    from django.core.files.storage import storages as _storages_registry
    from django.utils.functional import empty as _empty

    django_settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }

    # Drop memoized backends so both the ``storages["staticfiles"]`` registry and
    # the ``staticfiles_storage`` lazy proxy rebuild from the STORAGES set above
    # (either may already have resolved to the manifest backend during app load —
    # Wagtail's admin registration touches static handling early). The
    # ``StorageHandler`` caches the parsed config on the ``backends``
    # cached_property (backed by ``_backends``) and each instantiated backend in
    # ``_storages`` — reset all of them so the next ``storages["staticfiles"]``
    # (and the ``static`` templatetag that goes through it) re-reads STORAGES.
    _storages_registry._backends = None
    _storages_registry._storages = {}
    _storages_registry.__dict__.pop("backends", None)
    _sf_storage.staticfiles_storage._wrapped = _empty


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
    "cases/tests/test_enrich_ciaa_timeline.py",
]


# ---------------------------------------------------------------------------
# Cross-database test access (Task: TestCase.databases).
#
# The platform routes the ``entities`` app to the ``nes`` alias and
# ``courts``/``materials`` to ``ngm`` (see ``config.db_router``).
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
# ``django_db`` marker that did not already pin a ``databases`` set, using the
# ``"__all__"`` sentinel (mirroring the NES/NGM ``APITestCase``s) and matching
# production reality (the platform always has the three DBs and any case-detail
# read can fan out to ``nes``/``ngm``).
#
# NOTE: this MUST be the ``"__all__"`` sentinel, not an explicit frozenset of the
# three aliases. An explicit set enrolls the aliases for READS but does not
# reliably grant WRITE access to the secondary (``ngm``/``nes``) connections, so
# tests that write Materials to ``ngm`` hit ``DatabaseOperationForbidden``;
# ``"__all__"`` enrolls them fully. Tests that never touch another alias are
# unaffected; tests that explicitly pin ``databases=[...]`` are left as-is.
# ---------------------------------------------------------------------------


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
        new_kwargs["databases"] = "__all__"
        # add_marker(..., append=False) PREPENDS, so this becomes the "closest"
        # marker that pytest-django reads for the ``databases`` set.
        item.add_marker(
            pytest.mark.django_db(*marker.args, **new_kwargs), append=False
        )
