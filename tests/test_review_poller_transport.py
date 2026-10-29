# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
"""Transport failures must not end a poller run.

Two separate escapes, both seen in production over 30 days:

1. ``handle()`` called ``get_token()`` before the loop and turned *any*
   ``OIDCTokenError`` into a fatal ``PollerError``, under a comment about
   missing/invalid credentials. A read timeout against the issuer is neither,
   and it killed whole CronJob runs while auth.jawafdehi.org was serving
   thousands of requests with zero errors.

2. ``_claim`` is called inside ``except PollerError``, which never matched
   ``requests.exceptions.ConnectionError``. One ``ConnectionResetError(104)``
   from the jobs API therefore propagated out of the loop and ended the run.

Both are about the same thing: telling "this will never work" apart from "this
did not work just now".
"""

import itertools
from unittest import mock

import pytest
import requests

from review import oidc_client_credentials as cc
from review.management.commands import review_poller as rp


@pytest.fixture
def command(monkeypatch):
    """A Command with a token provider that hands out a token for free."""
    cmd = rp.Command()
    cmd.token_provider = mock.Mock()
    cmd.token_provider.get_token.return_value = "tok"
    monkeypatch.setattr(rp.settings, "JOBS_API_BASE", "https://api.test/api/jobs", raising=False)
    monkeypatch.setattr(
        rp.settings, "CASEWORK_API_BASE", "https://api.test/api/casework", raising=False
    )
    return cmd


class TestOutboundCallsSurviveABlip:
    def test_a_reset_connection_becomes_a_poller_error(self, command):
        """Not a bare requests exception — the loop only catches PollerError."""
        with mock.patch.object(
            rp.requests,
            "request",
            side_effect=requests.ConnectionError(
                "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
            ),
        ):
            with pytest.raises(rp.PollerError) as exc:
                command._claim(["case_review"])
        assert "ConnectionResetError" in str(exc.value)

    def test_a_non_idempotent_post_is_never_retried(self, command):
        """A retried /claim/ claims a SECOND job.

        `requests.RequestException` includes `ReadTimeout`, which fires after
        the server has already acted. Retrying it leaves job A RUNNING and
        unworked until its lease expires while the consumer works job B — and
        a retried `/{id}/result/` submits the same result twice. The poll loop
        re-claims on the next tick anyway, so the retry bought nothing.
        """
        with mock.patch.object(
            rp.requests, "request", side_effect=requests.ReadTimeout("read timeout")
        ) as req:
            with pytest.raises(rp.PollerError):
                command._claim(["case_review"])
        assert req.call_count == 1

    def test_a_result_submission_is_never_retried_either(self, command):
        with mock.patch.object(
            rp.requests, "request", side_effect=requests.ReadTimeout("read timeout")
        ) as req:
            with pytest.raises(rp.PollerError):
                command._submit(7, {"status": "done"})
        assert req.call_count == 1

    def test_an_empty_queue_still_reads_as_empty(self, command):
        """204 is the hot path; the retry wrapper must not disturb it."""
        with mock.patch.object(
            rp.requests, "request", return_value=mock.Mock(status_code=204)
        ):
            assert command._claim(["case_review"]) is None

    def test_a_transport_failure_fetching_the_token_is_retried_too(
        self, command
    ):
        """`_headers()` calls the issuer, so it can fail the same way."""
        command.token_provider.get_token.side_effect = cc.OIDCTransportError("read timeout")
        with mock.patch.object(rp.requests, "request") as req:
            with pytest.raises(rp.PollerError):
                command._claim(["case_review"])
        assert req.call_count == 0

    def test_a_bad_credential_is_not_retried_and_stays_fatal(self, command):
        """A configuration error must not be absorbed into the loop's retry."""
        command.token_provider.get_token.side_effect = cc.OIDCConfigurationError(
            "invalid_client"
        )
        with pytest.raises(cc.OIDCConfigurationError):
            command._claim(["case_review"])

    def test_every_outbound_helper_goes_through_the_retry(self, command):
        """A helper added later that calls requests directly would regress this."""
        with mock.patch.object(
            rp.requests, "request", return_value=mock.Mock(status_code=200, json=dict)
        ) as req:
            command._get_jobs("/?status=queued")
            command._post_jobs("/claim/", {"kinds": []})
            command._post_casework("/attach/", {})
        assert req.call_count == 3
        assert [c.args[0] for c in req.call_args_list] == ["GET", "POST", "POST"]


