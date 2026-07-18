import argparse
import logging

from casework.common.cli import (
    QUIET_LOGGERS,
    add_common_args,
    print_summary,
    setup_logging,
)


def _parse(argv):
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    return parser.parse_args(argv)


def test_dry_run_defaults_to_true_with_no_flags():
    """A bare invocation (no --dry-run/--apply) must be read-only.

    This is the priority guarantee: under this project's binding NO
    PRODUCTION CHANGES WHATSOEVER constraint, a caller who forgets to pass
    any write-related flag must NOT accidentally write.
    """
    args = _parse([])
    assert args.dry_run is True


def test_apply_flag_sets_dry_run_false():
    """--apply is the only way to opt into writes."""
    args = _parse(["--apply"])
    assert args.dry_run is False


def test_dry_run_flag_is_explicit_but_redundant_with_default():
    """--dry-run explicitly requested must still yield dry_run=True."""
    args = _parse(["--dry-run"])
    assert args.dry_run is True


def test_apply_and_dry_run_share_one_dest():
    """--apply and --dry-run write the same `dry_run` dest (dest="dry_run" on
    --apply). Confirms they aren't two independent, silently-conflicting
    flags -- the last one parsed wins on the shared dest.
    """
    assert _parse(["--dry-run", "--apply"]).dry_run is False
    assert _parse(["--apply", "--dry-run"]).dry_run is True


def test_setup_logging_quiets_noisy_third_party_loggers():
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    setup_logging(verbose=False)

    for name in QUIET_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def _reset_root_handlers():
    """`logging.basicConfig` is a no-op once the root logger already has a
    handler (pytest's own log-capture plugin installs one), so it would
    silently skip setting the level unless we clear handlers first to mimic
    a fresh process.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    return root, saved


def test_setup_logging_sets_root_level_info_by_default():
    root, saved = _reset_root_handlers()
    try:
        setup_logging(verbose=False)
        assert root.level == logging.INFO
    finally:
        root.handlers = saved


def test_setup_logging_sets_root_level_debug_when_verbose():
    root, saved = _reset_root_handlers()
    try:
        setup_logging(verbose=True)
        assert root.level == logging.DEBUG
    finally:
        root.handlers = saved


def test_print_summary_dry_run_banner_and_sorted_stats(capsys):
    print_summary({"b_stat": 2, "a_stat": 1}, dry_run=True, title="Tags")
    out = capsys.readouterr().out
    assert "=== Tags (DRY RUN) ===" in out
    # "a_stat" line must appear before "b_stat" -- stats are sorted by key.
    assert out.index("a_stat: 1") < out.index("b_stat: 2")


def test_print_summary_applied_banner(capsys):
    print_summary({}, dry_run=False, title="Tags")
    out = capsys.readouterr().out
    assert "=== Tags (APPLIED) ===" in out
