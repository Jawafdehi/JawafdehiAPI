# SPDX-License-Identifier: Hippocratic-3.0
"""No first-party app may share a top-level import name with an installed dependency.

This exists because of a real near-miss. An app package named ``events`` looked
completely fine from a source checkout and passed the entire unit suite — the
repo root precedes site-packages on ``sys.path``, so it shadowed the identically
named ``Events`` distribution that ``opensearch-py`` pulls in transitively. In
the Docker image the app is installed as a *wheel into site-packages*, where the
two land in the same directory and collide. The only thing that caught it was
the e2e job failing to run migrations.

So the unit suite could not have caught it by construction, and adding more unit
tests would not have helped. This check is the fix: it compares app names against
what the installed distributions actually claim, which is the same question the
wheel install asks.

Nothing here needs the collision to be *reachable* to be worth blocking. A
first-party package sharing a name with a dependency is ambiguous even when it
happens to resolve correctly today, because a transitive dependency can start
shipping that name in any future lockfile bump.
"""

from importlib.metadata import packages_distributions

from django.conf import settings

#: The distribution this project itself installs as. Packages it owns are not
#: collisions with themselves.
OWN_DISTRIBUTION = "jawafdehi"


def _first_party_apps():
    """Apps whose package directory lives in this repo.

    Defined by what is on disk rather than by a name prefix: a third-party app
    that happens to share a name with its own distribution (``auditlog`` ships
    from ``django-auditlog``, ``corsheaders`` from ``django-cors-headers``) is
    not a collision — it is the same package, correctly resolved. Only a
    directory we ship can shadow something.
    """
    return [
        app
        for app in settings.INSTALLED_APPS
        if "." not in app and (settings.BASE_DIR / app / "__init__.py").exists()
    ]


def test_no_app_shadows_an_installed_distribution():
    owners = packages_distributions()
    collisions = {}
    for app in _first_party_apps():
        dists = [d for d in owners.get(app, []) if d.lower() != OWN_DISTRIBUTION]
        if dists:
            collisions[app] = dists

    assert not collisions, (
        f"These INSTALLED_APPS share a top-level import name with an installed "
        f"dependency: {collisions}. That works from a source checkout (the repo "
        f"root shadows site-packages) but breaks the installed wheel, where both "
        f"land in site-packages. Rename the app."
    )


def test_the_check_can_actually_detect_a_collision():
    """Guard the guard.

    ``packages_distributions()`` returning an empty-ish map (wrong interpreter,
    no dist-info, a future stdlib change) would make the test above pass
    vacuously forever. ``events`` is a known-colliding name from a real transitive
    dependency, so if the mechanism works at all it must see this one.
    """
    owners = packages_distributions()
    assert owners.get("events"), (
        "Expected the `Events` distribution (transitive via opensearch-py) to "
        "claim the top-level name `events`. If this dependency is genuinely gone, "
        "swap in another known top-level name rather than deleting the check — "
        "otherwise the collision test above silently stops testing anything."
    )
