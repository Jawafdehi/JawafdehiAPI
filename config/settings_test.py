"""Test settings for the consolidated Jawafdehi platform .

Imports the production settings unchanged, then overrides ONLY the database
configuration so the WHOLE test suite runs on sqlite with zero external
services. The base settings already fall back to sqlite when the DB-URL env
vars are unset, but that fallback points all three aliases at the SAME
``db.sqlite3`` file — which makes the DB router's per-alias isolation a fiction
under tests (a write routed to ``nes`` would be visible on ``default``).

Here each of the three router aliases (``default`` / ``nes`` / ``ngm``) gets its
OWN sqlite database with a DISTINCT ``TEST["NAME"]``, so Django's test runner
builds three separate test databases and the router's read/write/allow_migrate
behaviour is exercised the same way it is against three separate Postgres DBs in
production. App code stays engine-agnostic: nothing here changes models, queries
or migrations — only which engine/name each alias points at.

Wire-up: pytest uses this via ``DJANGO_SETTINGS_MODULE`` in the root
``pyproject.toml`` having already been pointed here is NOT required — pytest's
``[tool.pytest.ini_options] DJANGO_SETTINGS_MODULE`` may name either the base or
this module. To run ``manage.py test`` on sqlite explicitly:

    DJANGO_SETTINGS_MODULE=config.settings_test python manage.py test

Each NAME is distinct because Django treats two sqlite connections that share a
``TEST["NAME"]`` (including the default ``:memory:``) as the SAME test database
(a mirror), which would collapse the three aliases back into one and hide
router/cross-DB bugs.
"""

import os

# Importing the base settings runs its module-level config (INSTALLED_APPS,
# REST_FRAMEWORK, the fail-closed guards under TESTING, etc.). Ensure the
# TESTING flag is set before that import so the prod guards relax.
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("SECRET_KEY", "test-key-for-tests-only")
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")

from config.settings import *  # noqa: F401,F403,E402

# Three independent sqlite databases — one per router alias. Distinct TEST NAMEs
# keep Django from mirroring them into a single shared test DB.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"NAME": "file:test_default?mode=memory&cache=shared"},
    },
    "nes": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"NAME": "file:test_nes?mode=memory&cache=shared"},
    },
    "ngm": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"NAME": "file:test_ngm?mode=memory&cache=shared"},
    },
}

# No read replicas under tests: this DATABASES override drops any "_ro" alias the
# base settings may have built from a stray *_READ_URL env, so clear the mapping
# too or the router could route a read to an alias that no longer exists.
REPLICA_ALIASES = {}
