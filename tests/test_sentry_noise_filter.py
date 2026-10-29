# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
"""What is worth reporting to Sentry, and what is somebody else's port scan.

`DisallowedHost` was the single busiest issue in the org: 25,293 lifetime
events on `mcp.jawafdehi.org`, with a twin on `mcp-internal.jawafdehi.org`,
together 77% of all event volume across every project. A sample of ten requests
returned ten credential-harvesting paths — `/gcp-key.json`,
`/service-account.json`, `/.config/gcloud/application_default_credentials.json`,
`/firebase-adminsdk.json`, `/wp-admin/phpinfo.php` — behind one spoofed Chrome
user agent.

It is RATE-LIMITED rather than dropped. An earlier version dropped it and
justified that with two compensating controls that turned out not to exist:
`manage.py check --deploy` is nowhere in `.github/`, and the readiness probe
cannot catch a bad `ALLOWED_HOSTS` because `config/settings.py` appends
`POD_IP` to it, so the kubelet probe passes while every public request 400s.
Dropping would have removed the only signal for a misconfigured public
hostname. One report per process per window keeps that signal and still kills
the volume.
"""

import pytest

from jawafdehi_shared import logging_config
from jawafdehi_shared.logging_config import drop_transport_noise


@pytest.fixture(autouse=True)
def fresh_window(monkeypatch):
    """Each test starts with the rate limiter having reported nothing."""
    monkeypatch.setattr(logging_config, "_last_disallowed_host_report", [float("-inf")])


def _event(*types_and_values):
    """An event whose `exception.values` is oldest-cause-first, as Sentry sends."""
    return {
        "exception": {
            "values": [{"type": t, "value": v} for t, v in types_and_values]
        }
    }


def _hint(exc_type, value=""):
    cls = type(exc_type, (Exception,), {})
    return {"exc_info": (cls, cls(value), None)}


