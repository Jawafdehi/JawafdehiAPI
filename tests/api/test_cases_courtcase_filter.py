"""
Tests for the ``?courtcase=<iri>`` reverse-lookup filter on GET /api/cases/.

Powers the "Related Jawafdehi cases" section on a court case's own page: the
PUBLISHED cases that cite a given NGM court case, flat and reverse-chronological.

Unlike the sibling ``?entity=`` filter, this one is PUBLISHED-only for EVERY
caller including casework roles — a court-case page is a public archive record,
not a casework surface, so a DRAFT/IN_REVIEW case must never be named on one.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from tests.conftest import create_case_with_entities, create_user_with_role

CC = "https://jawafdehi.org/courtcase/kathmandudc/081-fn-12327"
OTHER_CC = "https://jawafdehi.org/courtcase/special/081-cr-0079"


def _make(slug, title, state, court_cases):
    return Case.objects.create(
        slug=slug,
        title=title,
        state=state,
        case_type=CaseType.CORRUPTION,
        court_cases=court_cases,
    )


def _get(courtcase, client=None):
    resp = (client or APIClient()).get("/api/cases/", {"courtcase": courtcase})
    assert resp.status_code == 200
    return resp.data


@pytest.mark.django_db
class TestCaseCourtCaseFilter:
    def test_returns_only_citing_published_cases(self):
        _make("cf-a", "Cites it", CaseState.PUBLISHED, [CC])
        _make("cf-b", "Also cites it", CaseState.PUBLISHED, [OTHER_CC, CC])
        # Published but cites a different court case -> excluded.
        _make("cf-other", "Other court case", CaseState.PUBLISHED, [OTHER_CC])
        # Cites it but IN_REVIEW -> hidden.
        _make("cf-inreview", "In review", CaseState.IN_REVIEW, [CC])
        # Cites it but DRAFT -> hidden.
        _make("cf-draft", "Draft", CaseState.DRAFT, [CC])

        data = _get(CC)

        assert data["count"] == 2
        assert {c["slug"] for c in data["results"]} == {"cf-a", "cf-b"}

    def test_unknown_courtcase_returns_empty(self):
        _make("cf-lonely", "Lonely", CaseState.PUBLISHED, [CC])

        data = _get("https://jawafdehi.org/courtcase/supreme/099-cr-9999")

        assert data["count"] == 0
        assert data["results"] == []

    def test_mixed_case_iri_is_canonicalized_before_lookup(self):
        # Stored IRIs are canonical (build_courtcase_iri lowercases court +
        # case number). A caller echoing the court's own casing must still match.
        _make("cf-canon", "Canonical", CaseState.PUBLISHED, [CC])

        data = _get("https://jawafdehi.org/courtcase/KathmanduDC/081-FN-12327")

        assert [c["slug"] for c in data["results"]] == ["cf-canon"]

    def test_non_iri_param_returns_empty_not_500(self):
        _make("cf-safe", "Safe", CaseState.PUBLISHED, [CC])

        # A bare case number, a stray URL, junk — none can be cited by a case.
        # "" is malformed input too, NOT an absent filter: ``?courtcase=`` must
        # fail closed to an empty page rather than fall through to the full
        # unfiltered case list (a falsey presence check used to do exactly that).
        for junk in (
            "081-FN-12327",
            "https://example.com/nope",
            "../../etc/passwd",
            "",
        ):
            data = _get(junk)
            assert data["count"] == 0, repr(junk)

    def test_empty_param_does_not_fall_through_to_unfiltered_list(self):
        # Pins the regression directly: the empty-value response must not be the
        # unfiltered list. Cases here cite NO court case, so a fall-through would
        # return them while a correctly-failing-closed filter returns nothing.
        _make("cf-unrelated-1", "Unrelated", CaseState.PUBLISHED, [])
        _make("cf-unrelated-2", "Also unrelated", CaseState.PUBLISHED, [])

        assert APIClient().get("/api/cases/").data["count"] == 2
        assert _get("")["count"] == 0

    def test_reverse_chronological(self):
        now = timezone.now()
        old = _make("cf-old", "Oldest", CaseState.PUBLISHED, [CC])
        mid = _make("cf-mid", "Middle", CaseState.PUBLISHED, [CC])
        new = _make("cf-new", "Newest", CaseState.PUBLISHED, [CC])

        # auto_now_add ignores create-time created_at; set it explicitly.
        Case.objects.filter(pk=new.pk).update(created_at=now)
        Case.objects.filter(pk=mid.pk).update(created_at=now - timedelta(days=1))
        Case.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=2))

        assert [c["slug"] for c in _get(CC)["results"]] == [
            "cf-new",
            "cf-mid",
            "cf-old",
        ]

    def test_caseworker_also_sees_published_only(self):
        # THE deviation from ?entity=: a signed-in caseworker gets the same
        # PUBLISHED-only list as the public, because this powers a public
        # court-record page. Caseworkers work on the Jawafdehi case itself.
        _make("cf-pub", "Published", CaseState.PUBLISHED, [CC])
        _make("cf-hidden", "In review", CaseState.IN_REVIEW, [CC])

        client = APIClient()
        client.force_authenticate(
            user=create_user_with_role("cw", "cw@example.com", "Caseworker")
        )

        data = _get(CC, client)

        assert [c["slug"] for c in data["results"]] == ["cf-pub"]

    def test_state_param_cannot_widen_past_published(self):
        _make("cf-scoped", "In review", CaseState.IN_REVIEW, [CC])

        resp = APIClient().get("/api/cases/", {"courtcase": CC, "state": "IN_REVIEW"})

        assert resp.status_code == 200
        assert resp.data["count"] == 0


ENTITY = "https://jawafdehi.org/entity/person/cf-test-person"


@pytest.mark.django_db
class TestCourtCaseFilterComposesWithEntity:
    """``?courtcase=`` must NARROW alongside ``?entity=``, not be dropped by it.

    Both are reverse lookups and the ``entity`` branch returns early, so before
    this was fixed ``?entity=X&courtcase=Y`` silently ignored ``courtcase`` and
    answered as though only ``entity`` had been passed — a filter failing OPEN.
    """

    def test_both_params_intersect(self):
        # Cites the entity AND the court case -> the only match.
        create_case_with_entities(
            slug="cf-both",
            title="Both",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[ENTITY],
            court_cases=[CC],
        )
        # Cites the entity but a DIFFERENT court case -> must be excluded.
        create_case_with_entities(
            slug="cf-entity-only",
            title="Entity only",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[ENTITY],
            court_cases=[OTHER_CC],
        )

        resp = APIClient().get("/api/cases/", {"entity": ENTITY, "courtcase": CC})

        assert resp.status_code == 200
        assert [c["slug"] for c in resp.data["results"]] == ["cf-both"]

    def test_entity_param_cannot_widen_courtcase_past_published(self):
        # The PUBLISHED-only rule is the stricter of the two and must survive the
        # combination: a caseworker sees DRAFT/IN_REVIEW through ``?entity=``
        # alone, but adding ``courtcase`` scopes the answer to a public
        # court-record page, where an unpublished case must never be named.
        create_case_with_entities(
            slug="cf-combo-pub",
            title="Published",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[ENTITY],
            court_cases=[CC],
        )
        create_case_with_entities(
            slug="cf-combo-inreview",
            title="In review",
            state=CaseState.IN_REVIEW,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[ENTITY],
            court_cases=[CC],
        )

        client = APIClient()
        client.force_authenticate(
            user=create_user_with_role("cw2", "cw2@example.com", "Caseworker")
        )

        # Sanity: ?entity= alone DOES show the caseworker the in-review case.
        entity_only = client.get("/api/cases/", {"entity": ENTITY})
        assert {c["slug"] for c in entity_only.data["results"]} == {
            "cf-combo-pub",
            "cf-combo-inreview",
        }

        # Adding ?courtcase= narrows it back to PUBLISHED-only.
        combined = client.get("/api/cases/", {"entity": ENTITY, "courtcase": CC})
        assert combined.status_code == 200
        assert [c["slug"] for c in combined.data["results"]] == ["cf-combo-pub"]

    def test_malformed_courtcase_still_fails_closed_alongside_entity(self):
        create_case_with_entities(
            slug="cf-junk-combo",
            title="Cites entity",
            state=CaseState.PUBLISHED,
            case_type=CaseType.CORRUPTION,
            alleged_entities=[ENTITY],
            court_cases=[CC],
        )

        for junk in ("not-an-iri", ""):
            resp = APIClient().get("/api/cases/", {"entity": ENTITY, "courtcase": junk})
            assert resp.status_code == 200
            assert resp.data["count"] == 0, repr(junk)