class TestStartupTokenCheck:
    """`handle()`'s pre-loop fail-fast, which is where the CronJob died."""

    def _run(self, cmd, **opts):
        base = {
            "apply": False,
            "once": True,
            "poll": 0.0,
            "kinds": ["case_review"],
        }
        base.update(opts)
        return cmd.handle(**base)

    def test_a_transport_failure_does_not_abort_the_run(self, monkeypatch, command):
        provider = mock.Mock()
        provider.get_token.side_effect = cc.OIDCTransportError(
            "OIDC token request to https://auth.test/oauth/v2/token failed: Read timed out"
        )
        monkeypatch.setattr(rp, "get_provider", lambda: provider)
        monkeypatch.setattr(rp.Command, "_list_queued", lambda self, kinds: [])
        monkeypatch.setattr(rp.settings, "CASEWORK_POLLER_TOKEN", "", raising=False)

        cmd = rp.Command()
        self._run(cmd)  # must not raise

    def test_the_transport_failure_is_still_reported(self, monkeypatch, capsys):
        provider = mock.Mock()
        provider.get_token.side_effect = cc.OIDCTransportError("Read timed out")
        monkeypatch.setattr(rp, "get_provider", lambda: provider)
        monkeypatch.setattr(rp.Command, "_list_queued", lambda self, kinds: [])
        monkeypatch.setattr(rp.settings, "CASEWORK_POLLER_TOKEN", "", raising=False)

        cmd = rp.Command()
        self._run(cmd)
        assert "could not reach the OIDC token endpoint" in capsys.readouterr().err

    def test_a_bare_base_class_error_still_aborts_cleanly(self, monkeypatch):
        """`OIDCTokenError` is still public and still raised by name elsewhere.

        Catching only the two subclasses let it escape as a raw traceback
        instead of the PollerError this command contracts to raise.
        """
        provider = mock.Mock()
        provider.get_token.side_effect = cc.OIDCTokenError("something unclassified")
        monkeypatch.setattr(rp, "get_provider", lambda: provider)
        monkeypatch.setattr(rp.settings, "CASEWORK_POLLER_TOKEN", "", raising=False)

        cmd = rp.Command()
        with pytest.raises(rp.PollerError):
            self._run(cmd)

    def test_a_bad_credential_still_aborts_the_run(self, monkeypatch):
        """The guard's original purpose, which must survive the split."""
        provider = mock.Mock()
        provider.get_token.side_effect = cc.OIDCConfigurationError(
            "OIDC token request returned HTTP 401: invalid_client"
        )
        monkeypatch.setattr(rp, "get_provider", lambda: provider)
        monkeypatch.setattr(rp.settings, "CASEWORK_POLLER_TOKEN", "", raising=False)

        cmd = rp.Command()
        with pytest.raises(rp.PollerError) as exc:
            self._run(cmd)
        assert "invalid_client" in str(exc.value)

    def test_unconfigured_credentials_still_abort_the_run(self, monkeypatch):
        provider = mock.Mock()
        provider.get_token.side_effect = cc.OIDCConfigurationError("not configured")
        monkeypatch.setattr(rp, "get_provider", lambda: provider)
        monkeypatch.setattr(rp.settings, "CASEWORK_POLLER_TOKEN", "", raising=False)

        cmd = rp.Command()
        with pytest.raises(rp.PollerError):
            self._run(cmd)


