import pytest
from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from ngm import api_views

QUERY_URL = "/api/ngm/query_judicial"
CASE_DETAIL_URL_TEMPLATE = "/api/ngm/court_case/{case_id}"
User = get_user_model()


def add_user_to_groups(user, *group_names):
    for group_name in group_names:
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def clear_cache():
    """Clear cache before and after each test to prevent throttle carryover."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def authenticated_user(db):
    user = User.objects.create_user(username="ngm_user", password="testpass123")
    return user


@pytest.fixture
def authenticated_client(authenticated_user):
    user = authenticated_user
    token = Token.objects.create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.mark.django_db
def test_query_endpoint_requires_authentication(api_client):
    response = api_client.post(
        QUERY_URL,
        data={"query": "SELECT * FROM court_cases", "timeout": 5},
        format="json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_query_endpoint_rejects_invalid_query(authenticated_client):
    response = authenticated_client.post(
        QUERY_URL,
        data={"query": "DELETE FROM court_cases", "timeout": 5},
        format="json",
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["data"] is None
    assert payload["query_time_ms"] == 0
    assert "Only SELECT queries are allowed" in payload["error"]


@pytest.mark.django_db
def test_query_endpoint_serializer_errors_use_common_envelope(authenticated_client):
    response = authenticated_client.post(
        QUERY_URL,
        data={"query": "x" * 2049, "timeout": 5},
        format="json",
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert payload["data"] is None
    assert payload["query_time_ms"] == 0
    assert "query" in payload["error"]


@pytest.mark.django_db
def test_query_endpoint_returns_503_when_ngm_not_configured(
    authenticated_client, monkeypatch
):
    def raise_not_configured(query, timeout_seconds):
        raise ValueError("NGM database is not configured")

    monkeypatch.setattr(api_views, "execute_select_query", raise_not_configured)

    response = authenticated_client.post(
        QUERY_URL,
        data={"query": "SELECT * FROM court_cases", "timeout": 5},
        format="json",
    )
    assert response.status_code == 503
    payload = response.json()
    assert payload["success"] is False
    assert "not configured" in payload["error"].lower()


@pytest.mark.django_db
def test_query_endpoint_returns_500_for_unexpected_execution_errors(
    authenticated_client, monkeypatch
):
    def raise_unexpected(query, timeout_seconds):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(api_views, "execute_select_query", raise_unexpected)

    response = authenticated_client.post(
        QUERY_URL,
        data={"query": "SELECT * FROM court_cases", "timeout": 5},
        format="json",
    )

    assert response.status_code == 500
    payload = response.json()
    assert payload == {
        "success": False,
        "data": None,
        "error": "Internal server error",
        "query_time_ms": 0,
    }


@pytest.mark.django_db
def test_query_endpoint_success_response_shape(authenticated_client, monkeypatch):
    def fake_execute(query, timeout_seconds):
        assert query == "SELECT case_number FROM court_cases"
        assert timeout_seconds == 10
        return {
            "columns": ["case_number"],
            "rows": [["081-CR-0098"]],
            "row_count": 1,
            "max_rows": 500,
            "query_time_ms": 12,
        }

    monkeypatch.setattr(api_views, "execute_select_query", fake_execute)

    response = authenticated_client.post(
        QUERY_URL,
        data={"query": "SELECT case_number FROM court_cases", "timeout": 10},
        format="json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["error"] is None
    assert payload["data"]["columns"] == ["case_number"]
    assert payload["data"]["rows"] == [["081-CR-0098"]]
    assert payload["data"]["row_count"] == 1
    assert payload["data"]["max_rows"] == 500
    assert payload["query_time_ms"] == 12


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("group_names", "expected_rate"),
    [
        ([], "60/hour"),
        (["NGM_SilverTier"], "60/hour"),
        (["NGM_GoldTier"], "200/hour"),
        (["NGM_PlatinumTier"], "500/hour"),
        (["Moderator"], "500/hour"),
        (["Admin"], "500/hour"),
        (["NGM_SilverTier", "NGM_GoldTier"], "200/hour"),
        (["NGM_SilverTier", "Admin"], "500/hour"),
    ],
)
def test_query_rate_uses_highest_priority_group(
    authenticated_user, group_names, expected_rate
):
    add_user_to_groups(authenticated_user, *group_names)

    throttle = api_views.NGMQueryRateThrottle()

    assert throttle.get_user_rate(authenticated_user) == expected_rate


@pytest.mark.django_db
def test_query_endpoint_rate_limited_per_token(authenticated_client, monkeypatch):
    monkeypatch.setattr(api_views.NGMQueryRateThrottle, "DEFAULT_RATE", "2/min")

    def fake_execute(query, timeout_seconds):
        return {
            "columns": ["case_number"],
            "rows": [["081-CR-0098"]],
            "row_count": 1,
            "max_rows": 500,
            "query_time_ms": 1,
        }

    monkeypatch.setattr(api_views, "execute_select_query", fake_execute)

    payload = {"query": "SELECT case_number FROM court_cases", "timeout": 5}

    first = authenticated_client.post(QUERY_URL, data=payload, format="json")
    second = authenticated_client.post(QUERY_URL, data=payload, format="json")
    third = authenticated_client.post(QUERY_URL, data=payload, format="json")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429


@pytest.mark.django_db
def test_court_case_detail_public_access_and_throttling(
    api_client, clear_cache, monkeypatch
):
    """Test that court case detail endpoint is public and throttled."""

    # Mock the service to return a valid case
    def fake_get_details(court_identifier, case_number):
        return {
            "case": {
                "case_number": "081-CR-0081",
                "court_identifier": "supreme",
                "registration_date_bs": "2081-01-15",
                "registration_date_ad": None,
                "case_type": "Criminal",
                "division": None,
                "category": None,
                "section": None,
                "plaintiff": "State",
                "defendant": "John Doe",
                "original_case_number": None,
                "case_id": "supreme-081-CR-0081",
                "priority": None,
                "registration_number": "12345",
                "case_status": "Pending",
                "verdict_date_bs": None,
                "verdict_date_ad": None,
                "verdict_judge": None,
                "status": "active",
            },
            "hearings": [],
            "entities": [],
        }

    monkeypatch.setattr(api_views, "get_court_case_details", fake_get_details)

    # Test 1: Unauthenticated request should succeed (public endpoint)
    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:081-CR-0081")
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["case_number"] == "081-CR-0081"

    # Test 2: Throttling - make many requests to trigger rate limit
    # NGMQueryRateThrottle default is 60/hour, so we need to exceed that
    for _ in range(65):
        response = api_client.get(
            CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:081-CR-0081")
        )
        if response.status_code == 429:
            break

    # Should eventually get throttled
    assert response.status_code == 429


@pytest.mark.django_db
def test_court_case_detail_invalid_format(api_client, clear_cache):
    response = api_client.get(CASE_DETAIL_URL_TEMPLATE.format(case_id="invalid-format"))
    assert response.status_code == 400
    payload = response.json()
    assert "Invalid case_id format" in payload["error"]


@pytest.mark.django_db
def test_court_case_detail_not_found(api_client, clear_cache, monkeypatch):
    def fake_get_details(court_identifier, case_number):
        return None

    monkeypatch.setattr(api_views, "get_court_case_details", fake_get_details)

    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:999-XX-9999")
    )
    assert response.status_code == 404
    payload = response.json()
    assert "Case not found" in payload["error"]


@pytest.mark.django_db
def test_court_case_detail_success(api_client, clear_cache, monkeypatch):
    def fake_get_details(court_identifier, case_number):
        assert court_identifier == "supreme"
        assert case_number == "081-CR-0081"
        return {
            "case": {
                "case_number": "081-CR-0081",
                "court_identifier": "supreme",
                "registration_date_bs": "2081-01-15",
                "registration_date_ad": None,
                "case_type": "Criminal",
                "division": None,
                "category": None,
                "section": None,
                "plaintiff": "State",
                "defendant": "John Doe",
                "original_case_number": None,
                "case_id": "supreme-081-CR-0081",
                "priority": None,
                "registration_number": "12345",
                "case_status": "Pending",
                "verdict_date_bs": None,
                "verdict_date_ad": None,
                "verdict_judge": None,
                "status": "active",
            },
            "hearings": [
                {
                    "id": 1,
                    "case_number": "081-CR-0081",
                    "court_identifier": "supreme",
                    "hearing_date_bs": "2081-02-01",
                    "hearing_date_ad": None,
                    "bench": "Bench 1",
                    "bench_type": "Single",
                    "judge_names": "Judge A",
                    "lawyer_names": "Lawyer B",
                    "serial_no": "1",
                    "case_status": "Hearing",
                    "decision_type": None,
                    "remarks": "First hearing",
                }
            ],
            "entities": [
                {
                    "id": 1,
                    "case_number": "081-CR-0081",
                    "court_identifier": "supreme",
                    "side": "plaintiff",
                    "name": "State",
                    "address": None,
                    "nes_id": None,
                },
                {
                    "id": 2,
                    "case_number": "081-CR-0081",
                    "court_identifier": "supreme",
                    "side": "defendant",
                    "name": "John Doe",
                    "address": "Kathmandu",
                    "nes_id": None,
                },
            ],
        }

    monkeypatch.setattr(api_views, "get_court_case_details", fake_get_details)

    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:081-CR-0081")
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["case_number"] == "081-CR-0081"
    assert payload["court_identifier"] == "supreme"
    assert payload["plaintiff"] == "State"
    assert payload["defendant"] == "John Doe"
    assert len(payload["hearings"]) == 1
    assert len(payload["entities"]) == 2
    assert payload["hearings"][0]["judge_names"] == "Judge A"
    assert payload["entities"][0]["side"] == "plaintiff"
    assert payload["entities"][1]["side"] == "defendant"


@pytest.mark.django_db
def test_court_case_detail_returns_503_when_ngm_not_configured(
    api_client, clear_cache, monkeypatch
):
    def raise_not_configured(court_identifier, case_number):
        raise ValueError("NGM database is not configured")

    monkeypatch.setattr(api_views, "get_court_case_details", raise_not_configured)

    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:081-CR-0081")
    )
    assert response.status_code == 503
    payload = response.json()
    assert "not configured" in payload["error"].lower()


@pytest.mark.django_db
def test_court_case_detail_lowercase_normalization(
    api_client, clear_cache, monkeypatch
):
    """Test that lowercase case numbers are normalized to uppercase."""

    def fake_get_details(court_identifier, case_number):
        assert case_number == "081-CR-0081"  # Should be normalized
        return {
            "case": {
                "case_number": "081-CR-0081",
                "court_identifier": "supreme",
                "registration_date_bs": None,
                "registration_date_ad": None,
                "case_type": None,
                "division": None,
                "category": None,
                "section": None,
                "plaintiff": None,
                "defendant": None,
                "original_case_number": None,
                "case_id": None,
                "priority": None,
                "registration_number": None,
                "case_status": None,
                "verdict_date_bs": None,
                "verdict_date_ad": None,
                "verdict_judge": None,
                "status": None,
            },
            "hearings": [],
            "entities": [],
        }

    monkeypatch.setattr(api_views, "get_court_case_details", fake_get_details)

    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:081-cr-0081")
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_court_case_detail_missing_zeros_normalization(
    api_client, clear_cache, monkeypatch
):
    """Test that case numbers with missing leading zeros are normalized."""

    def fake_get_details(court_identifier, case_number):
        assert case_number == "081-CR-0081"  # Should be normalized from 81-cr-81
        return {
            "case": {
                "case_number": "081-CR-0081",
                "court_identifier": "supreme",
                "registration_date_bs": None,
                "registration_date_ad": None,
                "case_type": None,
                "division": None,
                "category": None,
                "section": None,
                "plaintiff": None,
                "defendant": None,
                "original_case_number": None,
                "case_id": None,
                "priority": None,
                "registration_number": None,
                "case_status": None,
                "verdict_date_bs": None,
                "verdict_date_ad": None,
                "verdict_judge": None,
                "status": None,
            },
            "hearings": [],
            "entities": [],
        }

    monkeypatch.setattr(api_views, "get_court_case_details", fake_get_details)

    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:81-cr-81")
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_court_case_detail_devanagari_normalization(
    api_client, clear_cache, monkeypatch
):
    """Test that Devanagari numerals are normalized to ASCII."""

    def fake_get_details(court_identifier, case_number):
        assert case_number == "081-CR-0081"  # Should be normalized from Devanagari
        return {
            "case": {
                "case_number": "081-CR-0081",
                "court_identifier": "supreme",
                "registration_date_bs": None,
                "registration_date_ad": None,
                "case_type": None,
                "division": None,
                "category": None,
                "section": None,
                "plaintiff": None,
                "defendant": None,
                "original_case_number": None,
                "case_id": None,
                "priority": None,
                "registration_number": None,
                "case_status": None,
                "verdict_date_bs": None,
                "verdict_date_ad": None,
                "verdict_judge": None,
                "status": None,
            },
            "hearings": [],
            "entities": [],
        }

    monkeypatch.setattr(api_views, "get_court_case_details", fake_get_details)

    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:०८१-CR-००८१")
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_court_case_detail_invalid_case_number_format(api_client, clear_cache):
    """Test that invalid case number format returns 400."""
    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:invalid-format")
    )
    assert response.status_code == 400
    assert "Invalid case number format" in response.json()["error"]


@pytest.mark.django_db
def test_court_case_detail_combined_normalization(api_client, clear_cache, monkeypatch):
    """Test normalization with Devanagari, lowercase, and missing zeros."""

    def fake_get_details(court_identifier, case_number):
        assert case_number == "081-CR-0081"
        return {
            "case": {
                "case_number": "081-CR-0081",
                "court_identifier": "supreme",
                "registration_date_bs": None,
                "registration_date_ad": None,
                "case_type": None,
                "division": None,
                "category": None,
                "section": None,
                "plaintiff": None,
                "defendant": None,
                "original_case_number": None,
                "case_id": None,
                "priority": None,
                "registration_number": None,
                "case_status": None,
                "verdict_date_bs": None,
                "verdict_date_ad": None,
                "verdict_judge": None,
                "status": None,
            },
            "hearings": [],
            "entities": [],
        }

    monkeypatch.setattr(api_views, "get_court_case_details", fake_get_details)

    # Test with Devanagari and missing zeros
    response = api_client.get(
        CASE_DETAIL_URL_TEMPLATE.format(case_id="supreme:८१-cr-८१")
    )
    assert response.status_code == 200
