"""
Tests for the statistics API endpoint.

Tests the /api/statistics/ endpoint for case statistics aggregation and the
shared snapshot it is served from. The endpoint reads the precomputed
``StatisticsSnapshot`` row (refreshed out-of-band by the ``refresh_statistics``
management command) and only computes inline as a one-time bootstrap when no
snapshot exists yet — which is the state every test here starts from.
"""

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
    StatisticsSnapshot,
)
from cases.services.statistics import (
    STATISTICS_SNAPSHOT_KEY,
    bootstrap_placeholder,
    compute_statistics,
)
from entities.models import StoredEntity
from courts.models import Court, CourtCase
from materials.models import Material


@pytest.fixture
def api_client():
    """Create an API client for testing."""
    return APIClient()


@pytest.mark.django_db
class TestStatisticsEndpoint:
    """Test suite for the statistics API endpoint."""

    def test_statistics_endpoint_returns_200(self, api_client):
        """Test that the statistics endpoint returns 200 OK."""
        response = api_client.get("/api/statistics/")
        assert response.status_code == 200

    def test_statistics_publicly_cacheable_on_bootstrap(self, api_client):
        """The bootstrap-winner response (real data) is publicly cacheable."""
        response = api_client.get("/api/statistics/")
        assert response["Cache-Control"] == "public, max-age=60, s-maxage=300"

    def test_statistics_publicly_cacheable_from_snapshot(self, api_client):
        """The served-snapshot response is publicly cacheable (edge TTL 5 min)."""
        api_client.get("/api/statistics/")  # bootstrap the snapshot row

        response = api_client.get("/api/statistics/")
        assert response["Cache-Control"] == "public, max-age=60, s-maxage=300"

    def test_statistics_response_structure(self, api_client):
        """Test that the response contains all required fields."""
        response = api_client.get("/api/statistics/")
        data = response.json()

        assert "published_cases" in data
        assert "entities_tracked" in data
        assert "cases_under_investigation" in data
        assert "cases_closed" in data
        assert "last_updated" in data

    def test_statistics_field_types(self, api_client):
        """Test that all fields have correct types."""
        response = api_client.get("/api/statistics/")
        data = response.json()

        assert isinstance(data["published_cases"], int)
        assert isinstance(data["entities_tracked"], int)
        assert isinstance(data["cases_under_investigation"], int)
        assert isinstance(data["cases_closed"], int)
        assert isinstance(data["last_updated"], str)

    def test_statistics_empty_database(self, api_client):
        """Test statistics with empty database returns zeros."""
        response = api_client.get("/api/statistics/")
        data = response.json()

        assert data["published_cases"] == 0
        assert data["entities_tracked"] == 0
        assert data["cases_under_investigation"] == 0
        assert data["cases_closed"] == 0


