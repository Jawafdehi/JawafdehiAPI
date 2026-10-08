"""Guards for the ``treebeard.E001`` silence in ``config.settings``.

That silence is deliberate but temporary — the reasoning is beside
``SILENCED_SYSTEM_CHECKS``. These tests are what makes it temporary rather than
permanent: one fails the day upstream fixes the warning (so we delete the entry),
and one fails the day something *else* starts being hidden by it.
"""

from django.conf import settings
from django.core.checks import run_checks


def test_silence_list_stays_scoped_to_the_treebeard_warning():
    """Nothing may be added here without its own justification.

    A broad ``SILENCED_SYSTEM_CHECKS`` is how a real check gets lost, so pin the
    exact contents rather than merely asserting the treebeard id is present.
    """
    assert settings.SILENCED_SYSTEM_CHECKS == ["treebeard.E001"]


def test_treebeard_warning_is_still_raised_by_upstream():
    """When this fails, the silence has outlived its cause — delete it.

    django-treebeard 5.3 flags Wagtail's ``BasePageManager`` and
    ``BaseCollectionManager`` for not subclassing ``MP_NodeManager``. Neither
    class is ours. The day Wagtail ships MP_NodeManager-derived managers (or we
    take a treebeard that drops the check), this stops firing and the entry in
    ``SILENCED_SYSTEM_CHECKS`` should go with it.
    """
    treebeard_messages = [m for m in run_checks() if m.id == "treebeard.E001"]

    assert treebeard_messages, (
        "treebeard.E001 is no longer raised — upstream appears to have fixed it. "
        "Remove 'treebeard.E001' from SILENCED_SYSTEM_CHECKS in config/settings.py "
        "and delete this test."
    )


def test_silencing_hides_only_the_treebeard_warning():
    """Every message the silence suppresses must be the one it was added for."""
    silenced = [m for m in run_checks() if m.is_silenced()]

    assert {m.id for m in silenced} == {"treebeard.E001"}


def test_no_system_check_issues_survive_unsilenced():
    """The check run is clean, so a new warning is visible instead of buried."""
    unsilenced = [m for m in run_checks() if not m.is_silenced()]

    assert unsilenced == [], "\n".join(str(m) for m in unsilenced)
