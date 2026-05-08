import pytest
from django.test import Client
from cases.models import Case, CaseState

@pytest.mark.django_db
def test_legacy_case_redirect():
    # 1. Create a case with the canonical slug for legacy 238
    # Canonical slug for 238 is case-081-cr-0060-681d9859
    case = Case.objects.create(
        case_id="case-081-cr-0060-id",
        title="Test Case 081-CR-0060",
        court_cases=["special:081-CR-0060"],
        state=CaseState.PUBLISHED,
        slug="case-081-cr-0060-681d9859"
    )
    
    client = Client()
    
    # 2. Request the legacy URL
    response = client.get("/case/238/")
    
    # 3. Verify 301 redirect to the slug
    assert response.status_code == 301
    assert response.url == "/case/case-081-cr-0060-681d9859"

@pytest.mark.django_db
def test_legacy_case_redirect_no_slug_fallback():
    # Case with no slug yet (e.g. DRAFT)
    # But in the new mapping, 238 points to a specific slug.
    # If the case exists but has NO slug (unlikely for published), it falls back to case_id.
    
    # Let's test with 240 which still maps to a Case Key "081-CR-0127"
    case = Case.objects.create(
        case_id="case-test-draft-081-cr-0127",
        title="Draft Test Case 081-CR-0127",
        court_cases=["special:081-CR-0127"],
        state=CaseState.DRAFT
    )
    
    client = Client()
    response = client.get("/case/240/")
    
    assert response.status_code == 301
    assert response.url == f"/case/{case.case_id}"

@pytest.mark.django_db
def test_legacy_case_redirect_unknown():
    client = Client()
    # Request an ID that isn't mapped
    response = client.get("/case/9999/")
    
    # Should fall back to index (200 OK rendering index.html)
    assert response.status_code == 200
    assert "index.html" in [t.name for t in response.templates]

@pytest.mark.django_db
def test_legacy_case_redirect_canonical_slug():
    # Verify that a canonical slug in LEGACY_CASE_MAP redirects even if case not in DB
    # (per the new fallback logic in views.py)
    client = Client()
    response = client.get("/case/229/") # Maps to case-081-cr-0044-a72c082d
    
    assert response.status_code == 301
    assert response.url == "/case/case-081-cr-0044-a72c082d"