@pytest.mark.django_db
class TestStatisticsCounting:
    """Test suite for statistics counting logic."""

    def test_published_cases_count(self, api_client):
        """Test that published cases are counted correctly."""
        # Create cases in different states
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Published Case 1",
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Published Case 2",
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.DRAFT, title="Draft Case"
        )

        response = api_client.get("/api/statistics/")
        data = response.json()

        assert data["published_cases"] == 2

    def test_cases_under_investigation_count(self, api_client):
        """Test that draft and in-review cases are counted as under investigation."""
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.DRAFT, title="Draft Case 1"
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.DRAFT, title="Draft Case 2"
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.IN_REVIEW,
            title="In Review Case",
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Published Case",
        )

        response = api_client.get("/api/statistics/")
        data = response.json()

        assert data["cases_under_investigation"] == 3  # 2 DRAFT + 1 IN_REVIEW

    def test_cases_under_investigation_excludes_published_and_closed(self, api_client):
        """Test that only DRAFT and IN_REVIEW cases count as under investigation."""
        draft_case = Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT,
            title="Draft Investigation Case",
        )
        review_case = Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.IN_REVIEW,
            title="Review Investigation Case",
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Published Case",
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.CLOSED,
            title="Closed Case",
        )

        response = api_client.get("/api/statistics/")
        data = response.json()

        assert draft_case.slug != review_case.slug
        assert data["cases_under_investigation"] == 2

    def test_cases_closed_count(self, api_client):
        """Test that closed cases are counted correctly."""
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.CLOSED, title="Closed Case 1"
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.CLOSED, title="Closed Case 2"
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Published Case",
        )

        response = api_client.get("/api/statistics/")
        data = response.json()

        assert data["cases_closed"] == 2

    def test_entities_tracked_count(self, api_client):
        """entities_tracked counts distinct NES ids bound to PUBLISHED cases.

        Entities have no local table (NES owns them); the metric is the number
        of distinct nes_ids referenced by published cases' binds.
        """
        published = Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Published Case",
        )
        CaseEntityRelationship.objects.create(
            case=published,
            nes_id="https://jawafdehi.org/entity/person/test1",
            relationship_type=RelationshipType.ACCUSED,
        )
        CaseEntityRelationship.objects.create(
            case=published,
            nes_id="https://jawafdehi.org/entity/person/test2",
            relationship_type=RelationshipType.RELATED,
        )
        # Same entity bound again (different role) must not be double counted.
        CaseEntityRelationship.objects.create(
            case=published,
            nes_id="https://jawafdehi.org/entity/person/test1",
            relationship_type=RelationshipType.WITNESS,
        )

        response = api_client.get("/api/statistics/")
        data = response.json()

        assert data["entities_tracked"] == 2

    def test_statistics_with_mixed_states(self, api_client):
        """Test statistics with cases in all different states."""
        # Only entities bound to PUBLISHED cases are tracked.
        published_case = Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Published Case",
        )
        CaseEntityRelationship.objects.create(
            case=published_case,
            nes_id="https://jawafdehi.org/entity/person/test1",
            relationship_type=RelationshipType.ALLEGED,
        )

        draft_case = Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.DRAFT, title="Draft Case"
        )
        # A bind on a non-published case is NOT counted.
        CaseEntityRelationship.objects.create(
            case=draft_case,
            nes_id="https://jawafdehi.org/entity/person/test2",
            relationship_type=RelationshipType.ACCUSED,
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.IN_REVIEW,
            title="In Review Case",
        )
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.CLOSED, title="Closed Case"
        )

        response = api_client.get("/api/statistics/")
        data = response.json()

        assert data["published_cases"] == 1
        assert data["cases_under_investigation"] == 2
        assert data["cases_closed"] == 1
        assert data["entities_tracked"] == 1


