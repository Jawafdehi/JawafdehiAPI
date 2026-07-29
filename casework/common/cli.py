"""Shared argparse, logging and reporting for casework enricher CLIs."""

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from casework.common.select import DEFAULT_ENRICHABLE_STATE

QUIET_LOGGERS = ("httpx", "urllib3", "boto3", "botocore", "s3transfer")

# Env var carrying the Bearer token. This is the documented, default path --
# see `resolve_api_token` for why the `--api-token` flag is not.
API_TOKEN_ENV = "JAWAFDEHI_API_TOKEN"

logger = logging.getLogger("casework.cli")

# casework/common/cli.py -> casework/common -> casework -> <repo-root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_LOG_DIR = _REPO_ROOT / "work" / "enricher-runs"


def basic_auth_from_env():
    """(user, password) for local DEV_AUTH Basic auth, from CASEWORK_API_USER /
    CASEWORK_API_PASSWORD. Raises if either is unset -- there is deliberately NO
    dev-default fallback, so a missing credential fails loud instead of silently
    authenticating as a baked-in dev account."""
    user = os.environ.get("CASEWORK_API_USER")
    password = os.environ.get("CASEWORK_API_PASSWORD")
    if not (user and password):
        raise SystemExit(
            f"API credentials required: set {API_TOKEN_ENV} for Bearer auth, or "
            "set CASEWORK_API_USER + CASEWORK_API_PASSWORD for local DEV_AUTH "
            "Basic auth.")
    return user, password


def resolve_api_token(args):
    """Bearer token for `CaseworkApi`: `$JAWAFDEHI_API_TOKEN` by default.

    The env var is the documented path because argparse is not a safe place
    to put a credential: a token passed as `--api-token <secret>` sits in
    `/proc/<pid>/cmdline` for the whole run and is readable by *any* local
    user with a plain `ps -af` (and lands in shell history). That was observed
    happening for real, which is why this indirection exists at all.

    The flag still works -- removing it outright would break every existing
    runbook/wrapper mid-port -- but it wins over the env var (so an explicit
    flag is never silently ignored, which would be its own class of bug) and
    warns through the logger, not `print`, so the warning shares the run log
    with everything else instead of vanishing into stdout.

    Returns "" when neither source is set; callers treat that as "no Bearer
    token" and fall back to local DEV_AUTH Basic auth.
    """
    flag_token = getattr(args, "api_token", "") or ""
    if flag_token:
        logger.warning(
            "--api-token was passed on the command line: the bearer token is "
            "visible in /proc/<pid>/cmdline (`ps -af`) to every local user for "
            "the duration of this run, and is now in your shell history. Set "
            "%s in the environment instead.", API_TOKEN_ENV)
        return flag_token
    return os.environ.get(API_TOKEN_ENV, "") or ""


