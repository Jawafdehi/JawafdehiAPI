import argparse
import json
import logging
import re
from pathlib import Path

import pytest

from casework.common.cli import (
    API_TOKEN_ENV,
    QUIET_LOGGERS,
    add_common_args,
    basic_auth_from_env,
    configure_run_logging,
    log_event,
    log_run_footer,
    log_run_header,
    new_run_id,
    print_summary,
    resolve_api_token,
    setup_logging,
)


def _parse(argv):
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    return parser.parse_args(argv)


def test_api_base_url_defaults_from_env(monkeypatch):
    monkeypatch.setenv("JAWAFDEHI_API_BASE", "https://api.example.test")
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    assert parser.parse_args([]).api_base_url == "https://api.example.test"


def test_api_base_url_has_no_silent_localhost_default(monkeypatch):
    # The dev localhost default is gone: with neither flag nor env set, it is
    # None, and CaseworkApi then raises rather than silently targeting a host.
    monkeypatch.delenv("JAWAFDEHI_API_BASE", raising=False)
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    assert parser.parse_args([]).api_base_url is None


def test_model_default_is_empty_not_haiku():
    # Bumped off the cheap default: "" means "use each stage's configured tier
    # model" (premium stages -> premium model), NOT force-haiku on every tier.
    # bootstrap() with an empty model leaves the per-tier CLAUDE_CLI_MODEL_*
    # settings in force instead of overriding them.
    assert _parse([]).model == ""


def test_provider_default_is_claude_cli():
    assert _parse([]).provider == "claude_cli"


def test_state_flag_defaults_to_draft():
    """Bulk selection gates on DRAFT unless the caller says otherwise.

    The flag exists so the state a bulk run targets is visible at the call
    site (`states=(args.state,)`) instead of being an invisible module
    constant -- and its default is the safe one.
    """
    assert _parse([]).state == "DRAFT"


def test_state_flag_accepts_an_override():
    assert _parse(["--state", "PUBLISHED"]).state == "PUBLISHED"


def test_state_flag_can_be_omitted_for_tools_that_cannot_honour_it():
    """`state_flag=False` must actually remove the argument, not neuter it.

    Registering a flag nothing reads is worse than not offering it: it parses
    cleanly and silently does nothing. `bind_materials` and `convert` opt out
    (see the two tests below); this pins the mechanism they rely on.
    """
    parser = argparse.ArgumentParser()
    add_common_args(parser, state_flag=False)
    args = parser.parse_args([])
    assert not hasattr(args, "state")
    with pytest.raises(SystemExit):
        parser.parse_args(["--state", "IN_REVIEW"])


def test_bind_materials_does_not_advertise_state():
    """The binder gates on the LIVE case state (plan_case's `required_state`),
    not on a selection filter, so `--state` has nothing to act on. Accepting it
    would let `--state IN_REVIEW` parse and leave the operator believing they
    had unlocked review-queue cases when the DRAFT-only invariant still held.
    """
    from casework import bind_materials

    with pytest.raises(SystemExit):
        bind_materials.build_parser().parse_args(
            ["--batch-csv", "x.csv", "--state", "IN_REVIEW"])


def test_convert_does_not_advertise_state():
    """convert has no state gate at all -- same reasoning as the binder."""
    from casework import convert

    with pytest.raises(SystemExit):
        convert.build_parser().parse_args(["--state", "DRAFT"])


# ---------------------------------------------------------------------------
# resolve_api_token: the Bearer token must not have to travel through argv.
# ---------------------------------------------------------------------------


def _token_args(api_token=""):
    return argparse.Namespace(api_token=api_token)


def test_resolve_api_token_reads_the_env_var(monkeypatch):
    monkeypatch.setenv(API_TOKEN_ENV, "env-token")
    assert resolve_api_token(_token_args()) == "env-token"


def test_resolve_api_token_is_empty_when_neither_source_is_set(monkeypatch):
    # "" means "no Bearer token"; callers then fall back to DEV_AUTH Basic.
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    assert resolve_api_token(_token_args()) == ""


def test_resolve_api_token_flag_wins_over_env(monkeypatch):
    # An explicitly passed flag must never be silently ignored -- that would
    # be its own class of bug (a run authenticating as someone else).
    monkeypatch.setenv(API_TOKEN_ENV, "env-token")
    assert resolve_api_token(_token_args("flag-token")) == "flag-token"


def test_resolve_api_token_warns_when_the_token_came_from_argv(monkeypatch, caplog):
    """A token in argv sits in /proc/<pid>/cmdline for the whole run and is
    readable by any local user via `ps -af`. Observed for real -- so using the
    flag must say so, at WARNING."""
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger="casework.cli"):
        resolve_api_token(_token_args("flag-token"))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "--api-token" in message
    assert "ps -af" in message
    assert API_TOKEN_ENV in message
    # The secret itself must never be echoed back into the log.
    assert "flag-token" not in message


