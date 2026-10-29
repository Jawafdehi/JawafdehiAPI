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

from unittest import mock

import pytest
import requests

from review import oidc_client_credentials as cc
from review.management.commands import review_poller as rp


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(rp, "_TRANSPORT_BACKOFF_SECONDS", 0)


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
    def test_a_reset_connection_becomes_a_poller_error(self, command, no_backoff):
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
        assert "after 3 attempts" in str(exc.value)

    def test_a_blip_that_clears_is_invisible_to_the_caller(self, command, no_backoff):
        responses = [
            requests.ConnectionError("reset"),
            mock.Mock(status_code=200, json=lambda: {"id": 7, "kind": "case_review"}),
        ]

        def flaky(*args, **kwargs):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(rp.requests, "request", side_effect=flaky):
            job = command._claim(["case_review"])
        assert job == {"id": 7, "kind": "case_review"}

    def test_an_empty_queue_still_reads_as_empty(self, command, no_backoff):
        """204 is the hot path; the retry wrapper must not disturb it."""
        with mock.patch.object(
            rp.requests, "request", return_value=mock.Mock(status_code=204)
        ):
            assert command._claim(["case_review"]) is None

    def test_a_transport_failure_fetching_the_token_is_retried_too(
        self, command, no_backoff
    ):
        """`_headers()` calls the issuer, so it can fail the same way."""
        command.token_provider.get_token.side_effect = cc.OIDCTransportError("read timeout")
        with mock.patch.object(rp.requests, "request") as req:
            with pytest.raises(rp.PollerError):
                command._claim(["case_review"])
        assert req.call_count == 0

    def test_a_bad_credential_is_not_retried_and_stays_fatal(self, command, no_backoff):
        """A configuration error must not be absorbed into the loop's retry."""
        command.token_provider.get_token.side_effect = cc.OIDCConfigurationError(
            "invalid_client"
        )
        with pytest.raises(cc.OIDCConfigurationError):
            command._claim(["case_review"])

    def test_every_outbound_helper_goes_through_the_retry(self, command, no_backoff):
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
