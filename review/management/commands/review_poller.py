"""Jobs consumer (formerly the review-only poller).

This command is now a **generic consumer of the central job queue** (`jobs`
app). It claims jobs of one or more `kind`s over the platform job API, dispatches
each to the handler registered for that kind, and submits the result back. It no
longer knows anything review-specific beyond the `case_review` handler it
registers — the queue (claim/lease/retry/dedup) lives server-side.

The loop:

  1. POST /api/jobs/claim/  {kinds:[...]}  -> claim the next available job; get
                                             its payload (already enriched
                                             server-side, e.g. the resolved case
                                             dict). 204 when the queue is empty.
  2. run the registered HANDLER for job["kind"] LOCALLY (e.g. case_review runs
     review.runner.process_case: likhit conversion + per-source analysis +
     scoring). Nothing but the final result is sent back.
  3. POST /api/jobs/<id>/result/  -> submit the result (or the error).

Execution is non-distributed by default (one consumer, one job at a time), but
the server claim is atomic (select_for_update skip_locked), so this scales to
many consumers without protocol changes.

Auth: OIDC-only, unchanged. The consumer authenticates as a dedicated Zitadel
service account (Caseworker / ReviewAssistant role) via
`review.oidc_client_credentials`, presenting its access token as
`Authorization: Bearer <token>`. Configured via CASEWORK_OIDC_CLIENT_ID /
CASEWORK_OIDC_CLIENT_SECRET (+ OIDC_ISSUER).

By default the consumer is READ-ONLY: it lists the currently-queued jobs and
exits without claiming. Claiming and submitting results are gated behind
--apply so the safe action is the default.

  manage.py review_poller                         # READ-ONLY: list queued, exit
  manage.py review_poller --apply                 # claim/run/submit, poll forever
  manage.py review_poller --apply --once          # drain then exit
  manage.py review_poller --apply --kinds case_review material_convert
"""

import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from review.oidc_client_credentials import OIDCTokenError, get_provider
from review.job_handlers import HANDLERS as _REVIEW_HANDLERS
from materials.job_handlers import HANDLERS as _MATERIAL_HANDLERS
from case_proposals.job_handlers import HANDLERS as _PROPOSAL_HANDLERS

#: Worker-side handlers this consumer can run, aggregated from the owning apps
#: (review, materials, case_proposals). Each app keeps its own heavy deps in its
#: handler module; the poller just dispatches by kind. Add a new app's HANDLERS
#: here to make the consumer able to claim that kind.
#:
#: Note that --kinds defaults to ALL of these, so adding one here widens what a
#: deployment claims unless it passes --kinds explicitly. That is harmless for
#: case_proposal_intent — nothing enqueues one until the bus's proposal-builder
#: consumer is running — but it is worth knowing before adding the next.
HANDLERS = {**_REVIEW_HANDLERS, **_MATERIAL_HANDLERS, **_PROPOSAL_HANDLERS}


class PollerError(Exception):
    pass


def _capture_job_failure(exc, *, job_id, kind, err) -> bool:
    """Send one job failure to Sentry. Returns whether it was sent.

    Best-effort by construction. A telemetry problem must never turn a reported job
    failure into an unreported one, so every path returns rather than raises —
    including the import, since ``sentry_sdk`` is a real dependency here but this
    command also runs in environments that trim it.

    Grouped by ``kind`` rather than by traceback. The interesting question is "is
    ``case_proposal_intent`` failing?", not "how many distinct stack shapes did the
    JSON decoder produce?" — and an LLM failure's traceback varies with the prompt
    while the thing worth alerting on does not.
    """
    try:
        import sentry_sdk
    except Exception:  # noqa: BLE001 - telemetry must not break the worker
        return False

    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("job.kind", kind or "unknown")
            scope.set_tag("component", "review_poller")
            scope.set_context("job", {"id": job_id, "kind": kind})
            # The queue's own record of the failure, which is what an operator
            # would otherwise have to go to Postgres to read.
            scope.set_extra("job_error", err[:4000])
            scope.fingerprint = ["review_poller", "job_failed", kind or "unknown"]
            return bool(sentry_sdk.capture_exception(exc))
    except Exception:  # noqa: BLE001
        return False


