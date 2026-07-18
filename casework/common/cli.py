"""Shared argparse, logging and reporting for casework enricher CLIs."""

import logging

QUIET_LOGGERS = ("httpx", "urllib3", "boto3", "botocore", "s3transfer")


def add_common_args(parser):
    """Register the CLI flags every ported enricher shares.

    `--dry-run` defaults to True and `--apply` opts into writes, matching the
    donor management commands' safety posture: an enricher run is read-only
    (prints what it WOULD do) unless the caller explicitly asks it to write.
    """
    parser.add_argument("--slug", action="append", default=[])
    parser.add_argument("--court-case", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fiscal-year", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", dest="dry_run", action="store_false")
    parser.add_argument("--provider", default="claude_cli")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:48010")
    parser.add_argument("--api-token", default="")
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
