"""API-driven review poller (no DB access).

This is the new modality: the poller talks ONLY to the casework HTTP API and
never reads or writes the CaseReview / ReviewConfig tables. The loop is:

  1. POST /jobs/claim/      -> claim the oldest pending review; get the case dict
                               + config (204 when the queue is empty).
  2. process the case LOCALLY (review.runner.process_case): likhit conversion,
     per-source analysis and rule scoring all happen here on the poller. The
     converted markdown is NOT uploaded — only the final scored result.
  3. POST /jobs/<id>/result/ -> submit the result (or the error).

Execution is assumed NON-distributed for now: a single poller, one job at a
time. The server's claim endpoint is still atomic (select_for_update), so this
is safe to scale to multiple pollers later without changing the protocol.

Auth: the poller authenticates with a long-lived DRF auth token (NOT a
username/password login) sent as `Authorization: Token <key>`. The token is
configured via CASEWORK_POLLER_TOKEN and belongs to a dedicated service account
with the Contributor (or ReviewAssistant) role. Create one with:
  manage.py drf_create_token <service-account-username>

By default the poller is READ-ONLY: it lists the currently-pending reviews and
exits without touching them. Claiming a review (pending->running) and submitting
its result are the only mutating operations, so they are gated behind --apply.
This makes the safe action the default and forces an explicit opt-in before the
poller writes anything back to the (possibly production) API.

  manage.py review_poller                    # READ-ONLY: list pending, exit
  manage.py review_poller --apply            # claim/score/submit, poll forever
  manage.py review_poller --apply --once     # claim/score/submit, drain then exit
"""

import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from review import runner


class PollerError(Exception):
    pass


class Command(BaseCommand):
    help = "Poll the casework API for pending reviews, process them locally, submit results."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Actually claim, score and submit reviews (mutates the API: "
                "pending->running->done/failed). Without this flag the poller is "
                "read-only and only lists the pending queue."
            ),
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="With --apply: drain currently-pending reviews then exit (no infinite poll).",
        )
        parser.add_argument(
            "--poll",
            type=float,
            default=3.0,
            help="Seconds between polls when the queue is empty.",
        )

    # ---- API helpers -------------------------------------------------

    def _headers(self):
        return {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }

    def _post(self, path, payload, timeout=60):
        url = f"{settings.CASEWORK_API_BASE.rstrip('/')}{path}"
        return requests.post(
            url, json=payload, headers=self._headers(), timeout=timeout
        )

    def _get(self, path, timeout=30):
        url = f"{settings.CASEWORK_API_BASE.rstrip('/')}{path}"
        return requests.get(url, headers=self._headers(), timeout=timeout)

    # ---- read-only listing ------------------------------------------

    def _list_pending(self):
        """Read-only: GET the review queue and report the pending reviews.

        Does NOT claim anything (no pending->running transition), so it is safe
        to run against production just to see what is waiting.
        """
        r = self._get("/reviews/?page_size=1000")
        if r.status_code != 200:
            raise PollerError(f"list failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        rows = data.get("results", data) if isinstance(data, dict) else data
        pending = [row for row in rows if row.get("status") == "pending"]
        self.stdout.write(
            f"queue: {len(rows)} total, {len(pending)} pending "
            "(read-only; use --apply to process them)"
        )
        for row in pending:
            self.stdout.write(
                f"  pending review {row.get('id')} ({row.get('slug')}) "
                f"created={row.get('created_at')}"
            )
        return pending

    # ---- job lifecycle ----------------------------------------------

    def _claim(self):
        """Return a job payload dict, or None if the queue is empty."""
        r = self._post("/jobs/claim/", {}, timeout=30)
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise PollerError(f"claim failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def _report_stage(self, review_id, stage):
        try:
            self._post(f"/jobs/{review_id}/stage/", {"stage": stage}, timeout=15)
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass

    def _submit(self, review_id, payload):
        r = self._post(f"/jobs/{review_id}/result/", payload, timeout=60)
        if r.status_code not in (200, 201):
            raise PollerError(
                f"result submit failed for {review_id}: HTTP {r.status_code} {r.text[:200]}"
            )

    def _attach_markdown(self, items):
        """Maintenance fix: attach locally-converted markdown back to sources."""
        for item in items or []:
            sid = item.get("source_id")
            try:
                r = self._post(
                    f"/sources/{sid}/markdown/",
                    {"markdown": item.get("markdown", "")},
                    timeout=60,
                )
                if r.status_code == 200:
                    body = r.json()
                    if body.get("created"):
                        self.stdout.write(f"    attached MARKDOWN url to source {sid}")
                else:
                    self.stderr.write(
                        f"    markdown attach failed for {sid}: HTTP {r.status_code} {r.text[:150]}"
                    )
            except Exception as e:  # noqa: BLE001 - maintenance is best-effort
                self.stderr.write(f"    markdown attach error for {sid}: {e}")

    def _process_job(self, job):
        review_id = job["review_id"]
        self.stdout.write(f"  claimed review {review_id} ({job.get('slug')})")
        try:
            out = runner.process_case(
                job["case"],
                job.get("config"),
                on_stage=lambda s: self._report_stage(review_id, s),
            )
            # Maintenance fix: populate MARKDOWN urls on sources we converted.
            self._attach_markdown(out.pop("markdown_to_attach", []))
            out["status"] = "done"
            self._submit(review_id, out)
            self.stdout.write(self.style.SUCCESS(f"  finished review {review_id}"))
        except Exception as e:  # noqa: BLE001 - report failure to the API
            import traceback

            err = f"{e}\n{traceback.format_exc()[:2000]}"
            try:
                self._submit(review_id, {"status": "failed", "error": err})
            except Exception as se:  # noqa: BLE001
                self.stderr.write(f"  could not report failure for {review_id}: {se}")
            self.stderr.write(f"  review {review_id} failed: {e}")

    # ---- main loop ---------------------------------------------------

    def handle(self, *args, **opts):
        apply = opts["apply"]
        once = opts["once"]
        poll = float(opts["poll"])
        self.token = settings.CASEWORK_POLLER_TOKEN
        if not self.token:
            raise PollerError(
                "CASEWORK_POLLER_TOKEN is not set. Create a DRF token for the "
                "poller's service account (manage.py drf_create_token <user>) "
                "and set it in the environment."
            )

        # Read-only by default: just report the pending queue and exit. Claiming
        # and submitting results (the mutating path) require an explicit --apply.
        if not apply:
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"review_poller (read-only): api={settings.CASEWORK_API_BASE}"
                )
            )
            self._list_pending()
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"review_poller up: api={settings.CASEWORK_API_BASE} once={once}"
            )
        )

        while True:
            try:
                job = self._claim()
            except PollerError as e:
                self.stderr.write(f"claim error: {e}")
                time.sleep(poll)
                continue

            if job is None:
                if once:
                    break
                time.sleep(poll)
                continue

            self._process_job(job)

        self.stdout.write(self.style.SUCCESS("review_poller: queue drained."))