def add_common_args(parser, *, state_flag=True):
    """Register the CLI flags every ported enricher shares.

    `state_flag=False` omits `--state`. Only the five enrichers route their
    selection through `select_cases`, so only they can honour it. `convert`
    has no state gate at all, and `bind_materials` enforces its own DRAFT-only
    invariant against the LIVE case (`plan_case`'s `required_state`) rather
    than against a selection filter. Offering them the flag registers an
    argument nothing reads: `--state IN_REVIEW` on the binder would parse
    cleanly, change nothing, and leave the operator believing they had
    unlocked review-queue cases. Fail-safe, but a CLI that silently ignores a
    flag is a defect -- so the two tools that cannot honour it do not
    advertise it.

    `--dry-run` defaults to True and `--apply` opts into writes. This
    deliberately INVERTS the donor's default: the donor's `add_common_args`
    (`casework/common.py:376` at donor commit 0321a85) declared
    `parser.add_argument("--dry-run", action="store_true")` with no
    `default=True`, so the donor defaulted to `dry_run=False` -- i.e. it
    applied writes unless the caller opted in to `--dry-run`. That posture is
    unacceptable under this project's binding NO PRODUCTION CHANGES
    WHATSOEVER constraint while these enrichers are being ported/tested, so
    the default is flipped here: a bare invocation is read-only (prints what
    it WOULD do), and the caller must explicitly pass `--apply` to write.
    """
    parser.add_argument("--slug", action="append", default=[])
    parser.add_argument("--court-case", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fiscal-year", default="")
    parser.add_argument("--force", action="store_true")
    if state_flag:
        parser.add_argument(
            "--state", default=DEFAULT_ENRICHABLE_STATE,
            help=f"Workflow state bulk selection gates on (default "
                 f"{DEFAULT_ENRICHABLE_STATE}). Bulk runs previously took DRAFT "
                 "*and* IN_REVIEW, silently rewriting cases a moderator already "
                 "had open; IN_REVIEW is now refused outright for bulk selection "
                 "(pass an explicit --slug/--court-case to act on one such case "
                 "knowingly). Ignored when --slug/--court-case is given -- those "
                 "bypass the state gate by design.")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", dest="dry_run", action="store_false")
    parser.add_argument(
        "--provider", default="claude_cli",
        help="LLM provider to route both tiers through (default claude_cli).")
    parser.add_argument(
        "--model", default="",
        help="LLM model/alias to force on both tiers via the claude_cli "
             "provider's model settings (other providers use their own model "
             "config and ignore this). Empty (the default) lets each stage use "
             "its configured tier model -- premium stages (bigo, timeline, "
             "allegations, entities) get the premium model, tags gets the cheap "
             "one -- falling back to the provider CLI's own default. An earlier "
             "default of 'haiku' overrode every tier with the cheap model; pass "
             "--model haiku explicitly to restore that for a cheap run.")
    parser.add_argument(
        "--api-base-url", default=os.environ.get("JAWAFDEHI_API_BASE"),
        help="Base URL of the case API; defaults to $JAWAFDEHI_API_BASE. If "
             "neither the flag nor the env var is set, the client raises rather "
             "than silently targeting a host. Local DEV_AUTH server: "
             "http://127.0.0.1:48010")
    parser.add_argument(
        "--api-token", default="",
        help=f"DISCOURAGED. Bearer token; prefer ${API_TOKEN_ENV} in the "
             "environment, which is what every client here reads by default. A "
             "token passed here is readable by any local user via `ps -af` for "
             "the whole run -- see resolve_api_token(). Kept only so existing "
             "runbooks keep working; it warns when used.")
    parser.add_argument(
        "--allow-remote-writes", action="store_true", default=False,
        help=(
            "Required to write (PATCH) to any non-loopback --api-base-url; "
            "meaningless without --apply. Reads are never guarded. "
            "CaseworkApi refuses PATCH requests to a non-loopback host unless "
            "this is set."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def setup_logging(verbose=False):
    """Configure root logging level and quiet the noisy third-party loggers."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def print_summary(stats, dry_run, title):
    """Print a run summary: title, dry-run/applied banner, sorted stat lines."""
    print(f"\n=== {title} ({'DRY RUN' if dry_run else 'APPLIED'}) ===")
    for key in sorted(stats):
        print(f"  {key}: {stats[key]}")


# ---------------------------------------------------------------------------
# Shared run-logging foundation.
#
# `enrich_*.py` currently narrate each case with `print()` -- no timestamps,
# no levels, no persisted file, no structured record. This is the shared
# layer the next task migrates those enrichers onto; nothing here is wired
# into them yet.
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _UTCFormatter(logging.Formatter):
    """Formatter whose %(asctime)s is UTC, not local time."""
    converter = time.gmtime


class _RunContextFilter(logging.Filter):
    """Injects run_id/stage attributes onto every record from a run logger."""

    def __init__(self, run_id: str, stage: str):
        super().__init__()
        self.run_id = run_id
        self.stage = stage

    def filter(self, record):
        record.run_id = self.run_id
        record.stage = self.stage
        return True


_RUN_LOG_FORMAT = "%(asctime)s %(levelname)-5s [run=%(run_id)s stage=%(stage)s] %(message)s"
_RUN_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%SZ"


def new_run_id() -> str:
    """Short unique id for one enricher invocation, e.g. 'a1b2c3d4'."""
    return uuid.uuid4().hex[:8]


def configure_run_logging(stage: str, *, verbose: bool = False, run_id: str | None = None,
                          log_dir: str | None = None) -> tuple[logging.Logger, str, dict]:
    """Configure the shared "casework.<stage>" run logger.

    Returns ``(logger, run_id, paths)`` where ``paths = {"log": <str>,
    "events": <str>}``. Idempotent: a second call for the same ``(stage,
    run_id)`` returns the existing logger/paths without adding duplicate
    handlers. Calling again for the same ``stage`` with a DIFFERENT
    ``run_id`` replaces the handlers (this logger is process-lifetime, one
    configuration per run).
    """
    run_id = run_id or new_run_id()
    logger = logging.getLogger(f"casework.{stage}")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    # This logger owns its own handlers (stdout + file); don't also double
    # the output through root's handlers (e.g. `setup_logging`'s basicConfig).
    logger.propagate = False

    marker = (stage, run_id)
    if getattr(logger, "_casework_run_marker", None) == marker:
        return logger, run_id, logger._casework_run_paths

    resolved_dir = Path(log_dir) if log_dir else Path(
        os.environ.get("CASEWORK_RUN_LOG_DIR") or _DEFAULT_LOG_DIR
    )
    resolved_dir.mkdir(parents=True, exist_ok=True)

    ts_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = resolved_dir / f"{ts_compact}-{stage}-{run_id}.log"
    events_path = resolved_dir / f"{ts_compact}-{stage}-{run_id}.events.jsonl"
    log_path.touch(exist_ok=True)
    events_path.touch(exist_ok=True)

    # Drop any handlers/filters this function previously installed on this
    # (reused, since logger names are process-global) logger instance.
    for h in list(logger.handlers):
        if getattr(h, "_casework_owned", False):
            logger.removeHandler(h)
            h.close()
    for f in list(logger.filters):
        if getattr(f, "_casework_owned", False):
            logger.removeFilter(f)

    formatter = _UTCFormatter(_RUN_LOG_FORMAT, datefmt=_RUN_LOG_DATEFMT)
    run_filter = _RunContextFilter(run_id, stage)
    run_filter._casework_owned = True
    logger.addFilter(run_filter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler._casework_owned = True
    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler._casework_owned = True
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)

    paths = {"log": str(log_path), "events": str(events_path)}
    logger._casework_run_marker = marker
    logger._casework_run_paths = paths
    return logger, run_id, paths


def log_event(logger: logging.Logger, events_path: str, *, run_id: str, stage: str,
              slug: str, step: str, status: str, detail: str = "",
              elapsed_ms: int | None = None, level: int = logging.INFO) -> None:
    """Append one JSON event to `events_path` AND emit a human-readable line.

    The JSON line is written with ``ensure_ascii=False`` so Devanagari
    slugs/detail survive round-trip instead of being ``\\uXXXX``-escaped.
    """
    record = {
        "ts": _utc_iso_now(),
        "run_id": run_id,
        "stage": stage,
        "slug": slug,
        "step": step,
        "status": status,
        "detail": detail,
        "elapsed_ms": elapsed_ms,
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    message = f"[{slug}] step={step} status={status}"
    if detail:
        message += f" {detail}"
    if elapsed_ms is not None:
        message += f" ({elapsed_ms}ms)"
    logger.log(level, message)


def log_run_header(logger, *, stage, base_url, dry_run, provider, model,
                   n_selected, run_id, paths) -> None:
    """One INFO block naming the run target, mode, provider/model, and where
    the log/events files are being written."""
    mode = "DRY-RUN" if dry_run else "APPLY"
    lines = [
        f"=== casework run: {stage} ===",
        f"  target      : {base_url}",
        f"  mode        : {mode}",
        f"  provider    : {provider}",
        f"  model       : {model or '(provider default)'}",
        f"  n_selected  : {n_selected}",
        f"  run_id      : {run_id}",
        f"  log file    : {paths['log']}",
        f"  events file : {paths['events']}",
    ]
    logger.info("\n".join(lines))


def log_run_footer(logger, *, stage, stats: dict, duration_s: float,
                   usage_summary: str = "") -> None:
    """One INFO block: per-status counts, wall-clock duration, usage summary."""
    counts = ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "(no cases)"
    lines = [
        f"=== casework run complete: {stage} ===",
        f"  counts   : {counts}",
        f"  duration : {duration_s:.1f}s",
    ]
    if usage_summary:
        lines.append(f"  usage    : {usage_summary}")
    logger.info("\n".join(lines))
