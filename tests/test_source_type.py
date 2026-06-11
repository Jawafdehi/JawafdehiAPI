"""
Minimal test coverage for source_type field on DocumentSource.

Verifies field persistence, serialization, and valid enum values.
"""

import datetime

import pytest

from cases.models import DocumentSource, SourceType


@pytest.mark.django_db
class TestSourceTypeField:
    """Test suite for source_type field."""

    def test_source_type_persistence(self):
        """Verify source_type field persists valid enum values."""
        source = DocumentSource.objects.create(
            title="Test Source",
            source_type=SourceType.NEWS,
            publication_date=datetime.date(2024, 1, 1),
        )

        source.refresh_from_db()
        assert source.source_type == SourceType.NEWS

    def test_source_type_nullable(self):
        """Verify source_type can be null."""
        source = DocumentSource.objects.create(title="Test Source", source_type=None)

        source.refresh_from_db()
        assert source.source_type is None

    def test_source_type_serialization(self):
        """Verify source_type serializes correctly in API responses."""
        from cases.serializers import DocumentSourceSerializer

        source = DocumentSource.objects.create(
            title="Test Source", source_type=SourceType.COURT_ORDER
        )

        serializer = DocumentSourceSerializer(source)
        assert serializer.data["source_type"] == "COURT_ORDER"

    def test_source_type_invalid_value_rejected(self):
        """Verify invalid enum values are rejected at model validation layer."""
        from django.core.exceptions import ValidationError

        # Test: Model layer validation - invalid value should be rejected
        # Django's choices validation happens when calling full_clean()
        source = DocumentSource(title="Test Source", source_type="INVALID_TYPE")

        # Calling full_clean() should raise ValidationError for invalid choice
        with pytest.raises(ValidationError) as exc_info:
            source.full_clean()

        # Verify the error is specifically for source_type field
        assert "source_type" in exc_info.value.error_dict
        error_messages = str(exc_info.value.error_dict["source_type"])
        assert (
            "not a valid choice" in error_messages.lower()
            or "invalid choice" in error_messages.lower()
        )
