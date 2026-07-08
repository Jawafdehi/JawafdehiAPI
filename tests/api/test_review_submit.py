"""Tests for POST /api/casework/reviews/submit/ identifier resolution.

Caseworkers submit a canonical @id IRI — a Jawafdehi case IRI
(``https://<base>/case/<slug>``) or a court-case IRI
(``https://<base>/courtcase/<court>/<case_number>``) bound to one case. The
endpoint resolves that IRI to the case's canonical slug and enqueues the review
on it. A bare ``slug`` is still accepted for the internal re-run / regrade path.
A non-IRI (a case number, a case name, a stray URL) is rejected at submit time
with a clear 400 — NOT silently enqueued to die later at payload-build.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from review.models import CaseReview
from tests.conftest import create_user_with_role

URL = "/api/casework/reviews/submit/"

CASE_SLUG = "case-080-cr-0111-alpha-land-fraud"
CASE_IRI = f"https://jawafdehi.org/case/{CASE_SLUG}"
COURTCASE_IRI = "https://jawafdehi.org/courtcase/special/080-cr-0111"


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def caseworker(db):
    return create_user_with_role("cw", "cw@example.com", "Caseworker")


@pytest.fixture
def case(db):
    return Case.objects.create(
        slug=CASE_SLUG,
        title="Alpha land fraud",
        case_type=CaseType.CORRUPTION,
        state=CaseState.IN_REVIEW,
        court_cases=[COURTCASE_IRI],
    )


@pytest.mark.django_db
def test_case_iri_resolves_to_slug(caseworker, case):
    resp = _authed_client(caseworker).post(URL, {"iri": CASE_IRI}, format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["slug"] == CASE_SLUG
    assert CaseReview.objects.filter(slug=CASE_SLUG).exists()


@pytest.mark.django_db
def test_courtcase_iri_resolves_to_the_referencing_case(caseworker, case):
    resp = _authed_client(caseworker).post(URL, {"iri": COURTCASE_IRI}, format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["slug"] == CASE_SLUG


@pytest.mark.django_db
def test_courtcase_iri_is_canonicalized_before_lookup(caseworker, case):
    # A court-case IRI on a non-canonical host still resolves: it is re-based to
    # the canonical authority before the join lookup. (The path grammar itself
    # is lowercase-only, matching the stored references.)
    resp = _authed_client(caseworker).post(
        URL,
        {"iri": "http://api.jawafdehi.org/courtcase/special/080-cr-0111"},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["slug"] == CASE_SLUG


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["080-CR-0111", "Giribandhu", "special:081-CR-0136"])
def test_non_iri_is_rejected(caseworker, case, bad):
    resp = _authed_client(caseworker).post(URL, {"iri": bad}, format="json")
    assert resp.status_code == 400
    assert not CaseReview.objects.exists()


@pytest.mark.django_db
def test_case_iri_for_unknown_case_is_rejected(caseworker):
    resp = _authed_client(caseworker).post(
        URL, {"iri": "https://jawafdehi.org/case/does-not-exist"}, format="json"
    )
    assert resp.status_code == 400
    assert "does-not-exist" in str(resp.data)
    assert not CaseReview.objects.exists()


@pytest.mark.django_db
def test_courtcase_iri_with_no_referencing_case_is_rejected(caseworker):
    resp = _authed_client(caseworker).post(
        URL,
        {"iri": "https://jawafdehi.org/courtcase/special/080-cr-9999"},
        format="json",
    )
    assert resp.status_code == 400
    assert not CaseReview.objects.exists()


@pytest.mark.django_db
def test_slug_path_still_accepted_for_rerun(caseworker, case):
    # The re-run / regrade path submits the already-resolved canonical slug.
    resp = _authed_client(caseworker).post(URL, {"slug": CASE_SLUG}, format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["slug"] == CASE_SLUG


@pytest.mark.django_db
def test_slug_path_for_unknown_case_is_rejected(caseworker):
    resp = _authed_client(caseworker).post(URL, {"slug": "no-such-case"}, format="json")
    assert resp.status_code == 400
    assert not CaseReview.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [{}, {"iri": CASE_IRI, "slug": CASE_SLUG}, {"iri": "", "slug": ""}],
)
def test_requires_exactly_one_identifier(caseworker, case, payload):
    resp = _authed_client(caseworker).post(URL, payload, format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_requires_contributor_role(case):
    reader = create_user_with_role("ro", "ro@example.com", "ReadOnly")
    resp = _authed_client(reader).post(URL, {"iri": CASE_IRI}, format="json")
    assert resp.status_code in (401, 403)
    assert not CaseReview.objects.exists()
