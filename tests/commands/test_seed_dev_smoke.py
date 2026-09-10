"""``seed_dev`` runs. That is the whole assertion, and it is worth a test.

The command is a CI gate -- the e2e job seeds through it before indexing -- but
nothing in the unit suite ran it, so a rename that touched only this file went
green locally and failed in CI. It touches four apps' models with literal field
names and no serializer in front of them, which is exactly the shape that rots
silently under a rename.

The specific bug: ``Case.case_type`` was renamed to ``offence_type`` and the
rename leaked onto the ``CourtCase`` created here. ``CourtCase.case_type`` is a
different model's own field -- the court's मुद्दाको किसिम off the NGM scrape --
so it died with ``FieldError: Invalid field name(s) for model CourtCase``.
"""

import pytest
from django.core.management import call_command

from cases.models import Case
from courts.models import CourtCase


@pytest.mark.django_db
def test_seed_dev_runs():
    call_command("seed_dev", verbosity=0)

    assert Case.objects.exists()
    assert CourtCase.objects.exists()


@pytest.mark.django_db
def test_seed_dev_is_idempotent():
    """The docstring promises it; the e2e job re-runs against a warm volume."""
    call_command("seed_dev", verbosity=0)
    before = (Case.objects.count(), CourtCase.objects.count())

    call_command("seed_dev", verbosity=0)

    assert (Case.objects.count(), CourtCase.objects.count()) == before
