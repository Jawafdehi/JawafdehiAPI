#!/usr/bin/env python
"""Standalone casework reviewer (DB-free script using the llm package).

This is the review "poller", moved out of the Django app to mirror the casework
enrichment scripts. It boots Django with the DB-free script settings (no
DATABASE_URL, no ORM) and talks ONLY over HTTP:

  1. POST /casework/jobs/claim/      -> claim the oldest pending review. The
                                        claim returns the case's basic details
                                        (case_id, slug, title/state/type) that
                                        jawafdehi-api identified at submit time.
  2. GET  /api/cases/<slug>/         -> fetch the FULL case content REMOTELY (the
                                        live case, never a local serialization).
  3. review.runner.process_case(...) -> likhit conversion + per-source analysis +
                                        rule scoring, all in this process.
  4. POST /casework/jobs/<id>/result/ -> submit the scored result (or the error).

Grading uses two tiers: gate rules -> REVIEW_LLM_PROVIDER_PREMIUM (e.g.
claude_cli), routine rules/narrative/source analysis -> REVIEW_LLM_PROVIDER_CHEAP
(e.g. codex_cli). Those env vars (and the CLI homes) come from the environment;
unlike the enrichers we do NOT collapse both tiers onto a single provider.

Auth:
  * Job API (claim/result):   CASEWORK_API_BASE + CASEWORK_POLLER_TOKEN.
  * Case content (GET cases): JAWAFDEHI_API_BASE_URL + JAWAFDEHI_API_TOKEN — the
    same client the enrichers use, which can read DRAFT cases.

Read-only by default (lists the pending queue and exits); --apply opts into the
mutating claim/score/submit loop.
"""

import argparse
import os
import sys
import time
import traceback

# Make `casework`, `review`, `llm`, `config` importable when run as a file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bootstrap():
    """Boot Django DB-free. Provider/token env must already be in os.environ
    (config.settings reads it at import), so set any --override BEFORE calling."""
    # Force the DB-free script settings: an inherited DJANGO_SETTINGS_MODULE
    # (e.g. config.settings from the shared API env) would otherwise boot the
    # ORM-backed settings and defeat the "reviewer never touches a DB" guarantee.
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings_scripts"
    import django

    django.setup()


