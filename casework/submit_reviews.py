"""Submit a batch of cases into the casework review queue, and read the grades back.

Two modes. The default POSTs each slug to `/api/casework/reviews/submit/`; `--report`
reads back what the grading produced and writes nothing. They are one module because
they share the batch selection and the review-row shape.

The script reads no case. A slug is all the POST needs, and the review list row already
carries the status, score and disposition the report shows -- so a 238-case batch costs
2 requests per case to submit and 1 to report, with no corpus listing in front of
either. Only a failed row costs a third request, for the `error` the list row omits.
"""

import argparse
import json
import logging
import os
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
from casework.common.review import review_path
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


#: Statuses that mean the credential itself is wrong, not this one case. 401 is an
#: expired, malformed or wrong-audience token -- `OIDCAuthentication` raises
#: `AuthenticationFailed` and supplies `authenticate_header`, so DRF answers 401, not
#: 403. 403 is a valid token without the Caseworker role. Counting either per-case
#: turns one stale token into several hundred logged errors and a zero exit code.
CREDENTIAL_STATUSES = (401, 403)


def _raise_if_credential_failure(exc, slug, note=""):
    """Turn a 401/403 into a run-ending SystemExit. Any other status returns."""
    if exc.code not in CREDENTIAL_STATUSES:
        return
    raise SystemExit(
        f"HTTP {exc.code} on {slug}: the API rejected this credential (401 = expired "
        "or invalid token, 403 = valid token without the Caseworker role). Every "
        f"remaining case would fail the same way, so the run stopped here.{note}"
    ) from exc