@pytest.mark.django_db
class TestStatisticsSnapshot:
    """The endpoint serves the shared precomputed snapshot, not live counts."""

    def test_first_request_bootstraps_snapshot(self, api_client):
        """With no snapshot row, the first request computes and persists one."""
        assert not StatisticsSnapshot.objects.exists()

        data = api_client.get("/api/statistics/").json()

        snapshot = StatisticsSnapshot.objects.get(pk=STATISTICS_SNAPSHOT_KEY)
        assert snapshot.data == data

    def test_claim_race_loser_serves_placeholder_uncached(
        self, api_client, monkeypatch
    ):
        """A request that loses the bootstrap claim race serves the placeholder
        with ``no-store``, so the zeroed blocks are never edge-cached.

        The race is simulated by having the placeholder build (which happens
        between the missing-snapshot check and the claiming INSERT) create the
        row first, forcing the view's INSERT into the IntegrityError path.
        """
        from django.utils import timezone

        from cases import api_views

        real_placeholder = api_views.bootstrap_placeholder

        def racing_placeholder():
            payload = real_placeholder()
            StatisticsSnapshot.objects.create(
                key=STATISTICS_SNAPSHOT_KEY,
                data={"published_cases": 999},
                computed_at=timezone.now(),
            )
            return payload

        monkeypatch.setattr(api_views, "bootstrap_placeholder", racing_placeholder)

        response = api_client.get("/api/statistics/")

        assert response.status_code == 200
        assert response["Cache-Control"] == "no-store"
        # The loser serves its own placeholder, not the winner's row.
        assert response.json()["published_cases"] == 0
        # The winner's row was left untouched by the losing request.
        snapshot = StatisticsSnapshot.objects.get(pk=STATISTICS_SNAPSHOT_KEY)
        assert snapshot.data == {"published_cases": 999}

    def test_committed_placeholder_row_is_served_uncached(self, api_client):
        """A placeholder row found in the database is served with ``no-store``.

        While the bootstrap winner spends multi-second computing the real
        payload, its committed claim row is what every other request serves —
        it must never be edge-cached, or zeroed statistics would be pinned at
        the CDN for a full TTL.
        """
        from django.utils import timezone

        StatisticsSnapshot.objects.create(
            key=STATISTICS_SNAPSHOT_KEY,
            data=bootstrap_placeholder(),
            computed_at=timezone.now(),
            is_placeholder=True,
        )

        response = api_client.get("/api/statistics/")
        assert response.status_code == 200
        assert response["Cache-Control"] == "no-store"

    def test_refresh_clears_placeholder_flag(self, api_client):
        """The refresh upsert converts a placeholder row into a cacheable one."""
        from django.utils import timezone

        StatisticsSnapshot.objects.create(
            key=STATISTICS_SNAPSHOT_KEY,
            data=bootstrap_placeholder(),
            computed_at=timezone.now(),
            is_placeholder=True,
        )

        call_command("refresh_statistics")

        snapshot = StatisticsSnapshot.objects.get(pk=STATISTICS_SNAPSHOT_KEY)
        assert snapshot.is_placeholder is False
        response = api_client.get("/api/statistics/")
        assert response["Cache-Control"] == "public, max-age=60, s-maxage=300"

    def test_bootstrap_placeholder_matches_payload_shape(self):
        """The claim-race placeholder mirrors the real payload exactly.

        Requests that lose the bootstrap claim race are served
        ``bootstrap_placeholder()`` — on an empty database it must equal the
        real computed payload (same keys, same zero values) so consumers never
        see a shape they can't handle. Pins the hand-written zero blocks
        against drift when metrics are added to the real computation.
        """
        placeholder = bootstrap_placeholder()
        real = compute_statistics()
        placeholder.pop("last_updated")
        real.pop("last_updated")
        assert placeholder == real

    def test_snapshot_is_served_until_refreshed(self, api_client):
        """Data changes do NOT show up until the snapshot is refreshed."""
        # Create initial case
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Initial Case",
        )

        # First request - bootstraps the snapshot
        response1 = api_client.get("/api/statistics/")
        data1 = response1.json()
        assert data1["published_cases"] == 1

        # Create another case
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED, title="New Case"
        )

        # Second request - still the stored snapshot (still 1)
        response2 = api_client.get("/api/statistics/")
        data2 = response2.json()
        assert data2["published_cases"] == 1

        # Verify last_updated is the same (same snapshot)
        assert data1["last_updated"] == data2["last_updated"]

    def test_refresh_command_updates_snapshot(self, api_client):
        """The refresh_statistics management command recomputes the snapshot."""
        # Create initial case
        Case.objects.create(
            case_type=CaseType.CORRUPTION,
            state=CaseState.PUBLISHED,
            title="Initial Case",
        )

        # First request - bootstraps the snapshot
        response1 = api_client.get("/api/statistics/")
        data1 = response1.json()
        assert data1["published_cases"] == 1

        # Create another case
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED, title="New Case"
        )

        # Refresh out-of-band, the way the scheduled job does
        call_command("refresh_statistics")

        # Request after refresh - reflects the new case
        response2 = api_client.get("/api/statistics/")
        data2 = response2.json()
        assert data2["published_cases"] == 2
        assert data2["last_updated"] > data1["last_updated"]

    def test_snapshot_is_consistent_across_requests(self, api_client):
        """Repeated requests serve the identical snapshot payload."""
        # First request
        response1 = api_client.get("/api/statistics/")
        data1 = response1.json()

        # Second request
        response2 = api_client.get("/api/statistics/")
        data2 = response2.json()

        # Should return identical data (same snapshot row)
        assert data1 == data2

    def test_snapshot_stores_complete_response(self, api_client):
        """Test that all fields survive the snapshot round-trip."""
        case = Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED, title="Test Case"
        )
        CaseEntityRelationship.objects.create(
            case=case,
            nes_id="https://jawafdehi.org/entity/person/test",
            relationship_type=RelationshipType.ACCUSED,
        )

        # First request - persists the snapshot
        response1 = api_client.get("/api/statistics/")
        data1 = response1.json()

        # Second request - from the stored snapshot
        response2 = api_client.get("/api/statistics/")
        data2 = response2.json()

        # All fields should match
        assert data1["published_cases"] == data2["published_cases"]
        assert data1["entities_tracked"] == data2["entities_tracked"]
        assert data1["cases_under_investigation"] == data2["cases_under_investigation"]
        assert data1["cases_closed"] == data2["cases_closed"]
        assert data1["last_updated"] == data2["last_updated"]