class TestTheRunStillEnds:
    """Absorbing a transport failure must not mean never giving up — and must
    not mean giving up on the first strike either.

    Round one of this PR converted `requests.ConnectionError` into
    `PollerError`, which the loop's `except PollerError: sleep; continue`
    caught — and that handler never consulted `once`, so `--apply --once`
    against a dead API spun forever. Round one's fix made `once` re-raise
    immediately, which re-created the ORIGINAL production bug for the CronJob
    drain shape: one `ConnectionResetError(104)` ends the run.

    Both shapes now share one wall-clock budget: blips are absorbed, and the
    run always terminates.
    """

    @staticmethod
    def _run(monkeypatch, *, once, claim):
        provider = mock.Mock()
        provider.get_token.return_value = "tok"
        monkeypatch.setattr(rp, "get_provider", lambda: provider)
        monkeypatch.setattr(rp.settings, "CASEWORK_POLLER_TOKEN", "", raising=False)
        monkeypatch.setattr(rp.time, "sleep", lambda _s: None)
        monkeypatch.setattr(rp.Command, "_claim", claim)
        return rp.Command().handle(
            apply=True, once=once, poll=0.0, kinds=["case_review"]
        )

    def test_once_absorbs_a_blip_instead_of_dying_on_it(self, monkeypatch):
        """The original production bug, for the documented CronJob drain mode."""
        calls = []

        def flaky(self, kinds):
            calls.append(1)
            if len(calls) == 1:
                raise rp.PollerError("connection reset by peer")
            return None  # queue empty -> `once` exits cleanly

        self._run(monkeypatch, once=True, claim=flaky)
        assert len(calls) == 2

    def test_once_still_terminates_when_it_never_recovers(self, monkeypatch):
        """The regression round one introduced: it must not spin forever."""
        # Steps of 100s, so the 900s budget is crossed in a handful of ticks.
        clock = itertools.count(0.0, 100.0)
        monkeypatch.setattr(rp.time, "monotonic", lambda: next(clock))

        def always_fails(self, kinds):
            raise rp.PollerError("unreachable")

        with pytest.raises(rp.PollerError) as exc:
            self._run(monkeypatch, once=True, claim=always_fails)
        assert "giving up after" in str(exc.value)

    def test_a_continuous_consumer_also_gives_up(self, monkeypatch):
        """A pod spinning forever against a dead API is invisible; one that
        exits is a visible CrashLoopBackOff."""
        clock = itertools.count(0.0, 100.0)
        monkeypatch.setattr(rp.time, "monotonic", lambda: next(clock))

        def always_fails(self, kinds):
            raise rp.PollerError("unreachable")

        with pytest.raises(rp.PollerError) as exc:
            self._run(monkeypatch, once=False, claim=always_fails)
        assert "giving up after" in str(exc.value)

    def test_the_budget_outlasts_a_keel_migration_window(self):
        """Sized against the outage this estate actually has.

        A Keel deploy that ships before its migration runs produces 10-14
        minute windows of 500s, and four shipped in one day. A budget
        expressed as a COUNT would be `attempts x --poll` — ten attempts at
        the 3s default is 30 seconds, which cannot ride that out.
        """
        assert rp._FAILURE_BUDGET_SECONDS >= 14 * 60

    def test_a_success_resets_the_budget(self, monkeypatch):
        """A blip every few polls must not accumulate into a false give-up."""

        class _Sentinel(Exception):
            """Not a PollerError, so it ends the loop rather than being absorbed."""

        # Each failure run is well inside the budget, separated by a success.
        outcomes = (
            [rp.PollerError("blip")] * 3
            + [None]
            + [rp.PollerError("blip")] * 3
            + [_Sentinel()]
        )
        seen = []

        def flaky(self, kinds):
            item = outcomes[len(seen)]
            seen.append(item)
            if isinstance(item, Exception):
                raise item
            return None

        with pytest.raises(_Sentinel):
            self._run(monkeypatch, once=False, claim=flaky)
        assert len(seen) == len(outcomes)


class TestANonJsonBodyDoesNotEndTheRun:
    """`requests.exceptions.JSONDecodeError` IS a `RequestException`.

    So calling `.json()` at the call site re-opened the hole `_request` exists
    to close: an ingress answering 200 with an HTML error page raised past the
    loop's `except PollerError` and killed the run.
    """

    @pytest.fixture
    def command(self, monkeypatch):
        cmd = rp.Command()
        cmd.token_provider = mock.Mock()
        cmd.token_provider.get_token.return_value = "tok"
        monkeypatch.setattr(
            rp.settings, "JOBS_API_BASE", "https://api.test/api/jobs", raising=False
        )
        return cmd

    @staticmethod
    def _html_response():
        response = mock.Mock(
            status_code=200,
            url="https://api.test/api/jobs/claim/",
            text="<html>502 Bad Gateway</html>",
        )
        response.json.side_effect = ValueError("Expecting value")
        response.request = mock.Mock(method="POST")
        return response

    def test_claim_reports_it_as_a_poller_error(self, command):
        with mock.patch.object(
            rp.requests, "request", return_value=self._html_response()
        ):
            with pytest.raises(rp.PollerError) as exc:
                command._claim(["case_review"])
        assert "non-JSON body" in str(exc.value)

    def test_the_body_is_quoted_so_the_cause_is_visible(self, command):
        with mock.patch.object(
            rp.requests, "request", return_value=self._html_response()
        ):
            with pytest.raises(rp.PollerError) as exc:
                command._claim(["case_review"])
        assert "502 Bad Gateway" in str(exc.value)

    def test_listing_has_the_same_protection(self, command):
        with mock.patch.object(
            rp.requests, "request", return_value=self._html_response()
        ):
            with pytest.raises(rp.PollerError):
                command._list_queued(["case_review"])


