# SPDX-License-Identifier: Hippocratic-3.0
"""Job failures reaching Sentry.

They did not. `_process_job` catches every exception and hands it to the queue, so
it never reaches the interpreter's excepthook — which is the only thing `sentry_sdk`
hooks for a management command. Every LLM job failure this worker has had, including
two rounds of misdiagnosed `error_max_turns`, existed only in `job.error` and in pod
stdout.

The pod also had no `SENTRY_DSN` at all, which is fixed in the infra repo. Both
halves were needed: code that reports to a client that was never initialised is
still silence.
"""

from unittest import mock

import pytest

from review.management.commands.review_poller import _capture_job_failure


class TestItActuallyReports:
    def test_the_exception_is_captured(self):
        with mock.patch("sentry_sdk.capture_exception", return_value="evt-1") as cap:
            sent = _capture_job_failure(
                ValueError("boom"), job_id=2876, kind="case_proposal_intent", err="boom\ntb"
            )
        assert sent is True
        assert isinstance(cap.call_args.args[0], ValueError)

    def test_it_is_grouped_by_kind_not_by_traceback(self):
        """The question worth alerting on is "is case_proposal_intent failing?", not
        "how many distinct stack shapes did the JSON decoder produce?". An LLM
        failure's traceback varies with the prompt; the thing to alert on does not.
        """
        scope = mock.MagicMock()
        with mock.patch("sentry_sdk.new_scope") as new_scope:
            new_scope.return_value.__enter__.return_value = scope
            with mock.patch("sentry_sdk.capture_exception", return_value="e"):
                _capture_job_failure(
                    ValueError("boom"), job_id=1, kind="case_proposal_intent", err="e"
                )

        assert scope.fingerprint == ["review_poller", "job_failed", "case_proposal_intent"]
        scope.set_tag.assert_any_call("job.kind", "case_proposal_intent")

    def test_the_queues_own_error_text_rides_along(self):
        """Otherwise an operator reading Sentry has to go to Postgres to find out
        what the queue recorded."""
        scope = mock.MagicMock()
        with mock.patch("sentry_sdk.new_scope") as new_scope:
            new_scope.return_value.__enter__.return_value = scope
            with mock.patch("sentry_sdk.capture_exception", return_value="e"):
                _capture_job_failure(ValueError("x"), job_id=1, kind="k", err="THE-ERROR-TEXT")

        scope.set_extra.assert_any_call("job_error", "THE-ERROR-TEXT")

    def test_a_missing_kind_still_groups_somewhere(self):
        scope = mock.MagicMock()
        with mock.patch("sentry_sdk.new_scope") as new_scope:
            new_scope.return_value.__enter__.return_value = scope
            with mock.patch("sentry_sdk.capture_exception", return_value="e"):
                _capture_job_failure(ValueError("x"), job_id=1, kind=None, err="e")

        assert scope.fingerprint == ["review_poller", "job_failed", "unknown"]


class TestTelemetryNeverBreaksTheWorker:
    """The failure this must not cause: a reported job failure becoming an
    unreported one because the reporting itself broke."""

    @pytest.mark.parametrize("boom", [RuntimeError("sentry down"), OSError("socket")])
    def test_a_capture_failure_is_swallowed(self, boom):
        with mock.patch("sentry_sdk.capture_exception", side_effect=boom):
            assert _capture_job_failure(ValueError("x"), job_id=1, kind="k", err="e") is False

    def test_a_scope_failure_is_swallowed(self):
        with mock.patch("sentry_sdk.new_scope", side_effect=RuntimeError("no hub")):
            assert _capture_job_failure(ValueError("x"), job_id=1, kind="k", err="e") is False

    def test_no_dsn_configured_is_a_quiet_false(self):
        """capture_exception returns None when the SDK has no client, which is the
        state in dev and CI. Must not read as an error."""
        with mock.patch("sentry_sdk.capture_exception", return_value=None):
            assert _capture_job_failure(ValueError("x"), job_id=1, kind="k", err="e") is False


class TestTheHandlerPathCallsIt:
    def test_a_failing_handler_reports_before_submitting(self):
        """Wired at the call site, not just available. The bug was never that the
        helper was wrong — it was that nothing called anything."""
        from review.management.commands.review_poller import Command

        cmd = Command()
        cmd._submit = mock.Mock()
        cmd._report_stage = mock.Mock()
        cmd.stdout = mock.MagicMock()
        cmd.stderr = mock.MagicMock()

        job = {"id": 99, "kind": "case_review", "payload": {}}
        with mock.patch.dict(
            "review.management.commands.review_poller.HANDLERS",
            {"case_review": mock.Mock(side_effect=ValueError("kaboom"))},
        ):
            with mock.patch(
                "review.management.commands.review_poller._capture_job_failure"
            ) as capture:
                cmd._process_job(job)

        assert capture.called, "a failing job must reach Sentry"
        assert capture.call_args.kwargs["job_id"] == 99
        assert capture.call_args.kwargs["kind"] == "case_review"
        # And the queue is still told, which is the pre-existing behaviour.
        assert cmd._submit.call_args.args[1]["status"] == "failed"
