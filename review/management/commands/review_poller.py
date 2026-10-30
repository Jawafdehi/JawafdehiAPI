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

from review.oidc_client_credentials import (
    OIDCTokenError,
    OIDCTransportError,
    get_provider,
)
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


#: How long claims may keep failing before the consumer gives up and exits
#: non-zero. A transport failure must not be fatal on the first occurrence —
#: that was this command's bug — but it must not be *never* fatal either: a pod
#: spinning forever against a dead API is invisible, where one that exits is a
#: visible CrashLoopBackOff. Same reasoning as
#: ``case_events.consumers.runner.connect``.
#:
#: Expressed in SECONDS, not in a count of attempts, and that matters. A count
#: is silently a function of ``--poll``: ten attempts at the 3s default is 30
#: seconds of tolerance, which is shorter than the API outages this estate
#: actually has — a Keel deploy that ships before its migration runs produces
#: 10-14 minute windows of 500s, and four of those shipped in one day. A budget
#: that cannot ride out the known outage is a budget that turns a recoverable
#: blip into a failed run.
_FAILURE_BUDGET_SECONDS = 900.0

#: Floor and ceiling for the exponential backoff between failed claims, so a
#: `--poll 0` consumer cannot hot-spin and a long outage does not sleep past
#: the budget it is being measured against.
_MIN_FAILURE_SLEEP_SECONDS = 1.0
_MAX_FAILURE_SLEEP_SECONDS = 60.0


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

    def _request(self, method, url, *, timeout, **kwargs):
        """One HTTP call to the platform, with transport failures typed.

        Every call out of this command goes through here so that a reset
        connection, a read timeout or an unreachable issuer surfaces as a
        ``PollerError`` — which the caller and the main loop already know how to
        absorb — instead of as a bare ``requests`` exception escaping to the
        interpreter and taking the whole run with it.

        That escape was real: ``_claim`` is called inside ``except PollerError``,
        which never matched ``requests.exceptions.ConnectionError``, so one
        ``ConnectionResetError(104)`` from the API ended the run.

        Deliberately does NOT retry. ``requests.RequestException`` includes
        ``ReadTimeout``, which fires *after* the server has acted, and every POST
        here is non-idempotent: a retried ``/claim/`` claims a second job while
        the first stays RUNNING and unworked until its lease expires, and a
        retried ``/{id}/result/`` submits the result twice. Retrying is also
        redundant — the poll loop re-claims on the next tick, and the queue's
        lease/retry model is the real safety net. One attempt, typed clearly.
        """
        try:
            return requests.request(
                method, url, headers=self._headers(), timeout=timeout, **kwargs
            )
        except (requests.RequestException, OIDCTransportError) as exc:
            raise PollerError(f"{method} {url} failed: {exc}") from exc

    @staticmethod
    def _json(response):
        """Decode a response body, as a ``PollerError`` on failure.

        ``requests.exceptions.JSONDecodeError`` is itself a ``RequestException``,
        so calling ``.json()`` at the call site re-opened the very hole
        ``_request`` exists to close: an ingress answering 200 with an HTML
        error page would raise past the loop's ``except PollerError`` and end
        the run.
        """
        try:
            return response.json()
        except ValueError as exc:  # JSONDecodeError subclasses ValueError
            body = (response.text or "")[:200]
            raise PollerError(
                f"{response.request.method if response.request else 'request'} "
                f"{response.url} returned HTTP {response.status_code} with a "
                f"non-JSON body: {body}"
            ) from exc

    def _post_jobs(self, path, payload, timeout=60):
        return self._request(
            "POST", self._jobs_url(path), json=payload, timeout=timeout
        )

    def _get_jobs(self, path, timeout=30):
        return self._request("GET", self._jobs_url(path), timeout=timeout)

    def _post_casework(self, path, payload, timeout=60):
        """Side calls to the review-specific casework API (e.g. markdown attach)."""
        url = f"{settings.CASEWORK_API_BASE.rstrip('/')}{path}"
        return self._request("POST", url, json=payload, timeout=timeout)

    # ---- read-only listing ------------------------------------------

    def _list_queued(self, kinds):
        """Read-only: GET the queue and report the queued jobs for our kinds.

        Does NOT claim anything (no queued->running transition), so it is safe to
        run against production just to see what is waiting.
        """
        r = self._get_jobs("/?status=queued&limit=1000")
        if r.status_code != 200:
            raise PollerError(f"list failed: HTTP {r.status_code} {r.text[:200]}")
        rows = self._json(r)
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
        return self._json(r)

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
            #
            # Guarded because `_submit` now raises `PollerError` on a transport
            # failure, and this call sits outside `_process_job`'s try. An
            # unguarded reset here would propagate past the loop's
            # `except PollerError` (which wraps only `_claim`) and end the run —
            # the exact escape the budget exists to bound. The job stays
            # RUNNING until its lease expires, which the queue already handles.
            try:
                self._submit(
                    job_id,
                    {"status": "failed", "error": f"no handler for kind {kind!r}"},
                )
            except PollerError as e:
                self.stderr.write(f"  could not report unknown kind for {job_id}: {e}")
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
        # Fail fast on missing/invalid credentials before entering the loop —
        # but ONLY on those. A transport failure here (read timeout against the
        # issuer, DNS blip, a 502 from its proxy) says nothing about whether the
        # credential is good, and treating it as fatal is what used to kill an
        # entire CronJob run over a few seconds of network weather. The loop
        # below already tolerates a failed claim and retries, and `_headers()`
        # re-requests the token on every call, so continuing is safe: either the
        # blip clears and the run proceeds, or every claim fails and the run
        # ends up exactly where it would have anyway — just noisily, and after
        # having given the issuer a chance to come back.
        try:
            self.token_provider.get_token()
        except OIDCTransportError as e:
            self.stderr.write(
                f"warning: could not reach the OIDC token endpoint at startup ({e}); "
                "continuing — the poll loop will retry."
            )
        except OIDCTokenError as e:
            # Configuration errors AND the bare base class. Catching only the
            # two subclasses let a plain `OIDCTokenError` — still public, still
            # raised by name elsewhere in the tree — escape as a raw traceback
            # instead of the clean PollerError this command contracts to raise.
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

        failing_since = None
        consecutive_failures = 0
        while True:
            try:
                job = self._claim(kinds)
            except PollerError as e:
                # Absorbing this is the point — one blip must not end the run,
                # and that applies to `--once` too: a CronJob drain that dies on
                # the first reset connection is the exact production failure
                # this command had. `--once` means "drain what is there and
                # exit", not "exit at the first sign of weather".
                #
                # It must still terminate, though, so the same wall-clock budget
                # bounds both shapes. A pod that keeps retrying past it exits
                # non-zero, which is visible; one that spins forever is not.
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                self.stderr.write(f"claim error: {e}")
                elapsed = now - failing_since
                if elapsed >= _FAILURE_BUDGET_SECONDS:
                    raise PollerError(
                        f"giving up after {elapsed:.0f}s of failed claims; "
                        f"last error: {e}"
                    ) from e
                # Back off while it is down. A flat `poll` sleep means ~300
                # claims per pod across a 900s budget — against a service that
                # is already failing, and each one costs a token fetch too.
                # With `--poll 0` it would be a hot spin for the whole window.
                consecutive_failures += 1
                base = max(poll, _MIN_FAILURE_SLEEP_SECONDS)
                # The ceiling must never be SHORTER than the configured poll
                # interval: with `--poll 300`, clamping to 60 would make a
                # failing API polled five times MORE often than a healthy one,
                # each attempt also fetching a token.
                ceiling = max(poll, _MAX_FAILURE_SLEEP_SECONDS)
                time.sleep(min(base * (2 ** min(consecutive_failures - 1, 6)), ceiling))
                continue

            failing_since = None
            consecutive_failures = 0

            if job is None:
                if once:
                    break
                time.sleep(poll)
                continue

            self._process_job(job)

        self.stdout.write(self.style.SUCCESS("review_poller: queue drained."))
