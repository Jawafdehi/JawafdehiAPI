"""Guards for the ``treebeard.E001`` silence in ``config.settings``.

That silence is deliberate but temporary — the reasoning is beside
``SILENCED_SYSTEM_CHECKS``. These tests are what makes it temporary rather than
permanent: one fails the day upstream fixes the warning (so we delete the entry),
and one fails the day something *else* starts being hidden by it.
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


def test_silencing_hides_only_the_four_known_upstream_models():
    """Pin WHICH objects are silenced, not just the id.

    Comparing ids alone is too weak: a new MP_Node in this repo with a plain
    manager raises the same id, gets swallowed by the same silence, and nothing
    fails — verified by probe. The four below are the known set; two are
    Wagtail's own and two are ours (every Wagtail ``Page`` subclass is an
    MP_Node, so our page models trip the same upstream manager).

    A fifth entry here means someone added a tree model whose manager genuinely
    is ours to fix. Fix the manager rather than widening this list.
    """
    silenced = [m for m in run_checks() if m.is_silenced()]

    assert {m.id for m in silenced} == {"treebeard.E001"}
    assert len(silenced) == 4, (
        "The set of objects silenced by treebeard.E001 changed. Expected the 4 "
        "known MP_Node models (wagtailcore Collection + Page, and our "
        "content.ArticleIndexPage + content.ArticlePage); got "
        f"{len(silenced)}. A new one is probably a model of ours whose manager "
        "should subclass MP_NodeManager instead of being silenced."
    )


def test_no_system_check_issues_survive_unsilenced():
    """The check run is clean, so a new warning is visible instead of buried."""
    unsilenced = [m for m in run_checks() if not m.is_silenced()]

    assert unsilenced == [], "\n".join(str(m) for m in unsilenced)
