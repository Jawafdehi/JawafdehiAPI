"""Submit a batch of cases into the casework review queue, and read the grades back.

Two modes. The default POSTs each slug to `/api/casework/reviews/submit/`; `--report`
reads back what the grading produced and writes nothing. They are one module because
they share the batch selection and the review-row shape.

The script reads no case. A slug is all the POST needs, and the review list row already
carries the title, state, score and disposition the report shows -- so a 238-case batch
costs 2 requests per case to submit and 1 to report, with no corpus listing in front of
either.
"""

import argparse
import json
import logging
import sys
import time
import urllib.error

from casework.common.api import CaseworkApi
from casework.common.cli import (
    add_common_args,
    basic_auth_from_env,
    configure_run_logging,
    format_counts,
    log_event,
    log_run_footer,
    log_run_header,
)
from casework.common.select import slugs_from_batch_csv

STAGE = "submit_reviews"


def slugs_for_run(args):
    """The slugs to work on, in batch order, capped by `--limit`.

    A run with neither `--batch-csv` nor `--slug` is refused rather than falling
    through to the whole corpus: that fallback would enqueue ~3,000 LLM grading runs
    off a forgotten flag. There is no state gate -- a PUBLISHED case is a legitimate
    review target, unlike an enrichment target.
    """
    slugs = []
    if args.batch_csv:
        slugs.extend(slugs_from_batch_csv(args.batch_csv))
    slugs.extend(s for s in args.slug if s not in slugs)
    if not slugs:
        raise SystemExit(
            "nothing selected: pass --batch-csv or --slug. A bare run is refused "
            "rather than submitting every case in the corpus for LLM grading.")
    return slugs[: args.limit] if args.limit else slugs


def existing_review(api, slug):
    """The newest review for `slug`, or None when the case has never been reviewed."""
    rows = api.reviews_for_slug(slug)
    return rows[0] if rows else None


def _describe(review):
    """`"review 1841 done PASS 84"` -- what a skip line says about the run it found."""
    bits = [f"review {review.get('id')}", str(review.get("status") or "?")]
    if review.get("disposition"):
        bits.append(str(review["disposition"]))
    if review.get("overall_score") is not None:
        bits.append(str(review["overall_score"]))
    return " ".join(bits)


def submit_batch(api, slugs, *, dry_run, force, logger, events_path, run_id):
    """POST each slug that has no review yet. Returns a status->count mapping.

    A 403 aborts the run: the role check fails identically on every remaining case,
    so one clear error beats several hundred. Any other HTTP failure is recorded and
    the batch continues -- a re-run skips whatever already landed.
    """
    stats = {"selected": len(slugs), "submitted": 0, "would_submit": 0,
             "already_reviewed": 0, "error": 0}

    def event(slug, status, detail="", level=logging.INFO):
        log_event(logger, events_path, run_id=run_id, stage=STAGE, slug=slug,
                  step="submit", status=status, detail=detail, level=level)

    for slug in slugs:
        if not force:
            found = existing_review(api, slug)
            if found is not None:
                stats["already_reviewed"] += 1
                event(slug, "already-reviewed", _describe(found))
                continue

        if dry_run:
            stats["would_submit"] += 1
            event(slug, "would-submit", json.dumps({"slug": slug}, ensure_ascii=False))
            continue

        started = time.monotonic()
        try:
            review = api.submit_review(slug)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise SystemExit(
                    f"HTTP 403 submitting {slug}: this token lacks the Caseworker "
                    "role, which every remaining case would fail on too. Nothing "
                    f"further was submitted ({stats['submitted']} landed).") from exc
            stats["error"] += 1
            event(slug, "error", f"HTTP {exc.code}", level=logging.WARNING)
            continue
        except Exception as exc:  # noqa: BLE001 - network, decode, anything else
            stats["error"] += 1
            event(slug, "error", f"{type(exc).__name__}: {exc}", level=logging.WARNING)
            continue

        stats["submitted"] += 1
        event(slug, "submitted", f"review {review.get('id')}")
        logger.debug("submitted %s in %dms", slug,
                     int((time.monotonic() - started) * 1000))
    return stats


def build_parser():
    p = argparse.ArgumentParser(
        description="Submit a batch of cases to the casework review queue, or report "
                    "the grades a submitted batch came back with.")
    add_common_args(p)
    p.add_argument(
        "--report", action="store_true",
        help="Read-only: print and write the grades for the batch instead of "
             "submitting it. Ignores --apply.")
    return p


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given."""
    if args.api_token:
        return CaseworkApi(
            args.api_base_url, token=args.api_token,
            allow_remote_writes=args.allow_remote_writes,
        )
    return CaseworkApi(
        args.api_base_url,
        basic=basic_auth_from_env(),
        allow_remote_writes=args.allow_remote_writes,
    )


def main(argv=None):
    args = build_parser().parse_args(argv)
    slugs = slugs_for_run(args)
    logger, run_id, paths = configure_run_logging(STAGE, verbose=args.verbose)
    api = build_api(args)

    log_run_header(logger, stage=STAGE, base_url=api.base_url,
                   dry_run=args.dry_run and not args.report,
                   provider="(none)", model="", n_selected=len(slugs),
                   run_id=run_id, paths=paths)

    started = time.monotonic()
    stats = submit_batch(api, slugs, dry_run=args.dry_run, force=args.force,
                         logger=logger, events_path=paths["events"], run_id=run_id)
    log_run_footer(logger, stage=STAGE, stats=stats,
                   duration_s=time.monotonic() - started)
    print(f"\n=== submit reviews ({'DRY RUN' if args.dry_run else 'APPLIED'}) ===")
    print(f"  {format_counts(stats)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
