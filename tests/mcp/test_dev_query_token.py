"""Security contract for the Zitadel-free E2E query token."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


@pytest.fixture
def dev_query_auth(settings, monkeypatch):
    from courts.views import IngestionCasesView, QueryView
    from jawafdehi_shared.auth.dev_service import (
        DevelopmentQueryTokenAuthentication,
    )
    from jawafdehi_shared.auth.oidc import OIDCAuthentication

    settings.TESTING = True
    settings.DEV_AUTH = True
    settings.DEV_NGM_QUERY_TOKEN = "query-only"
    settings.DEV_NGM_QUERY_USERNAME = "mcp-query-e2e"
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "jawafdehi_shared.auth.dev_service."
            "DevelopmentQueryTokenAuthentication",
            "jawafdehi_shared.auth.oidc.OIDCAuthentication",
        ],
    }
    authentication_classes = [
        DevelopmentQueryTokenAuthentication,
        OIDCAuthentication,
    ]
    monkeypatch.setattr(
        QueryView,
        "authentication_classes",
        authentication_classes,
    )
    monkeypatch.setattr(
        IngestionCasesView,
        "authentication_classes",
        authentication_classes,
    )
    get_user_model().objects.create(username="mcp-query-e2e")
    return APIClient()


@pytest.mark.django_db(databases="__all__")
def test_dev_query_token_can_only_reach_read_plane(dev_query_auth):
    dev_query_auth.credentials(HTTP_AUTHORIZATION="Bearer query-only")

    query = dev_query_auth.post(
        "/api/query/",
        {"query": "SELECT 1"},
        format="json",
    )
    ingestion = dev_query_auth.post(
        "/api/ingestion/cases/",
        {"items": []},
        format="json",
    )

    assert query.status_code == 200
    assert ingestion.status_code == 401