@pytest.mark.django_db
class TestStatisticsPerformance:
    """Test suite for statistics performance characteristics."""

    def test_statistics_with_large_dataset(self, api_client):
        """Test statistics calculation with a larger dataset."""
        # Create multiple published cases, each binding a distinct NES entity.
        for i in range(5):
            case = Case.objects.create(
                case_type=CaseType.CORRUPTION,
                state=CaseState.PUBLISHED,
                title=f"Published Case {i}",
            )
            CaseEntityRelationship.objects.create(
                case=case,
                nes_id=f"https://jawafdehi.org/entity/person/test{i}",
                relationship_type=RelationshipType.ACCUSED,
            )

        for i in range(3):
            Case.objects.create(
                case_type=CaseType.CORRUPTION,
                state=CaseState.DRAFT,
                title=f"Draft Case {i}",
            )

        for i in range(2):
            Case.objects.create(
                case_type=CaseType.CORRUPTION,
                state=CaseState.CLOSED,
                title=f"Closed Case {i}",
            )

        response = api_client.get("/api/statistics/")
        data = response.json()

        assert data["published_cases"] == 5
        assert data["cases_under_investigation"] == 3
        assert data["cases_closed"] == 2
        # Each published case binds one distinct NES entity id, so 5 unique
        # entities are tracked (entities_tracked counts distinct nes_ids).
        assert data["entities_tracked"] == 5

    def test_multiple_concurrent_requests(self, api_client):
        """Test that multiple requests return consistent results."""
        Case.objects.create(
            case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED, title="Test Case"
        )

        # Make multiple requests
        responses = [api_client.get("/api/statistics/") for _ in range(5)]

        # All should return 200
        assert all(r.status_code == 200 for r in responses)

        # All should return same data (from cache after first request)
        data_list = [r.json() for r in responses]
        first_data = data_list[0]
        assert all(d == first_data for d in data_list)


def _make_entity(prefix, slug, entity_type, data):
    """Create a StoredEntity row with a canonical @id IRI and ``data`` doc."""
    iri = f"https://jawafdehi.org/entity/{prefix}/{slug}"
    full = {"@id": iri, "@type": entity_type, **data}
    return StoredEntity.objects.create(
        iri=iri,
        entity_type=entity_type,
        prefix=prefix,
        slug=slug,
        data=full,
    )


@pytest.mark.django_db
class TestNesMetrics:
    """The ``nes`` block of the statistics payload (NES entity coverage)."""

    def test_nes_block_present_and_zeroed_when_empty(self, api_client):
        data = api_client.get("/api/statistics/").json()
        assert "nes" in data
        nes = data["nes"]
        assert nes["total"] == 0
        assert nes["by_prefix"] == []
        assert nes["by_type"] == []
        for key in ("with_identifier", "with_provenance", "with_bilingual_name"):
            assert nes["counts"][key] == 0
            assert nes["completeness"][key] == 0.0

    def test_nes_total_and_breakdowns(self, api_client):
        _make_entity("person", "ram", "Person", {"name": {"en": "Ram", "ne": "राम"}})
        _make_entity("person", "sita", "Person", {"name": "Sita"})
        _make_entity("organization", "ciaa", "Organization", {"name": "CIAA"})

        nes = api_client.get("/api/statistics/").json()["nes"]
        assert nes["total"] == 3

        by_prefix = {row["prefix"]: row["count"] for row in nes["by_prefix"]}
        assert by_prefix == {"person": 2, "organization": 1}
        # Highest count first.
        assert nes["by_prefix"][0]["prefix"] == "person"

        by_type = {row["entity_type"]: row["count"] for row in nes["by_type"]}
        assert by_type == {"Person": 2, "Organization": 1}


