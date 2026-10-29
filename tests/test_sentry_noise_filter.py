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
    monkeypatch.setattr(logging_config, "_disallowed_host_last_reported", {})
    monkeypatch.setattr(logging_config, "_window_started_at", float("-inf"))
    monkeypatch.setattr(logging_config, "_reports_this_window", 0)


def _host_event(host):
    return _event(("DisallowedHost", f"Invalid HTTP_HOST header: '{host}'."))


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
        first = _host_event("mcp.jawafdehi.org")
        assert drop_transport_noise(first, {}) is first
        for _ in range(500):
            assert drop_transport_noise(_host_event("mcp.jawafdehi.org"), {}) is None

    def test_a_later_window_reports_again(self, monkeypatch):
        """So an ongoing misconfiguration keeps nagging rather than going quiet."""
        monkeypatch.setenv("SENTRY_DISALLOWED_HOST_WINDOW_SECONDS", "60")
        first = _event(("DisallowedHost", "a"))
        assert drop_transport_noise(first, {}) is first
        assert drop_transport_noise(_event(("DisallowedHost", "b")), {}) is None

        monkeypatch.setattr(logging_config, "_disallowed_host_last_reported", {})
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


class TestTheWindowIsPerHost:
    """Round three: a single global slot defeats the whole justification.

    The scanner contributes ~160 events a day; a genuine ALLOWED_HOSTS
    misconfiguration contributes one. With one shared slot, the event that
    gets through is essentially never the one worth seeing — and it names the
    scanner's host, so even the heartbeat points at the wrong thing.
    """

    def test_a_new_host_reports_even_while_a_scanner_is_running(self):
        scanner = _host_event("mcp.jawafdehi.org")
        assert drop_transport_noise(scanner, {}) is scanner
        for _ in range(50):
            drop_transport_noise(_host_event("mcp.jawafdehi.org"), {})

        # The real one: a public hostname missing from ALLOWED_HOSTS.
        genuine = _host_event("www.jawafdehi.org")
        assert drop_transport_noise(genuine, {}) is genuine

    def test_each_host_is_limited_independently(self):
        for host in ("a.example", "b.example", "c.example"):
            first = _host_event(host)
            assert drop_transport_noise(first, {}) is first
            assert drop_transport_noise(_host_event(host), {}) is None

    def test_the_host_is_parsed_out_of_djangos_message(self):
        assert (
            logging_config._rejected_host(
                "Invalid HTTP_HOST header: 'evil.example'. You may need to add..."
            )
            == "evil.example"
        )

    def test_an_unparseable_message_still_rate_limits_under_one_key(self):
        """Degrades to the old global behaviour rather than reporting forever."""
        first = _event(("DisallowedHost", "something unexpected"))
        assert drop_transport_noise(first, {}) is first
        assert drop_transport_noise(_event(("DisallowedHost", "also odd")), {}) is None

    def test_a_rotating_host_header_cannot_grow_the_map_without_limit(self):
        for n in range(logging_config._MAX_TRACKED_HOSTS + 50):
            drop_transport_noise(_host_event(f"h{n}.example"), {})
        assert (
            len(logging_config._disallowed_host_last_reported)
            <= logging_config._MAX_TRACKED_HOSTS
        )

    def test_a_rotating_host_header_is_actually_rate_limited(self):
        """Per-host alone is no limit at all: every request looks new.

        The observed adversary already uses two hostnames, so rotating is a
        trivial change for it. The global ceiling is what bounds the volume.
        """
        reported = sum(
            drop_transport_noise(_host_event(f"h{n}.example"), {}) is not None
            for n in range(5000)
        )
        assert reported <= logging_config._MAX_REPORTS_PER_WINDOW

    def test_overflow_evicts_the_oldest_rather_than_clearing(self, monkeypatch):
        """Clearing would drop the flooding host's timestamp too, so it would
        report again on its next request — overflow making the limiter looser."""
        monkeypatch.setattr(logging_config, "_MAX_REPORTS_PER_WINDOW", 10**9)
        drop_transport_noise(_host_event("flooder.example"), {})
        for n in range(logging_config._MAX_TRACKED_HOSTS + 10):
            drop_transport_noise(_host_event(f"h{n}.example"), {})
        # The flooder was the oldest, so it may have been evicted — what must
        # NOT happen is the map being wiped wholesale.
        assert (
            len(logging_config._disallowed_host_last_reported)
            >= logging_config._MAX_TRACKED_HOSTS - 1
        )

    def test_the_window_resets_the_global_ceiling(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DISALLOWED_HOST_WINDOW_SECONDS", "60")
        for n in range(50):
            drop_transport_noise(_host_event(f"a{n}.example"), {})
        # Exhausted; now move past the window.
        monkeypatch.setattr(logging_config, "_window_started_at", float("-inf"))
        monkeypatch.setattr(logging_config, "_reports_this_window", 0)
        fresh = _host_event("later.example")
        assert drop_transport_noise(fresh, {}) is fresh
