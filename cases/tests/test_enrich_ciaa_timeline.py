"""Tests for the enrich_ciaa_timeline management command.

The command created/read DocumentSource rows to source timeline content. Under
the ADR "cases own no documents", DocumentSource has been removed and the
command is now stubbed to raise NotImplementedError. This test pins that
contract.
"""

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_enrich_ciaa_timeline_raises_not_implemented():
    """The command is stubbed out; invoking it raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        call_command("enrich_ciaa_timeline", "--dry-run")
