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


def _copied_packages():
    """Top-level package names the Dockerfile actually COPYs into the image."""
    dockerfile = (settings.BASE_DIR / "Dockerfile").read_text()
    return set(re.findall(r"^COPY\s+([A-Za-z_][A-Za-z0-9_]*)/\s", dockerfile, re.MULTILINE))


def _first_party_packages():
    """Every top-level importable package in the repo, app or not."""
    base = Path(settings.BASE_DIR).resolve()
    return {
        p.name for p in base.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith(".")
    }


#: `import X` / `from X import ...`, including indented (function-local) ones.
_IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def test_shipped_code_never_imports_a_package_the_image_lacks():
    """A COPYed package may only import other COPYed packages.

    The INSTALLED_APPS checks above miss a whole class of this bug: a package
    that is NOT a Django app (so not in the registry, so invisible to
    ``_first_party_apps``) but IS imported by shipped code. ``casework`` is one --
    a local-only helper package, never COPYed. When ``extract_verdicts`` imported
    ``casework.convert`` for its document conversion, every check in this file
    passed, CI passed, the image built, and the command failed on all 84 cases in
    production with ``No module named 'casework'``. It had worked in development
    for the same reason the docstring at the top of this file gives: a source
    checkout makes every directory importable.

    Function-local imports are included deliberately -- that is where the real
    one was, and where they hide best.
    """
    copied = _copied_packages()
    first_party = _first_party_packages()
    base = Path(settings.BASE_DIR).resolve()

    offenders = {}
    for pkg in sorted(copied):
        for py in sorted((base / pkg).rglob("*.py")):
            if "/migrations/" in str(py) or "/tests/" in str(py):
                continue
            for name in set(_IMPORT.findall(py.read_text(encoding="utf-8"))):
                if name in first_party and name not in copied:
                    offenders.setdefault(name, []).append(str(py.relative_to(base)))

    assert not offenders, (
        "Shipped code imports first-party packages that are never COPYed into "
        f"the image: { {k: v[:3] for k, v in offenders.items()} }. Either add "
        "`COPY <pkg>/ ./<pkg>/` to the Dockerfile (and the wheel `packages` "
        "list) or import something that ships."
    )


def test_the_import_check_reads_function_local_imports():
    """Guard the guard: the regex must see an indented import.

    The real defect was a lazy `from casework.convert import ...` inside a
    method. A top-level-only regex would pass this file forever while missing
    exactly the shape that caused the incident.
    """
    sample = "def f():\n    from casework.convert import extract_markdown\n    import llm\n"
    assert set(_IMPORT.findall(sample)) == {"casework", "llm"}


def test_the_import_check_has_something_to_check():
    """Guard the guard: empty inputs would make the scan pass vacuously."""
    copied, first_party = _copied_packages(), _first_party_packages()
    assert len(copied) > 10, f"suspiciously few COPYed packages: {sorted(copied)}"
    assert "courts" in copied and "review" in copied, sorted(copied)
    # A first-party package that is deliberately NOT shipped must exist, or the
    # check above can never fire and its passing means nothing.
    assert first_party - copied, (
        "Every first-party package is COPYed, so the scan cannot distinguish "
        "shipped from unshipped. If that is now genuinely true, keep the scan "
        "but drop this guard."
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