def test_resolve_api_token_env_path_is_silent(monkeypatch, caplog):
    monkeypatch.setenv(API_TOKEN_ENV, "env-token")
    with caplog.at_level(logging.WARNING, logger="casework.cli"):
        resolve_api_token(_token_args())
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_resolve_api_token_warning_goes_through_the_logger_not_stdout(
        monkeypatch, capsys):
    """Warn via the logger, not `print`: the run log is the record of what a
    run did, and a bare `print` bypasses it (and the run's log file)."""
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    resolve_api_token(_token_args("flag-token"))
    captured = capsys.readouterr()
    assert captured.out == ""


def test_basic_auth_from_env_returns_credentials(monkeypatch):
    monkeypatch.setenv("CASEWORK_API_USER", "u")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "p")
    assert basic_auth_from_env() == ("u", "p")


def test_basic_auth_from_env_raises_when_unset(monkeypatch):
    # No baked-in dev credential fallback -- must fail loud.
    monkeypatch.delenv("CASEWORK_API_USER", raising=False)
    monkeypatch.delenv("CASEWORK_API_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        basic_auth_from_env()


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


def test_allow_remote_writes_flag_defaults_false():
    args = _parse([])
    assert args.allow_remote_writes is False


def test_allow_remote_writes_flag_parses_true():
    args = _parse(["--allow-remote-writes"])
    assert args.allow_remote_writes is True


# ---------------------------------------------------------------------------
# Shared run-logging foundation: new_run_id / configure_run_logging /
# log_event / log_run_header / log_run_footer. PP2 depends on these exact
# names and signatures.
# ---------------------------------------------------------------------------


def _cleanup_logger(stage):
    """Undo `configure_run_logging`'s handler/filter installation so tests
    don't leak file handles or state onto the process-global logger object
    across test cases (the logger name is reused by module name).
    """
    logger = logging.getLogger(f"casework.{stage}")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    for f in list(logger.filters):
        logger.removeFilter(f)
    for attr in ("_casework_run_marker", "_casework_run_paths"):
        if hasattr(logger, attr):
            delattr(logger, attr)


def test_new_run_id_is_short_and_unique():
    a, b = new_run_id(), new_run_id()
    assert a != b
    assert len(a) == 8
    assert re.fullmatch(r"[0-9a-f]{8}", a)


def test_configure_run_logging_creates_both_files_under_tmp_log_dir(tmp_path):
    stage = "test-create-files"
    try:
        logger, run_id, paths = configure_run_logging(
            stage, run_id="deadbeef", log_dir=str(tmp_path)
        )
        assert Path(paths["log"]).exists()
        assert Path(paths["events"]).exists()
        assert Path(paths["log"]).parent == tmp_path
        assert run_id == "deadbeef"
        assert logger.name == f"casework.{stage}"
    finally:
        _cleanup_logger(stage)


def test_configure_run_logging_same_run_id_does_not_double_handlers(tmp_path):
    stage = "test-idempotent"
    try:
        logger1, _, _ = configure_run_logging(
            stage, run_id="samerun1", log_dir=str(tmp_path)
        )
        count_after_first = len(logger1.handlers)

        logger2, _, _ = configure_run_logging(
            stage, run_id="samerun1", log_dir=str(tmp_path)
        )
        count_after_second = len(logger2.handlers)

        assert count_after_first == count_after_second
        assert logger1 is logger2
    finally:
        _cleanup_logger(stage)


def test_configure_run_logging_different_run_id_replaces_not_accumulates(tmp_path):
    stage = "test-replace"
    try:
        logger1, _, _ = configure_run_logging(
            stage, run_id="run-one", log_dir=str(tmp_path)
        )
        count_after_first = len(logger1.handlers)

        logger2, _, _ = configure_run_logging(
            stage, run_id="run-two", log_dir=str(tmp_path)
        )
        count_after_second = len(logger2.handlers)

        assert count_after_first == count_after_second
    finally:
        _cleanup_logger(stage)


def test_configure_run_logging_defaults_verbose_level(tmp_path):
    stage = "test-verbose"
    try:
        logger, _, _ = configure_run_logging(
            stage, verbose=True, run_id="v1", log_dir=str(tmp_path)
        )
        assert logger.level == logging.DEBUG
    finally:
        _cleanup_logger(stage)


def test_configure_run_logging_uses_env_var_for_default_log_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    stage = "test-env-log-dir"
    try:
        _, _, paths = configure_run_logging(stage, run_id="envrun")
        assert Path(paths["log"]).parent == tmp_path
    finally:
        _cleanup_logger(stage)


def test_configure_run_logging_falls_back_to_repo_root_work_dir(monkeypatch, tmp_path):
    import casework.common.cli as cli_module

    monkeypatch.delenv("CASEWORK_RUN_LOG_DIR", raising=False)
    fake_default = tmp_path / "work" / "enricher-runs"
    monkeypatch.setattr(cli_module, "_DEFAULT_LOG_DIR", fake_default)
    stage = "test-default-log-dir"
    try:
        _, _, paths = configure_run_logging(stage, run_id="defaultrun")
        assert Path(paths["log"]).parent == fake_default
        assert fake_default.exists()
    finally:
        _cleanup_logger(stage)


def test_run_log_lines_are_utc_iso_timestamped(tmp_path):
    stage = "test-format"
    try:
        logger, run_id, paths = configure_run_logging(
            stage, run_id="fmtrun1", log_dir=str(tmp_path)
        )
        logger.info("hello from the format test")
        for h in logger.handlers:
            h.flush()
        content = Path(paths["log"]).read_text(encoding="utf-8")
        assert re.search(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z (DEBUG|INFO|WARNING|ERROR)\s+"
            rf"\[run={run_id} stage={stage}\] hello from the format test",
            content,
            re.MULTILINE,
        )
    finally:
        _cleanup_logger(stage)


def test_log_event_writes_one_parseable_json_line_with_required_keys(tmp_path):
    events_path = tmp_path / "events.jsonl"
    logger = logging.getLogger("test.log_event.keys")

    log_event(
        logger, str(events_path),
        run_id="r1", stage="bigo", slug="some-case", step="fetch",
        status="ok", detail="fetched fine", elapsed_ms=42,
    )

    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["run_id"] == "r1"
    assert record["stage"] == "bigo"
    assert record["slug"] == "some-case"
    assert record["step"] == "fetch"
    assert record["status"] == "ok"
    assert record["detail"] == "fetched fine"
    assert record["elapsed_ms"] == 42
    assert "ts" in record and record["ts"]


def test_log_event_devanagari_survives_round_trip_not_escaped(tmp_path):
    events_path = tmp_path / "events.jsonl"
    logger = logging.getLogger("test.log_event.devanagari")
    slug = "श्री-५-को-सरकारी-मुद्दा"
    detail = "बिगो रकम पुष्टि भयो"

    log_event(
        logger, str(events_path),
        run_id="r1", stage="bigo", slug=slug, step="fetch",
        status="ok", detail=detail,
    )

    raw = events_path.read_text(encoding="utf-8")
    assert "\\u" not in raw
    assert slug in raw
    assert detail in raw
    record = json.loads(raw.splitlines()[0])
    assert record["slug"] == slug
    assert record["detail"] == detail


def test_log_event_emits_human_readable_line_through_logger(tmp_path, caplog):
    events_path = tmp_path / "events.jsonl"
    logger = logging.getLogger("test.log_event.human")

    with caplog.at_level(logging.INFO, logger="test.log_event.human"):
        log_event(
            logger, str(events_path),
            run_id="r1", stage="bigo", slug="some-case", step="fetch",
            status="ok", detail="fetched fine", elapsed_ms=42,
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "some-case" in m and "step=fetch" in m and "status=ok" in m
        and "fetched fine" in m and "42ms" in m
        for m in messages
    )


def test_log_run_header_is_one_info_record_with_target_mode_and_paths(caplog):
    logger = logging.getLogger("test.log_run_header")
    paths = {"log": "/tmp/x.log", "events": "/tmp/x.events.jsonl"}

    with caplog.at_level(logging.INFO, logger="test.log_run_header"):
        log_run_header(
            logger, stage="bigo", base_url="http://127.0.0.1:48010",
            dry_run=True, provider="claude_cli", model="haiku",
            n_selected=12, run_id="abc12345", paths=paths,
        )

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    message = info_records[0].getMessage()
    assert "http://127.0.0.1:48010" in message
    assert "DRY-RUN" in message
    assert "claude_cli" in message
    assert "haiku" in message
    assert "12" in message
    assert "abc12345" in message
    assert paths["log"] in message
    assert paths["events"] in message


def test_log_run_header_shows_apply_when_not_dry_run(caplog):
    logger = logging.getLogger("test.log_run_header_apply")
    paths = {"log": "/tmp/x.log", "events": "/tmp/x.events.jsonl"}

    with caplog.at_level(logging.INFO, logger="test.log_run_header_apply"):
        log_run_header(
            logger, stage="bigo", base_url="http://127.0.0.1:48010",
            dry_run=False, provider="claude_cli", model="haiku",
            n_selected=0, run_id="abc12345", paths=paths,
        )

    message = caplog.records[0].getMessage()
    assert "APPLY" in message
    assert "DRY-RUN" not in message


def test_log_run_footer_is_one_info_record_with_counts_and_duration(caplog):
    logger = logging.getLogger("test.log_run_footer")

    with caplog.at_level(logging.INFO, logger="test.log_run_footer"):
        log_run_footer(
            logger, stage="bigo",
            stats={"enriched": 3, "skipped": 1, "error": 0},
            duration_s=12.345, usage_summary="tokens: 4200 in / 900 out",
        )

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    message = info_records[0].getMessage()
    assert "enriched=3" in message
    assert "skipped=1" in message
    assert "error=0" in message
    assert "12.3" in message
    assert "tokens: 4200 in / 900 out" in message
