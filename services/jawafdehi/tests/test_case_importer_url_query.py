"""
Test case_importer.py JSONField query behavior.

Verifies that the importer correctly finds existing sources by URL
when the url field is a JSONField containing a list.
"""

import json

import pytest

from cases.models import CaseEntityRelationship, DocumentSource, RelationshipType
from cases.services.case_importer import CaseImporter


@pytest.mark.django_db
class TestCaseImporterURLQuery:
    """Test suite for case importer URL deduplication with JSONField."""

    def test_finds_existing_source_by_url(self):
        """Verify importer finds existing source when URL matches."""
        # Create a source with URL in list format
        existing = DocumentSource.objects.create(
            title="Existing Source", url=["https://example.com/document.pdf"]
        )

        # Try to import with same URL
        importer = CaseImporter()
        source_data = {
            "title": "Different Title",
            "url": "https://example.com/document.pdf",
            "description": "Test description",
        }

        result = importer.get_or_create_source(source_data)

        # Should reuse existing source, not create new one
        assert result.source_id == existing.source_id
        assert importer.stats["sources_reused"] == 1
        assert importer.stats["sources_created"] == 0

    def test_creates_new_source_when_url_not_found(self):
        """Verify importer creates new source when URL doesn't match."""
        # Create a source with different URL
        DocumentSource.objects.create(
            title="Existing Source", url=["https://example.com/other.pdf"]
        )

        # Try to import with different URL
        importer = CaseImporter()
        source_data = {
            "title": "New Source",
            "url": "https://example.com/new.pdf",
            "description": "Test description",
        }

        result = importer.get_or_create_source(source_data)

        # Should create new source
        assert result.title == "New Source"
        assert importer.stats["sources_created"] == 1
        assert importer.stats["sources_reused"] == 0

    def test_finds_source_with_multiple_urls(self):
        """Verify importer finds source when it has multiple URLs."""
        # Create a source with multiple URLs
        existing = DocumentSource.objects.create(
            title="Multi-URL Source",
            url=["https://example.com/primary.pdf", "https://example.com/backup.pdf"],
        )

        # Try to import with one of the URLs
        importer = CaseImporter()
        source_data = {
            "title": "Different Title",
            "url": "https://example.com/backup.pdf",
            "description": "Test description",
        }

        result = importer.get_or_create_source(source_data)

        # Should reuse existing source
        assert result.source_id == existing.source_id
        assert importer.stats["sources_reused"] == 1

    def test_ignores_soft_deleted_sources_by_url(self):
        """Verify soft-deleted sources are ignored when matching by URL."""
        # Create a soft-deleted source with target URL
        deleted_source = DocumentSource.objects.create(
            title="Deleted Source",
            url=["https://example.com/deleted.pdf"],
            is_deleted=True,
        )

        # Try to import with same URL
        importer = CaseImporter()
        source_data = {
            "title": "New Source",
            "url": "https://example.com/deleted.pdf",
            "description": "Test description",
        }

        result = importer.get_or_create_source(source_data)

        # Should create new source, not reuse deleted one
        assert result.source_id != deleted_source.source_id
        assert result.title == "New Source"
        assert importer.stats["sources_created"] == 1
        assert importer.stats["sources_reused"] == 0

    def test_ignores_soft_deleted_sources_by_title(self):
        """Verify soft-deleted sources are ignored when matching by title."""
        # Create a soft-deleted source with target title
        deleted_source = DocumentSource.objects.create(
            title="Deleted Source",
            url=["https://example.com/deleted.pdf"],
            is_deleted=True,
        )

        # Try to import with same title but no URL
        importer = CaseImporter()
        source_data = {
            "title": "Deleted Source",
            "url": "",
            "description": "Test description",
        }

        result = importer.get_or_create_source(source_data)

        # Should create new source, not reuse deleted one
        assert result.source_id != deleted_source.source_id
        assert result.title == "Deleted Source"
        assert importer.stats["sources_created"] == 1
        assert importer.stats["sources_reused"] == 0

    def test_import_case_creates_unified_entity_relationships(self, tmp_path):
        """Importer binds accused/related entities by their canonical NES id.

        NES owns the entity data; a bind requires a valid canonical entity @id
        IRI (``https://jawafdehi.org/entity/<prefix>/<slug>``). Entries that are
        bare names (no valid NES id) are skipped — there is
        no local entity/name fallback anymore.
        """
        payload = {
            "title": "Unified importer sync test",
            "description": "Sample description",
            # Valid NES ids are bound; the bare-name entry is skipped.
            "alleged_entities": ["https://jawafdehi.org/entity/person/ram-bahadur-karki"],
            "related_entities": [
                "https://jawafdehi.org/entity/organization/ministry-of-water-supply",
                "Ministry Of Water Supply",
            ],
            "locations": [],
            "sources": [],
            "tags": [],
            "key_allegations": ["Sample allegation"],
            "timeline": [],
        }

        file_path = tmp_path / "case.json"
        file_path.write_text(json.dumps(payload), encoding="utf-8")

        importer = CaseImporter()
        case = importer.import_from_json(str(file_path), case_state="DRAFT")

        accused = CaseEntityRelationship.objects.filter(
            case=case,
            relationship_type=RelationshipType.ACCUSED,
        )
        assert list(accused.values_list("nes_id", flat=True)) == [
            "https://jawafdehi.org/entity/person/ram-bahadur-karki"
        ]
        related = CaseEntityRelationship.objects.filter(
            case=case,
            relationship_type=RelationshipType.RELATED,
        )
        assert list(related.values_list("nes_id", flat=True)) == [
            "https://jawafdehi.org/entity/organization/ministry-of-water-supply"
        ]
        # The bare-name related entry was skipped (no valid NES id).
        assert importer.stats["entities_bound"] == 2
        assert importer.stats["entities_skipped_no_nes_id"] == 1
