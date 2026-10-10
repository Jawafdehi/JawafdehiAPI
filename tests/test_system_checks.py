"""Guards for the ``treebeard.E001`` silence in ``config.settings``.

That silence is deliberate but temporary — the reasoning is beside
``SILENCED_SYSTEM_CHECKS``. These tests are what makes it temporary rather than
permanent: one fails the day upstream fixes the warning (so we delete the entry),
one fails if it is ever escalated to an Error, and one fails the day something
*else* starts being hidden by it.

Deliberately NOT here: an assertion that the whole project's check run is clean.
That is a project-wide invariant, it would red-line this treebeard-specific
module on any unrelated dependency bump, and under settings_test it would only
ever prove cleanliness on sqlite anyway.
"""

from django.conf import settings
from django.core import checks
from django.core.checks import run_checks


def test_silence_list_stays_scoped_to_the_treebeard_warning():
    """Nothing may be added here without its own justification.

    A broad ``SILENCED_SYSTEM_CHECKS`` is how a real check gets lost, so pin the
    exact contents rather than merely asserting the treebeard id is present.
    """
    assert settings.SILENCED_SYSTEM_CHECKS == ["treebeard.E001"]


def test_treebeard_warning_is_still_raised_by_upstream():
    """When this fails, the silence has outlived its cause — delete it.

    django-treebeard 5.3 flags any MP_Node whose default manager does not
    subclass ``MP_NodeManager``. The managers are Wagtail's — ``BasePageManager``
    and ``BaseCollectionManager``, both plain ``models.Manager`` — so we cannot
    fix this from here. The day Wagtail ships MP_NodeManager-derived managers (or
    we take a treebeard that drops the check), this stops firing and the entry in
    ``SILENCED_SYSTEM_CHECKS`` should go with it.
    """
    treebeard_messages = [m for m in run_checks() if m.id == "treebeard.E001"]

    assert treebeard_messages, (
        "treebeard.E001 is no longer raised — upstream appears to have fixed it. "
        "Remove 'treebeard.E001' from SILENCED_SYSTEM_CHECKS in config/settings.py "
        "and delete this test."
    )


def test_treebeard_message_is_still_only_a_warning():
    """The transition this silence must NOT survive: Warning -> Error.

    ``SILENCED_SYSTEM_CHECKS`` suppresses by id, and Django builds its fatal set
    from ``not e.is_silenced()`` (``core/management/base.py``). So if treebeard 6
    escalates E001 to ``checks.Error``, the silence keeps hiding it, ``manage.py
    check`` still exits 0, and a genuinely fatal misconfiguration ships.

    Asserting the id alone does not catch that — verified by probe: an Error
    carrying this id leaves every other test in this file green. The level is the
    assertion that matters.
    """
    levels = {m.level for m in run_checks() if m.id == "treebeard.E001"}

    assert levels, "treebeard.E001 is not being raised at all — see the test above."
    assert max(levels) < checks.ERROR, (
        "treebeard.E001 is now raised at ERROR level, so silencing it hides a "
        "fatal check. Stop silencing it and fix the managers (or pin treebeard)."
    )


def test_silencing_hides_only_managers_we_do_not_own():
    """Pin WHICH managers are silenced — the only part that is ours to judge.

    An earlier version of this test pinned the COUNT at 4. That was wrong twice
    over: it red-lined on any new Wagtail page type (routine, and its failure
    message prescribed a fix that cannot be applied, since the manager is
    Wagtail's), and it never checked identity, so a same-count substitution —
    drop a page model, add a repo-owned MP_Node with a plain manager — passed
    green while hiding a warning that genuinely IS ours to fix.

    The message's ``obj`` is the MANAGER class, not the model, and every Wagtail
    ``Page`` subclass shares one. So the manager set is invariant under adding
    page models and changes exactly when someone introduces a tree model whose
    manager we control. That is the signal worth failing on.
    """
    silenced = [m for m in run_checks() if m.is_silenced()]

    assert {m.id for m in silenced} == {"treebeard.E001"}
    assert {m.obj.__name__ for m in silenced} == {
        "BasePageManagerFromPageQuerySet",
        "BaseCollectionManagerFromCollectionQuerySet",
    }, (
        "A treebeard.E001 is being silenced for a manager that is not one of "
        "Wagtail's two. If the manager is ours, make it subclass "
        "MP_NodeManager instead of letting this silence swallow it."
    )