def _warn_on_slug_drift(logger, requested, review):
    """Warn when the review came back filed under a different slug than we asked for.

    The two sides resolve slugs differently: `SubmitSerializer` falls back to
    `CaseSlugHistory` for a retired slug, while the list endpoint filters on
    `case__slug` and sees live slugs only. So a stale batch row submits fine but is
    invisible to the next run's skip check, and the case is re-graded at full LLM
    cost on every run. One warning naming both slugs is what makes that visible.
    """
    landed = (review.get("slug") or "").strip()
    if landed and landed != requested:
        logger.warning(
            "%s is a retired slug -- the review was filed under %s. The skip check "
            "reads live slugs only, so this case will be re-submitted on every run "
            "until the batch CSV is updated.", requested, landed)


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

    Two classes of failure abort the whole run instead of being counted: a credential
    rejection (`CREDENTIAL_STATUSES`) and the write-guard's `RuntimeError`. Both fail
    identically on every remaining case, so counting them per-case would turn one
    configuration mistake into several hundred logged errors and a zero exit code.
    Every other failure -- on the pre-check read as much as on the POST -- is recorded
    and the batch continues; a re-run skips whatever already landed.
    """
    stats = {"selected": len(slugs), "submitted": 0, "would_submit": 0,
             "already_reviewed": 0, "error": 0}

    def event(slug, status, detail="", level=logging.INFO):
        log_event(logger, events_path, run_id=run_id, stage=STAGE, slug=slug,
                  step="submit", status=status, detail=detail, level=level)

    for slug in slugs:
        if not force:
            # The pre-check read is half of this run's requests. A blip on one of
            # them must cost that case, not the batch -- the same rule the POST
            # below follows, and the one `bind_materials` follows on its case read.
            try:
                found = existing_review(api, slug)
            except urllib.error.HTTPError as exc:
                _raise_if_credential_failure(
                    exc, slug, f" {stats['submitted']} case(s) were submitted first.")
                stats["error"] += 1
                event(slug, "error", f"HTTP {exc.code} reading reviews",
                      level=logging.WARNING)
                continue
            except Exception as exc:  # noqa: BLE001 - network, decode, anything else
                stats["error"] += 1
                event(slug, "error", f"{type(exc).__name__} reading reviews: {exc}",
                      level=logging.WARNING)
                continue
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
            _raise_if_credential_failure(
                exc, slug, f" {stats['submitted']} case(s) were submitted first.")
            stats["error"] += 1
            event(slug, "error", f"HTTP {exc.code}", level=logging.WARNING)
            continue
        except RuntimeError:
            # The write-guard, raised before any socket opens. Not a per-case
            # failure: every remaining slug would raise it too.
            raise
        except Exception as exc:  # noqa: BLE001 - network, decode, anything else
            stats["error"] += 1
            event(slug, "error", f"{type(exc).__name__}: {exc}", level=logging.WARNING)
            continue

        stats["submitted"] += 1
        event(slug, "submitted", f"review {review.get('id')}")
        _warn_on_slug_drift(logger, slug, review)
        logger.debug("submitted %s in %dms", slug,
                     int((time.monotonic() - started) * 1000))
    return stats


def _row(slug, status, **kw):
    """A report row with every column present, so the renderer never key-errors."""
    return {"slug": slug, "review_id": None, "status": status, "score": None,
            "disposition": None, "duration": None, "error": "", **kw}


def report_rows(api, slugs):
    """One row per batch slug: what the review queue did with it.

    Everything but `error` comes off the list row. `error` lives only on the detail
    serializer, so it is fetched for failed rows and nothing else.

    A read that fails becomes an `unreadable` row rather than ending the report. This
    pass runs over a batch that took hours to grade, so losing the whole file to one
    blip on case 200 of 238 is the expensive outcome. A credential rejection still
    aborts -- every remaining read would fail the same way.
    """
    rows = []
    for slug in slugs:
        try:
            review = existing_review(api, slug)
        except urllib.error.HTTPError as exc:
            _raise_if_credential_failure(exc, slug)
            rows.append(_row(slug, "unreadable", error=f"HTTP {exc.code}"))
            continue
        except Exception as exc:  # noqa: BLE001 - network, decode, anything else
            rows.append(_row(slug, "unreadable", error=f"{type(exc).__name__}: {exc}"))
            continue

        if review is None:
            rows.append(_row(slug, "never-submitted"))
            continue

        error = ""
        if review.get("status") == "failed":
            try:
                detail = api.review_detail(review["id"]) or {}
            except Exception as exc:  # noqa: BLE001 - the error line is a nicety
                detail = {"error": f"({type(exc).__name__} reading the detail)"}
            first_line = (detail.get("error") or "").strip().splitlines()
            error = first_line[0] if first_line else ""
        rows.append(_row(
            slug,
            review.get("status") or "?",
            review_id=review.get("id"),
            score=review.get("overall_score"),
            disposition=review.get("disposition"),
            duration=review.get("duration_seconds"),
            error=error,
        ))
    return rows


def summarize(rows):
    """Status counts, disposition counts, score spread, and the never-submitted."""
    statuses, dispositions, scores, never = {}, {}, [], []
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        if row["status"] == "never-submitted":
            never.append(row["slug"])
        if row.get("disposition"):
            dispositions[row["disposition"]] = dispositions.get(row["disposition"], 0) + 1
        if isinstance(row.get("score"), (int, float)):
            scores.append(row["score"])
    return {
        "statuses": statuses,
        "dispositions": dispositions,
        "scored": len(scores),
        "avg": round(sum(scores) / len(scores), 1) if scores else None,
        "min": min(scores) if scores else None,
        "max": max(scores) if scores else None,
        "never_submitted": never,
    }


def _counts_table(title, counts):
    lines = [f"| {title} | Cases |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(counts.items())]
    return lines + [""]


def render_report(rows, summary, *, base_url, run_id, batch):
    """The markdown the report run writes. Read top-down: totals, then cases."""
    lines = [
        f"# Review batch report — `{batch}`",
        "",
        f"- Target: `{base_url}`",
        f"- Run id: `{run_id}`",
        f"- Cases in batch: {len(rows)}",
        "",
        "## Summary",
        "",
    ]
    lines += _counts_table("Status", summary["statuses"])
    lines += _counts_table("Disposition", summary["dispositions"])
    if summary["avg"] is not None:
        lines += [f"Score: avg **{summary['avg']}**, min {summary['min']}, "
                  f"max {summary['max']} (over {summary['scored']} graded).", ""]

    lines += ["## Cases", "",
              "| # | Slug | Review | Status | Score | Disposition | Seconds |",
              "|---|---|---|---|---|---|---|"]
    for i, row in enumerate(rows, 1):
        duration = (f"{row['duration']:.0f}"
                    if isinstance(row.get("duration"), (int, float)) else "—")
        lines.append(
            f"| {i} | `{row['slug']}` | {row['review_id'] or '—'} | {row['status']} "
            f"| {row['score'] if row['score'] is not None else '—'} "
            f"| {row['disposition'] or '—'} | {duration} |")
    lines.append("")

    problems = [r for r in rows if r["status"] in ("failed", "unreadable")]
    if problems:
        lines += ["## Failed and unreadable", "",
                  "Re-submit these on their own — `--slug <a> --slug <b> --force` — "
                  "rather than re-running the batch with `--force`, which re-grades "
                  "every passing case too.", ""]
        for r in problems:
            named = f"review {r['review_id']}" if r["review_id"] else "no review read"
            lines.append(f"- `{r['slug']}` — {named} — "
                         f"{r['error'] or '(no error recorded)'}")
        lines.append("")

    if summary["never_submitted"]:
        lines += ["## Never submitted", "",
                  "These batch slugs carry no review at all. Re-run without "
                  "`--report` to submit them.", ""]
        lines += [f"- `{slug}`" for slug in summary["never_submitted"]]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


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

    # `--report` writes nothing, so the header must never say APPLY on one -- the
    # `.log` file is the record of what a run was allowed to do.
    log_run_header(logger, stage=STAGE, base_url=api.base_url,
                   dry_run=args.dry_run or args.report,
                   provider="(none)", model="", n_selected=len(slugs),
                   run_id=run_id, paths=paths)

    started = time.monotonic()
    if args.report:
        rows = report_rows(api, slugs)
        summary = summarize(rows)
        path = review_path(STAGE, run_id, args.review_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_report(rows, summary, base_url=api.base_url, run_id=run_id,
                          batch=os.path.basename(args.batch_csv or "(--slug)")),
            encoding="utf-8")
        log_run_footer(logger, stage=STAGE, stats=summary["statuses"],
                       duration_s=time.monotonic() - started)
        print("\n=== review batch report (READ-ONLY) ===")
        print(f"  {len(rows)} cases — {format_counts(summary['statuses'])}")
        if summary["dispositions"]:
            print(f"  {format_counts(summary['dispositions'])}   "
                  f"score avg {summary['avg']} "
                  f"(min {summary['min']}, max {summary['max']})")
        print(f"  Wrote {path}")
        return 0

    stats = submit_batch(api, slugs, dry_run=args.dry_run, force=args.force,
                         logger=logger, events_path=paths["events"], run_id=run_id)
    log_run_footer(logger, stage=STAGE, stats=stats,
                   duration_s=time.monotonic() - started)
    print(f"\n=== submit reviews ({'DRY RUN' if args.dry_run else 'APPLIED'}) ===")
    print(f"  {format_counts(stats)}")
    # Exit non-zero when any case failed. This DIVERGES from the sibling enrichers,
    # which always return 0: a wrapper cannot otherwise tell a clean batch from one
    # where every POST 500'd, and the abort paths above exist precisely to stop a
    # configuration mistake exiting 0. Skipped and would-submit cases are not errors.
    return 1 if stats["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