class TestDisallowedHostIsRateLimited:
    def test_the_first_one_reports(self):
        """A real misconfiguration must still surface."""
        event = _event(("DisallowedHost", "Invalid HTTP_HOST header: 'x'"))
        assert drop_transport_noise(event, {}) is event

    def test_the_flood_behind_it_does_not(self):
        first = _event(("DisallowedHost", "scan 1"))
        assert drop_transport_noise(first, {}) is first
        for n in range(500):
            assert drop_transport_noise(_event(("DisallowedHost", f"scan {n}")), {}) is None

    def test_a_later_window_reports_again(self, monkeypatch):
        """So an ongoing misconfiguration keeps nagging rather than going quiet."""
        monkeypatch.setenv("SENTRY_DISALLOWED_HOST_WINDOW_SECONDS", "60")
        first = _event(("DisallowedHost", "a"))
        assert drop_transport_noise(first, {}) is first
        assert drop_transport_noise(_event(("DisallowedHost", "b")), {}) is None

        monkeypatch.setattr(logging_config, "_last_disallowed_host_report", [-1000.0])
        third = _event(("DisallowedHost", "c"))
        assert drop_transport_noise(third, {}) is third

    def test_a_zero_window_disables_the_limit(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DISALLOWED_HOST_WINDOW_SECONDS", "0")
        for _ in range(5):
            event = _event(("DisallowedHost", "x"))
            assert drop_transport_noise(event, {}) is event

    def test_an_unparseable_window_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DISALLOWED_HOST_WINDOW_SECONDS", "not-a-number")
        first = _event(("DisallowedHost", "a"))
        assert drop_transport_noise(first, {}) is first
        assert drop_transport_noise(_event(("DisallowedHost", "b")), {}) is None

    def test_it_is_matched_from_the_hint_too(self):
        """sentry_sdk populates the hint for logging-originated events."""
        assert drop_transport_noise({}, _hint("DisallowedHost")) is not None
        assert drop_transport_noise({}, _hint("DisallowedHost")) is None


class TestOnlyTheRaisedExceptionCounts:
    """The chained-cause trap: `raise ConfigError(...) from DisallowedHost`."""

    def test_an_unrelated_error_wrapping_one_is_not_rate_limited(self):
        # Sentry orders values oldest-cause-first, so the raised error is last.
        event = _event(
            ("DisallowedHost", "Invalid HTTP_HOST header"),
            ("ImproperlyConfigured", "ALLOWED_HOSTS is empty"),
        )
        assert drop_transport_noise(event, {}) is event

    def test_it_is_not_consumed_from_the_window_either(self):
        """A wrapped cause must not spend the window's one slot."""
        wrapped = _event(
            ("DisallowedHost", "cause"), ("ImproperlyConfigured", "raised")
        )
        assert drop_transport_noise(wrapped, {}) is wrapped
        real = _event(("DisallowedHost", "the actual one"))
        assert drop_transport_noise(real, {}) is real

    def test_a_genuine_one_is_still_matched_as_the_last_entry(self):
        """A DisallowedHost that is itself the raised error is rate-limited,
        even when it has a cause of its own."""
        first = _event(("OSError", "cause"), ("DisallowedHost", "raised"))
        assert drop_transport_noise(first, {}) is first
        second = _event(("OSError", "cause"), ("DisallowedHost", "raised"))
        assert drop_transport_noise(second, {}) is None


class TestEverythingElseStillReports:
    """The filter must stay narrow — this is the half that matters."""

    @pytest.mark.parametrize(
        "exc_type",
        [
            "SuspiciousOperation",  # DisallowedHost's PARENT class
            "SearchUnavailable",
            "ProgrammingError",
            "OperationalError",
            "PollerError",
            "ValueError",
        ],
    )
    def test_a_real_error_survives(self, exc_type):
        event = _event((exc_type, "something genuinely broke"))
        assert drop_transport_noise(event, {}) is event

    def test_a_real_error_is_never_rate_limited(self):
        for _ in range(50):
            event = _event(("SearchUnavailable", "opensearch down"))
            assert drop_transport_noise(event, {}) is event

    def test_an_event_with_no_exception_survives(self):
        event = {"message": "something happened"}
        assert drop_transport_noise(event, {}) is event

    def test_an_empty_values_list_survives(self):
        event = {"exception": {"values": []}}
        assert drop_transport_noise(event, {}) is event

    def test_a_null_exception_block_survives(self):
        event = {"exception": None}
        assert drop_transport_noise(event, {}) is event

    def test_a_value_missing_its_type_survives(self):
        event = {"exception": {"values": [{"value": "no type key"}]}}
        assert drop_transport_noise(event, {}) is event


class TestPreexistingTransportFilterStillWorks:
    """Regression guard: the new branch runs after these, not instead of them."""

    def test_client_disconnect_is_still_dropped(self):
        assert drop_transport_noise(_event(("ClientDisconnect", "gone")), {}) is None

    def test_the_cancel_scope_message_is_still_dropped(self):
        event = _event(
            ("RuntimeError", "Attempted to exit cancel scope in a different task")
        )
        assert drop_transport_noise(event, {}) is None

    def test_it_is_dropped_from_the_hint_too(self):
        assert drop_transport_noise({}, _hint("ClientDisconnect", "gone")) is None


class TestTheWindowIsSizedAgainstProcessesNotPods:
    """The limiter is process-local and the image runs `--workers 2`.

    So the ceiling is `replicas x 2 / window`. A one-hour window puts that
    ceiling above the observed ~160 events/day, i.e. it would almost never
    bind — which is why the default is a day, not an hour. This pins the
    default so a well-meaning "make it more responsive" change has to confront
    the arithmetic.
    """

    def test_the_default_window_is_a_day(self):
        assert logging_config._DEFAULT_DISALLOWED_HOST_WINDOW == 86400.0

    def test_the_default_is_used_when_unset(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DISALLOWED_HOST_WINDOW_SECONDS", raising=False)
        assert logging_config._disallowed_host_window() == 86400.0

    def test_a_negative_window_fails_towards_reporting(self, monkeypatch):
        """Clamped to 0, which switches limiting OFF rather than inverting it.

        A nonsensical value must err towards reporting too much, never towards
        silently dropping — the failure mode this whole filter has to avoid.
        """
        monkeypatch.setenv("SENTRY_DISALLOWED_HOST_WINDOW_SECONDS", "-60")
        assert logging_config._disallowed_host_window() == 0.0
        for _ in range(3):
            event = _event(("DisallowedHost", "x"))
            assert drop_transport_noise(event, {}) is event