class Command(BaseCommand):
    help = "Consume jobs from the central queue, run them locally, submit results."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Actually claim, run and submit jobs (mutates the queue: "
                "queued->running->done/failed). Without this flag the consumer "
                "is read-only and only lists the queued jobs."
            ),
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="With --apply: drain currently-available jobs then exit (no infinite poll).",
        )
        parser.add_argument(
            "--kinds",
            nargs="+",
            default=list(HANDLERS.keys()),
            help=(
                "Job kinds this consumer will claim. Defaults to every kind it "
                "has a registered handler for (currently: "
                f"{', '.join(sorted(HANDLERS))})."
            ),
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
            "Authorization": f"Bearer {self.token_provider.get_token()}",
            "Content-Type": "application/json",
        }

    def _jobs_url(self, path=""):
        return f"{settings.JOBS_API_BASE.rstrip('/')}{path}"

    def _post_jobs(self, path, payload, timeout=60):
        return requests.post(
            self._jobs_url(path), json=payload, headers=self._headers(), timeout=timeout
        )

    def _get_jobs(self, path, timeout=30):
        return requests.get(
            self._jobs_url(path), headers=self._headers(), timeout=timeout
        )

    def _post_casework(self, path, payload, timeout=60):
        """Side calls to the review-specific casework API (e.g. markdown attach)."""
        url = f"{settings.CASEWORK_API_BASE.rstrip('/')}{path}"
        return requests.post(
            url, json=payload, headers=self._headers(), timeout=timeout
        )

    # ---- read-only listing ------------------------------------------

    def _list_queued(self, kinds):
        """Read-only: GET the queue and report the queued jobs for our kinds.

        Does NOT claim anything (no queued->running transition), so it is safe to
        run against production just to see what is waiting.
        """
        r = self._get_jobs("/?status=queued&limit=1000")
        if r.status_code != 200:
            raise PollerError(f"list failed: HTTP {r.status_code} {r.text[:200]}")
        rows = r.json()
        mine = [row for row in rows if row.get("kind") in set(kinds)]
        self.stdout.write(
            f"queue: {len(mine)} queued job(s) for kinds={sorted(set(kinds))} "
            "(read-only; use --apply to process them)"
        )
        for row in mine:
            self.stdout.write(
                f"  queued job {row.get('id')} kind={row.get('kind')} "
                f"created={row.get('created_at')}"
            )
        return mine

    # ---- job lifecycle ----------------------------------------------

    def _claim(self, kinds):
        """Return a claimed job dict, or None if nothing is available."""
        r = self._post_jobs("/claim/", {"kinds": list(kinds)}, timeout=30)
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise PollerError(f"claim failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def _report_stage(self, job_id, stage):
        try:
            self._post_jobs(f"/{job_id}/stage/", {"stage": stage}, timeout=15)
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass

    def _submit(self, job_id, payload):
        r = self._post_jobs(f"/{job_id}/result/", payload, timeout=60)
        if r.status_code not in (200, 201):
            raise PollerError(
                f"result submit failed for {job_id}: HTTP {r.status_code} {r.text[:200]}"
            )

    def _process_job(self, job):
        job_id = job["id"]
        kind = job.get("kind")
        payload = job.get("payload") or {}
        self.stdout.write(f"  claimed job {job_id} kind={kind}")

        handler = HANDLERS.get(kind)
        if handler is None:
            # We claimed a kind we can't handle (shouldn't happen — we only claim
            # our --kinds). Report it as a failure so it isn't stuck RUNNING.
            self._submit(
                job_id, {"status": "failed", "error": f"no handler for kind {kind!r}"}
            )
            self.stderr.write(f"  job {job_id}: no handler for kind {kind!r}")
            return

        try:
            out = handler(
                payload,
                on_stage=lambda s: self._report_stage(job_id, s),
            )
            # The handler's ENTIRE return value is the job result — nest it
            # under "result" (the JobResultSerializer field the server stores
            # verbatim as job.result and hands to the kind's on_result hook).
            # Submitted flat, the serializer keeps only the body's inner
            # "result" key, and the case_review hook then reads
            # case_title/case_type off the wrong (scored) dict — where
            # case_type is a dict that overflows the varchar column.
            self._submit(
                job_id,
                {
                    "status": "done",
                    "result": out,
                    "duration_seconds": out.get("duration_seconds"),
                },
            )
            self.stdout.write(self.style.SUCCESS(f"  finished job {job_id}"))
        except Exception as e:  # noqa: BLE001 - report failure to the queue
            import traceback

            err = f"{e}\n{traceback.format_exc()[:2000]}"
            # Report to Sentry EXPLICITLY. This except block is why nothing else
            # would: the exception is caught here and handed to the queue, so it
            # never reaches the interpreter's excepthook, which is the only thing
            # sentry_sdk hooks for a management command. Every LLM job failure this
            # worker has ever had — including two rounds of misdiagnosed
            # `error_max_turns` — was therefore visible only in `job.error` and in
            # this pod's stdout, and stdout is 90 days of VictoriaLogs that nobody
            # alerts on.
            #
            # A no-op when no DSN is configured, so this is safe in dev and CI.
            _capture_job_failure(e, job_id=job_id, kind=kind, err=err)
            try:
                # Transient errors are retryable; the queue re-queues with backoff
                # up to the kind's max_attempts, else dead-letters.
                self._submit(
                    job_id, {"status": "failed", "error": err, "retryable": True}
                )
            except Exception as se:  # noqa: BLE001
                self.stderr.write(f"  could not report failure for {job_id}: {se}")
            self.stderr.write(f"  job {job_id} failed: {e}")

    # ---- main loop ---------------------------------------------------

    def handle(self, *args, **opts):
        apply = opts["apply"]
        once = opts["once"]
        poll = float(opts["poll"])
        kinds = opts["kinds"]

        unknown = [k for k in kinds if k not in HANDLERS]
        if unknown:
            raise PollerError(
                f"no handler registered for kind(s) {unknown}; "
                f"known kinds: {sorted(HANDLERS)}"
            )

        # Deprecation: a static CASEWORK_POLLER_TOKEN no longer works against the
        # OIDC-only API. Fail loudly if someone set ONLY the old token, so the
        # misconfiguration is obvious rather than surfacing as opaque 401s.
        if settings.CASEWORK_POLLER_TOKEN and not (
            settings.CASEWORK_OIDC_CLIENT_ID and settings.CASEWORK_OIDC_CLIENT_SECRET
        ):
            raise PollerError(
                "CASEWORK_POLLER_TOKEN is deprecated and no longer accepted: the "
                "API is OIDC-only. Configure the consumer's Zitadel service account "
                "via CASEWORK_OIDC_CLIENT_ID / CASEWORK_OIDC_CLIENT_SECRET "
                "(+ OIDC_ISSUER) for the client-credentials grant."
            )

        self.token_provider = get_provider()
        # Fail fast on missing/invalid credentials before entering the loop.
        try:
            self.token_provider.get_token()
        except OIDCTokenError as e:
            raise PollerError(str(e)) from e

        # Read-only by default: just report the queued jobs and exit.
        if not apply:
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"review_poller (read-only): jobs_api={settings.JOBS_API_BASE}"
                )
            )
            self._list_queued(kinds)
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"review_poller up: jobs_api={settings.JOBS_API_BASE} "
                f"kinds={sorted(set(kinds))} once={once}"
            )
        )

        while True:
            try:
                job = self._claim(kinds)
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
