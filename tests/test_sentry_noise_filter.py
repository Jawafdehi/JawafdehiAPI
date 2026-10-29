# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
"""What is worth reporting to Sentry, and what is somebody else's port scan.

`DisallowedHost` was the single busiest issue in the org: 25,293 lifetime
events on `mcp.jawafdehi.org`, with a twin on `mcp-internal.jawafdehi.org`,
together 77% of all event volume across every project. A sample of ten requests
returned ten credential-harvesting paths — `/gcp-key.json`,
`/service-account.json`, `/.config/gcloud/application_default_credentials.json`,
`/firebase-adminsdk.json`, `/wp-admin/phpinfo.php` — behind one spoofed Chrome
user agent.

Django has already answered those with a 400 before any of our code runs, so
there is nothing to fix and nothing to act on, and their volume buried the real
defects underneath them.
"""

import pytest

from jawafdehi_shared.logging_config import drop_transport_noise


def _event(exc_type, value=""):
    return {"exception": {"values": [{"type": exc_type, "value": value}]}}


def _hint(exc_type=None, value=""):
    if exc_type is None:
        return {}
    cls = type(exc_type, (Exception,), {})
    return {"exc_info": (cls, cls(value), None)}


class TestDisallowedHost:
    def test_it_is_dropped(self):
        event = _event(
            "DisallowedHost",
            "Invalid HTTP_HOST header: 'mcp.jawafdehi.org'.",
        )
        assert drop_transport_noise(event, {}) is None

    def test_it_is_dropped_when_only_the_hint_carries_the_type(self):
        """sentry_sdk populates the hint for logging-originated events."""
        assert drop_transport_noise({}, _hint("DisallowedHost", "bad host")) is None

    def test_the_escape_hatch_puts_it_back(self, monkeypatch):
        monkeypatch.setenv("SENTRY_REPORT_DISALLOWED_HOST", "true")
        event = _event("DisallowedHost", "Invalid HTTP_HOST header")
        assert drop_transport_noise(event, {}) is event

    @pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "nonsense"])
    def test_anything_but_an_affirmative_keeps_it_dropped(self, monkeypatch, value):
        monkeypatch.setenv("SENTRY_REPORT_DISALLOWED_HOST", value)
        assert drop_transport_noise(_event("DisallowedHost"), {}) is None


class TestEverythingElseStillReports:
    """The filter must stay narrow — this is the half that matters."""

    @pytest.mark.parametrize(
        "exc_type",
        [
            "SuspiciousOperation",  # DisallowedHost's PARENT class: still reported
            "SearchUnavailable",
            "ProgrammingError",
            "OperationalError",
            "PollerError",
            "ValueError",
        ],
    )
    def test_a_real_error_survives(self, exc_type):
        event = _event(exc_type, "something genuinely broke")
        assert drop_transport_noise(event, {}) is event

    def test_an_event_with_no_exception_survives(self):
        """Message-only events (logger.error with no exc_info)."""
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

    @pytest.mark.parametrize(
        "exc_type",
        ["ClientDisconnect", "RuntimeError"],
    )
    def test_known_transport_noise_is_still_dropped(self, exc_type):
        value = (
            "Attempted to exit cancel scope in a different task"
            if exc_type == "RuntimeError"
            else "client went away"
        )
        assert drop_transport_noise(_event(exc_type, value), {}) is None