class Reviewer:
    """Claim -> fetch remote case -> grade -> submit, over HTTP only."""

    def __init__(self, jobs, cases, runner, usage_cls, render_usage_table):
        self.jobs = jobs
        self.cases = cases
        self.runner = runner
        self.session_usage = usage_cls()
        self._render_usage_table = render_usage_table

    # ---- read-only listing ------------------------------------------

    def list_pending(self):
        r = self.jobs.get("/reviews/?page_size=1000")
        if r.status_code != 200:
            raise RuntimeError(f"list failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        rows = data.get("results", data) if isinstance(data, dict) else data
        pending = [row for row in rows if row.get("status") == "pending"]
        print(
            f"queue: {len(rows)} total, {len(pending)} pending "
            "(read-only; use --apply to process them)"
        )
        for row in pending:
            print(f"  pending review {row.get('id')} ({row.get('slug')})")
        return pending

    # ---- job lifecycle ----------------------------------------------

    def _claim(self):
        r = self.jobs.post("/jobs/claim/", {}, timeout=30)
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"claim failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def _report_stage(self, review_id, stage):
        try:
            self.jobs.post(f"/jobs/{review_id}/stage/", {"stage": stage}, timeout=15)
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass

    def _submit(self, review_id, payload):
        r = self.jobs.post(f"/jobs/{review_id}/result/", payload, timeout=60)
        if r.status_code not in (200, 201):
            raise RuntimeError(
                f"result submit failed for {review_id}: "
                f"HTTP {r.status_code} {r.text[:200]}"
            )

    def _process_job(self, job):
        review_id = job["review_id"]
        slug = job.get("slug")
        print(f"  claimed review {review_id} ({slug})")
        try:
            # Fetch the FULL case content remotely (the reviewer grades the live
            # case, not a local serialization). The claim only carried basics.
            self._report_stage(review_id, "fetching_case")
            case = self.cases.get_case(slug)
            case.setdefault("slug", slug)

            out = self.runner.process_case(
                case,
                job.get("config"),
                on_stage=lambda s: self._report_stage(review_id, s),
            )
            # Maintenance fix: populate MARKDOWN urls on sources we converted.
            self.jobs.attach_markdown(out.pop("markdown_to_attach", []))
            out["status"] = "done"
            self._submit(review_id, out)
            print(f"  finished review {review_id}")

            tu = (out.get("result") or {}).get("token_usage") or {}
            if tu.get("by_provider"):
                print(
                    self._render_usage_table(
                        tu["by_provider"], title=f"review {review_id} usage"
                    )
                )
                self.session_usage.merge_usage_dict(tu)
        except Exception as e:  # noqa: BLE001 - report failure to the API
            err = f"{e}\n{traceback.format_exc()[:2000]}"
            try:
                self._submit(review_id, {"status": "failed", "error": err})
            except Exception as se:  # noqa: BLE001
                print(
                    f"  could not report failure for {review_id}: {se}", file=sys.stderr
                )
            print(f"  review {review_id} failed: {e}", file=sys.stderr)

    # ---- main loop ---------------------------------------------------

    def run(self, *, apply, once, poll):
        if not apply:
            print("review_runner (read-only): listing pending queue")
            self.list_pending()
            return

        print(f"review_runner up (once={once})")
        while True:
            try:
                job = self._claim()
            except RuntimeError as e:
                print(f"claim error: {e}", file=sys.stderr)
                time.sleep(poll)
                continue
            if job is None:
                if once:
                    break
                time.sleep(poll)
                continue
            self._process_job(job)

        print("review_runner: queue drained.")
        print(
            self._render_usage_table(self.session_usage.totals(), title="session total")
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Claim, score and submit reviews (mutates the API). Without it the "
        "runner is read-only and only lists the pending queue.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="With --apply: drain currently-pending reviews then exit.",
    )
    parser.add_argument(
        "--poll", type=float, default=3.0, help="Seconds between polls when idle."
    )
    parser.add_argument(
        "--premium-provider",
        default=None,
        help="Override REVIEW_LLM_PROVIDER_PREMIUM (e.g. claude_cli).",
    )
    parser.add_argument(
        "--cheap-provider",
        default=None,
        help="Override REVIEW_LLM_PROVIDER_CHEAP (e.g. codex_cli).",
    )
    parser.add_argument(
        "--casework-api-base",
        default=None,
        help="Job API base, .../api/casework (CASEWORK_API_BASE).",
    )
    parser.add_argument(
        "--casework-token", default=None, help="Job API token (CASEWORK_POLLER_TOKEN)."
    )
    parser.add_argument(
        "--api-base-url",
        default=None,
        help="Case API base for fetching case content (JAWAFDEHI_API_BASE_URL).",
    )
    parser.add_argument(
        "--api-token", default=None, help="Case API token (JAWAFDEHI_API_TOKEN)."
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args()

    # Bridge --overrides onto env BEFORE bootstrap (config.settings reads env at
    # import). Provider vars default to the deployment's env when not overridden.
    if args.premium_provider:
        os.environ["REVIEW_LLM_PROVIDER_PREMIUM"] = args.premium_provider
    if args.cheap_provider:
        os.environ["REVIEW_LLM_PROVIDER_CHEAP"] = args.cheap_provider
    if args.casework_api_base:
        os.environ["CASEWORK_API_BASE"] = args.casework_api_base
    if args.casework_token:
        os.environ["CASEWORK_POLLER_TOKEN"] = args.casework_token

    _bootstrap()

    from casework.common import CaseworkApi, setup_logging
    from llm.usage import SessionUsage, render_usage_table
    from review import runner
    from review.upstream_client import UpstreamClient, UpstreamError

    setup_logging(args.verbose)

    try:
        jobs = UpstreamClient()
    except UpstreamError as e:
        print(f"job API client error: {e}", file=sys.stderr)
        raise SystemExit(1)
    cases = CaseworkApi(base_url=args.api_base_url, token=args.api_token)

    reviewer = Reviewer(jobs, cases, runner, SessionUsage, render_usage_table)
    reviewer.run(apply=args.apply, once=args.once, poll=args.poll)


if __name__ == "__main__":
    main()