@pytest.mark.django_db
class TestNgmMetrics:
    """The ``ngm`` block of the statistics payload (judicial coverage)."""

    def test_ngm_block_present_and_zeroed_when_empty(self, api_client):
        data = api_client.get("/api/statistics/").json()
        assert "ngm" in data
        ngm = data["ngm"]
        assert ngm["court_cases_total"] == 0
        assert ngm["courts_total"] == 0
        assert ngm["by_court_type"] == []
        for key in ("nes_resolved", "with_registration_date", "with_document_sources"):
            assert ngm["counts"][key] == 0
            assert ngm["completeness"][key] == 0.0
        # Materials moved to their own block (no longer under ngm).
        assert "materials_total" not in ngm
        assert "by_material_type" not in ngm

    def test_materials_block_present_and_zeroed_when_empty(self, api_client):
        data = api_client.get("/api/statistics/").json()
        assert "materials" in data
        mats = data["materials"]
        assert mats["total"] == 0
        assert mats["by_type"] == []
        assert mats["by_source"] == []
        for key in ("with_description", "with_url", "with_date"):
            assert mats["counts"][key] == 0
            assert mats["completeness"][key] == 0.0

    def test_ngm_totals_breakdowns_and_completeness(self, api_client):
        from datetime import date

        district = Court.objects.create(
            identifier="kathmandudc",
            court_type="district",
            full_name_nepali="जिल्ला अदालत काठमाडौं",
            full_name_english="District Court Kathmandu",
        )
        supreme = Court.objects.create(
            identifier="sc",
            court_type="supreme",
            full_name_nepali="सर्वोच्च अदालत",
            full_name_english="Supreme Court",
        )

        # Case 1: fully populated — NES-resolved, has reg date + document sources.
        CourtCase.objects.create(
            case_number="082-OA-0001",
            court=district,
            registration_date_ad=date(2026, 1, 11),
            nes_id="https://jawafdehi.org/entity/person/ram",
            document_sources=[{"document_id": "ngm:doc:1"}],
        )
        # Case 2: bare — no nes_id, no reg date, no document sources.
        CourtCase.objects.create(case_number="082-OA-0002", court=district)
        # Case 3: different court type, partially populated (reg date only).
        CourtCase.objects.create(
            case_number="082-CR-0003",
            court=supreme,
            registration_date_ad=date(2026, 2, 1),
        )

        Material.objects.create(
            iri="https://jawafdehi.org/material/nkp/2080-act-1",
            material_type="Legislation",
            source="nkp",
            ident="2080-act-1",
            data={
                "@id": "https://jawafdehi.org/material/nkp/2080-act-1",
                "@type": "Legislation",
                "name": "Act",
            },
        )

        payload = api_client.get("/api/statistics/").json()
        ngm = payload["ngm"]
        assert ngm["court_cases_total"] == 3
        assert ngm["courts_total"] == 2

        by_court_type = {
            row["court__court_type"]: row["count"] for row in ngm["by_court_type"]
        }
        assert by_court_type == {"district": 2, "supreme": 1}

        # Materials now live in their own top-level block, not under ngm.
        mats = payload["materials"]
        assert mats["total"] == 1
        by_material_type = {
            row["material_type"]: row["count"] for row in mats["by_type"]
        }
        assert by_material_type == {"Legislation": 1}
        by_source = {row["source"]: row["count"] for row in mats["by_source"]}
        assert by_source == {"nkp": 1}

        # 1 of 3 NES-resolved; 2 of 3 have a reg date; 1 of 3 has document sources.
        assert ngm["counts"]["nes_resolved"] == 1
        assert ngm["counts"]["with_registration_date"] == 2
        assert ngm["counts"]["with_document_sources"] == 1
        assert ngm["completeness"]["nes_resolved"] == pytest.approx(33.3)
        assert ngm["completeness"]["with_registration_date"] == pytest.approx(66.7)
        assert ngm["completeness"]["with_document_sources"] == pytest.approx(33.3)
