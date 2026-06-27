"""Tests for the NGM REST shim client (``ngm/client.py``).

These mock ``httpx`` so they run with no live NGM service. They assert the
client forwards correctly to the NGM service's gated ``/api/query`` and
read-plane ``/api/cases/{court}/{number}`` endpoints, unwraps the response into
the shape the proxy views expect, and maps HTTP statuses to the right
exceptions.
"""

import httpx
import pytest

from ngm import client


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


@pytest.fixture
def configured(settings):
    settings.NGM_API_BASE_URL = "https://ngm.example.test"
    settings.NGM_API_TOKEN = ""
    return settings


# ---------------------------------------------------------------------------
# query_judicial
# ---------------------------------------------------------------------------


def test_query_judicial_forwards_and_unwraps_envelope(configured, monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse(
            200,
            {
                "success": True,
                "data": {
                    "columns": ["case_number"],
                    "rows": [["081-CR-0098"]],
                    "row_count": 1,
                    "max_rows": 500,
                },
                "error": None,
                "query_time_ms": 12,
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    result = client.query_judicial("SELECT case_number FROM court_cases", 10)

    assert captured["url"] == "https://ngm.example.test/api/query"
    assert captured["json"] == {
        "query": "SELECT case_number FROM court_cases",
        "timeout": 10,
    }
    # No token configured -> no Authorization header.
    assert "Authorization" not in captured["headers"]
    assert result == {
        "columns": ["case_number"],
        "rows": [["081-CR-0098"]],
        "row_count": 1,
        "max_rows": 500,
        "query_time_ms": 12,
    }


def test_query_judicial_sends_bearer_token_when_configured(configured, monkeypatch):
    configured.NGM_API_TOKEN = "svc-token-abc"
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["headers"] = headers
        return FakeResponse(
            200,
            {"data": {"columns": [], "rows": [], "row_count": 0, "max_rows": 500}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    client.query_judicial("SELECT 1 FROM court_cases", 5)

    assert captured["headers"]["Authorization"] == "Bearer svc-token-abc"


def test_query_judicial_raises_rejected_on_400(configured, monkeypatch):
    def fake_post(url, json, headers, timeout):
        return FakeResponse(400, {"error": "Only SELECT queries are allowed"})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(
        client.NGMQueryRejected, match="Only SELECT queries are allowed"
    ):
        client.query_judicial("DELETE FROM court_cases", 5)


def test_query_judicial_raises_service_error_on_500(configured, monkeypatch):
    def fake_post(url, json, headers, timeout):
        return FakeResponse(500, {"error": "boom"})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(client.NGMServiceError):
        client.query_judicial("SELECT 1 FROM court_cases", 5)


def test_query_judicial_raises_service_error_on_transport_failure(
    configured, monkeypatch
):
    def fake_post(url, json, headers, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(client.NGMServiceError):
        client.query_judicial("SELECT 1 FROM court_cases", 5)


def test_query_judicial_not_configured(settings, monkeypatch):
    settings.NGM_API_BASE_URL = ""

    def fail(*a, **k):  # should never be reached
        raise AssertionError("httpx.post must not be called when unconfigured")

    monkeypatch.setattr(httpx, "post", fail)

    with pytest.raises(client.NGMServiceNotConfigured):
        client.query_judicial("SELECT 1 FROM court_cases", 5)


# ---------------------------------------------------------------------------
# get_court_case
# ---------------------------------------------------------------------------


def test_get_court_case_forwards_and_nests_top_level_shape(configured, monkeypatch):
    captured = {}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        # Read plane returns case fields at top level alongside hearings/entities.
        return FakeResponse(
            200,
            {
                "case_number": "081-CR-0081",
                "court_identifier": "supreme",
                "hearings": [{"id": 1}],
                "entities": [{"id": 2}],
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    result = client.get_court_case("supreme", "081-CR-0081")

    assert captured["url"] == "https://ngm.example.test/api/cases/supreme/081-CR-0081"
    assert result["case"]["case_number"] == "081-CR-0081"
    assert result["hearings"] == [{"id": 1}]
    assert result["entities"] == [{"id": 2}]
    assert "hearings" not in result["case"]


def test_get_court_case_accepts_already_nested_shape(configured, monkeypatch):
    def fake_get(url, headers, timeout):
        return FakeResponse(
            200,
            {
                "case": {"case_number": "081-CR-0081"},
                "hearings": [],
                "entities": [],
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    result = client.get_court_case("supreme", "081-CR-0081")
    assert result["case"] == {"case_number": "081-CR-0081"}


def test_get_court_case_returns_none_on_404(configured, monkeypatch):
    def fake_get(url, headers, timeout):
        return FakeResponse(404, {"error": "not found"})

    monkeypatch.setattr(httpx, "get", fake_get)

    assert client.get_court_case("supreme", "999-XX-9999") is None


def test_get_court_case_rejects_unsafe_ref(configured, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("httpx.get must not be called for an invalid ref")

    monkeypatch.setattr(httpx, "get", fail)

    with pytest.raises(client.NGMServiceError):
        client.get_court_case("supreme/../etc", "081-CR-0081")


def test_get_court_case_raises_service_error_on_500(configured, monkeypatch):
    def fake_get(url, headers, timeout):
        return FakeResponse(500)

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(client.NGMServiceError):
        client.get_court_case("supreme", "081-CR-0081")


def test_get_court_case_not_configured(settings):
    settings.NGM_API_BASE_URL = ""
    with pytest.raises(client.NGMServiceNotConfigured):
        client.get_court_case("supreme", "081-CR-0081")
