# SPDX-License-Identifier: Hippocratic-3.0
"""Adding a Django app means editing three lists. These tests keep them in sync.

An app has to be named in ``INSTALLED_APPS``, in the wheel's ``packages`` list,
and in the ``Dockerfile``'s explicit ``COPY`` block. **None of those fail at test
time** — the unit suite runs from a source checkout where every directory is
importable regardless — so getting one wrong produces a green build and an image
that dies on startup.

Both checks here come from one incident. A new app was added to
``INSTALLED_APPS`` and to the wheel packages, but not to the ``Dockerfile``, so
it was simply absent from the image. It was also named ``events``, which is a
top-level name already shipped by the ``Events`` distribution that
``opensearch-py`` pulls in transitively — so instead of a clean
``ModuleNotFoundError``, ``import events`` silently resolved to the *dependency*,
and the failure surfaced as a baffling ImportError deep in an unrelated module.

Two distinct problems, and both are worth blocking:

- the missing ``COPY`` was the trigger, and :func:`test_every_first_party_app_is_copied_into_the_image` catches it;
- the name collision was what made it hard to read, and it stays latent even
  when everything resolves correctly today, because a transitive dependency can
  claim a bare noun in any future lockfile bump.
"""

import re
from importlib.metadata import packages_distributions
from pathlib import Path

from django.apps import apps
from django.conf import settings

#: The distribution this project itself installs as. Packages it owns are not
#: collisions with themselves.
OWN_DISTRIBUTION = "jawafdehi"


def _first_party_apps():
    """Top-level package names of apps whose code lives in this repo.

    Defined by what is on disk rather than by a name prefix: a third-party app
    that happens to share a name with its own distribution (``auditlog`` ships
    from ``django-auditlog``, ``corsheaders`` from ``django-cors-headers``) is
    not a collision — it is the same package, correctly resolved. Only a
    directory we ship can shadow something.

    Read from the app REGISTRY rather than the raw ``INSTALLED_APPS`` strings.
    An entry may be either ``"case_events"`` or the equally standard
    ``"case_events.apps.EventsConfig"``, and the previous string-based version
    skipped anything containing a dot — so writing an app the dotted way removed
    it from all three checks below at once, silently reopening exactly the hole
    they exist to close.
    """
    base = Path(settings.BASE_DIR).resolve()
    names = set()
    for config in apps.get_app_configs():
        if not Path(config.path).resolve().is_relative_to(base):
            continue  # third-party, installed into site-packages
        top = config.name.split(".")[0]
        if (base / top / "__init__.py").exists():
            names.add(top)
    return sorted(names)


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


def test_every_first_party_app_is_copied_into_the_image():
    """The Dockerfile's COPY list must name every first-party app.

    The list is explicit rather than a single ``COPY . .`` (that would drag the
    venv, .git and test fixtures into the image), which means it silently drifts.
    An app missing here is absent from the image entirely — the build only fails
    later, at ``collectstatic`` or ``migrate``, with an error that points at the
    importer rather than at the omission.
    """
    dockerfile = (settings.BASE_DIR / "Dockerfile").read_text()
    copied = set(re.findall(r"^COPY\s+([A-Za-z_][A-Za-z0-9_]*)/\s", dockerfile, re.MULTILINE))

    missing = [app for app in _first_party_apps() if app not in copied]
    assert not missing, (
        f"These apps are in INSTALLED_APPS but are never COPYed into the image: "
        f"{missing}. Add `COPY {missing[0]}/ ./{missing[0]}/` to the Dockerfile. "
        f"Check the wheel `packages` list in pyproject.toml too."
    )


def test_every_first_party_app_ships_in_the_wheel():
    """The same app list, third copy: hatchling's explicit ``packages``."""
    pyproject = (settings.BASE_DIR / "pyproject.toml").read_text()
    block = pyproject.partition("[tool.hatch.build.targets.wheel]")[2]
    packaged = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', block.partition("]")[0]))

    missing = [app for app in _first_party_apps() if app not in packaged]
    assert not missing, (
        f"These apps are in INSTALLED_APPS but not in the wheel `packages` list: "
        f"{missing}. They would be missing from the installed package."
    )


def test_the_app_list_is_not_empty_and_finds_dotted_appconfigs():
    """Guard the shared input to all three checks above.

    Everything here loops over ``_first_party_apps()``, so if that returns a
    short list the checks pass vacuously. It previously dropped every app
    declared as ``"pkg.apps.SomeConfig"``; ``case_events`` is declared that way
    in its own AppConfig, so it is the natural canary.
    """
    found = _first_party_apps()
    assert len(found) > 5, f"suspiciously few first-party apps: {found}"
    assert "case_events" in found, found
    # And the registry really does expose the dotted form somewhere, so this
    # test is exercising the case it claims to.
    assert any("." in config.name for config in apps.get_app_configs())


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
