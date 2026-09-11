"""``scrape_case``'s flags must map to the dests ``handle()`` reads.

argparse does not check this. ``--case-type`` builds ``options["case_type"]``,
so when the rename to ``offence_type`` reached the handler but not the flag,
the command kept parsing cleanly and raised ``KeyError: 'offence_type'`` only
once someone passed ``--create-db-entry``. Nothing in the suite ran it.
"""

import pytest
from django.core.management import load_command_class


@pytest.fixture
def parser():
    return load_command_class("cases", "scrape_case").create_parser("manage.py", "scrape_case")


def test_the_offence_flag_lands_on_the_dest_the_handler_reads(parser):
    parsed = parser.parse_args(["some.json", "--offence-type", "CORRUPTION"])

    assert parsed.offence_type == "CORRUPTION"


def test_the_old_case_type_spelling_still_works(parser):
    """A rename inside the codebase is no reason to break a shell history."""
    parsed = parser.parse_args(["some.json", "--case-type", "CORRUPTION"])

    assert parsed.offence_type == "CORRUPTION"


def test_every_flag_the_handler_reads_exists(parser):
    """The general form of the bug, not just the one instance of it."""
    import inspect

    from cases.management.commands import scrape_case

    source = inspect.getsource(scrape_case.Command.handle)
    read = set(__import__("re").findall(r'options\["(\w+)"\]', source))
    dests = {action.dest for action in parser._actions}

    assert read <= dests, f"handle() reads options with no flag: {read - dests}"