class TestBackoffAndEscapes:
    """Round four: two holes left in the failure handling."""

    @staticmethod
    def _run(monkeypatch, *, once, claim, poll=3.0, sleeps=None):
        provider = mock.Mock()
        provider.get_token.return_value = "tok"
        monkeypatch.setattr(rp, "get_provider", lambda: provider)
        monkeypatch.setattr(rp.settings, "CASEWORK_POLLER_TOKEN", "", raising=False)
        monkeypatch.setattr(
            rp.time, "sleep", (sleeps.append if sleeps is not None else (lambda _s: None))
        )
        monkeypatch.setattr(rp.Command, "_claim", claim)
        return rp.Command().handle(
            apply=True, once=once, poll=poll, kinds=["case_review"]
        )

    def test_the_retry_backs_off_instead_of_hammering(self, monkeypatch):
        """A flat `poll` sleep means ~300 claims per pod across the budget,
        each with a token fetch, against a service that is already failing."""
        clock = itertools.count(0.0, 100.0)
        monkeypatch.setattr(rp.time, "monotonic", lambda: next(clock))
        sleeps = []

        def always_fails(self, kinds):
            raise rp.PollerError("unreachable")

        with pytest.raises(rp.PollerError):
            self._run(monkeypatch, once=False, claim=always_fails, sleeps=sleeps)

        assert sleeps == sorted(sleeps), "backoff must be non-decreasing"
        assert sleeps[-1] > sleeps[0], "it must actually grow"
        assert max(sleeps) <= rp._MAX_FAILURE_SLEEP_SECONDS

    def test_poll_zero_cannot_hot_spin(self, monkeypatch):
        """`--poll 0` would otherwise be a busy loop for the whole budget."""
        clock = itertools.count(0.0, 100.0)
        monkeypatch.setattr(rp.time, "monotonic", lambda: next(clock))
        sleeps = []

        def always_fails(self, kinds):
            raise rp.PollerError("unreachable")

        with pytest.raises(rp.PollerError):
            self._run(monkeypatch, once=False, claim=always_fails, poll=0.0, sleeps=sleeps)
        assert min(sleeps) >= rp._MIN_FAILURE_SLEEP_SECONDS

    def test_a_success_clears_the_backoff_too(self, monkeypatch):
        """Otherwise a long-lived consumer that saw a blip stays slow forever."""

        class _Sentinel(Exception):
            pass

        outcomes = [rp.PollerError("x")] * 4 + [None] + [rp.PollerError("x"), _Sentinel()]
        seen = []
        sleeps = []

        def flaky(self, kinds):
            item = outcomes[len(seen)]
            seen.append(item)
            if isinstance(item, Exception):
                raise item
            return None

        with pytest.raises(_Sentinel):
            self._run(monkeypatch, once=False, claim=flaky, sleeps=sleeps)
        # The sleep after the post-success failure is back at the floor.
        assert sleeps[-1] == sleeps[0]


class TestSubmitFailuresDoNotEndTheRun:
    """`_process_job`'s unknown-kind branch calls `_submit` outside any try.

    `_submit` now raises PollerError on a transport failure, and the loop's
    `except PollerError` wraps only `_claim` — so a reset there propagated out
    and ended the run, the exact escape the budget exists to bound.
    """

    @pytest.fixture
    def command(self, monkeypatch):
        cmd = rp.Command()
        cmd.token_provider = mock.Mock()
        cmd.token_provider.get_token.return_value = "tok"
        monkeypatch.setattr(
            rp.settings, "JOBS_API_BASE", "https://api.test/api/jobs", raising=False
        )
        return cmd

    def test_an_unknown_kind_whose_report_fails_does_not_raise(self, command):
        with mock.patch.object(
            rp.requests, "request", side_effect=requests.ConnectionError("reset")
        ):
            command._process_job({"id": 9, "kind": "a_kind_with_no_handler"})
