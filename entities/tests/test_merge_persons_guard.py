"""manage.py merge_persons — superseded, and refuses to run without an override."""

import pytest
from django.core.management import CommandError, call_command


def test_merge_persons_refuses_to_run_without_the_override_flag():
    with pytest.raises(CommandError):
        call_command("merge_persons")
