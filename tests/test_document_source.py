"""
Property-based tests for DocumentSource model.

Feature: accountability-platform-core
Property 11: Source validation enforces required fields
Validates: Requirements 4.2
"""

import pytest

from django.core.exceptions import ValidationError
from hypothesis import given, settings

from cases.models import DocumentSource
from cases.models import SourceType
from tests.conftest import create_document_source_with_entities
from tests.strategies import (
    valid_source_data,
    source_data_missing_title,
    source_data_missing_description,
    source_data_with_empty_title,
    source_data_with_empty_description,
)

# ============================================================================
# Property 11: Source validation enforces required fields
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=100)
@given(source_data=valid_source_data())
def test_document_source_accepts_valid_data(source_data):
    """
    Feature: accountability-platform-core, Property 11: Source validation enforces required fields

    For any DocumentSource with all required fields (title, description),
    validation should pass without raising ValidationError.
    Validates: Requirements 4.2
    """
    source = create_document_source_with_entities(**source_data)
    source.save()

    # Should not raise ValidationError
    try:
        source.full_clean()
    except ValidationError as e:
        pytest.fail(f"DocumentSource validation rejected valid data: {e}")


@pytest.mark.django_db
@settings(max_examples=100)
@given(source_data=source_data_missing_title())
def test_document_source_rejects_missing_title(source_data):
    """
    Feature: accountability-platform-core, Property 11: Source validation enforces required fields

    For any DocumentSource creation attempt missing title,
    the Platform should reject the operation.
    Validates: Requirements 4.2
    """
    # Should raise ValidationError when missing title
    with pytest.raises(ValidationError) as exc_info:
        source = create_document_source_with_entities(**source_data)
        source.save()
        source.full_clean()

    # Verify error mentions title
    error_message = str(exc_info.value).lower()
    assert (
        "title" in error_message
    ), f"Validation error should mention 'title', but got: {exc_info.value}"


@pytest.mark.django_db
@settings(max_examples=100)
@given(source_data=source_data_missing_description())
def test_document_source_accepts_missing_description(source_data):
    """
    Feature: accountability-platform-core, Property 11: Source validation enforces required fields

    For any DocumentSource creation attempt missing description,
    the Platform should accept it (description is optional).
    Validates: Requirements 4.2
    """
    # Should not raise ValidationError when missing description
    source = create_document_source_with_entities(**source_data)
    source.save()

    try:
        source.full_clean()
    except ValidationError as e:
        pytest.fail(
            f"DocumentSource should accept missing description, but raised: {e}"
        )


@pytest.mark.django_db
@settings(max_examples=50)
@given(source_data=source_data_with_empty_title())
def test_document_source_rejects_empty_title(source_data):
    """
    Feature: accountability-platform-core, Property 11: Source validation enforces required fields

    For any DocumentSource with empty title (whitespace only),
    the Platform should reject the operation.
    Validates: Requirements 4.2
    """
    # Should raise ValidationError when title is empty
    with pytest.raises(ValidationError):
        source = create_document_source_with_entities(**source_data)
        source.save()


@pytest.mark.django_db
@settings(max_examples=50)
@given(source_data=source_data_with_empty_description())
def test_document_source_accepts_empty_description(source_data):
    """
    Feature: accountability-platform-core, Property 11: Source validation enforces required fields

    For any DocumentSource with empty description (whitespace only),
    the Platform should accept it (description is optional).
    Validates: Requirements 4.2
    """
    # Should not raise ValidationError when description is empty
    source = create_document_source_with_entities(**source_data)
    source.save()

    try:
        source.full_clean()
    except ValidationError as e:
        pytest.fail(f"DocumentSource should accept empty description, but raised: {e}")


# ============================================================================
# Edge Cases
# ============================================================================


@pytest.mark.django_db
def test_document_source_requires_title():
    """
    Edge case: DocumentSource must have a non-empty title.
    Validates: Requirements 4.2
    """
    with pytest.raises(ValidationError):
        source = create_document_source_with_entities(
            description="Valid description", related_entity_ids=[]
        )
        source.save()


@pytest.mark.django_db
def test_document_source_accepts_missing_description_edge_case():
    """
    Edge case: DocumentSource can be created without description (description is optional).
    Validates: Requirements 4.2
    """
    source = create_document_source_with_entities(
        title="Valid Title", related_entity_ids=[]
    )
    source.save()

    # Should not raise ValidationError without description
    try:
        source.full_clean()
    except ValidationError as e:
        pytest.fail(f"DocumentSource should allow missing description, but raised: {e}")


@pytest.mark.django_db
def test_document_source_url_is_optional():
    """
    Edge case: URL field is optional for DocumentSource.
    Validates: Requirements 4.1, 4.2
    """
    source = create_document_source_with_entities(
        title="Valid Title", description="Valid description", related_entity_ids=[]
    )
    source.save()

    # Should not raise ValidationError without URL
    try:
        source.full_clean()
    except ValidationError as e:
        pytest.fail(f"DocumentSource should allow missing URL, but raised: {e}")


@pytest.mark.django_db
def test_document_source_soft_deletion():
    """
    Edge case: DocumentSource should support soft deletion via is_deleted flag.
    Validates: Design document soft deletion requirement
    """
    source = create_document_source_with_entities(
        title="Valid Title", description="Valid description", related_entity_ids=[]
    )
    source.save()

    # Soft delete
    source.is_deleted = True
    source.save()

    # Should still exist in database
    assert DocumentSource.objects.filter(
        id=source.id
    ).exists(), "Soft-deleted source should still exist in database"

    # Verify is_deleted flag is set
    source.refresh_from_db()
    assert (
        source.is_deleted is True
    ), "is_deleted flag should be True after soft deletion"


@pytest.mark.django_db
def test_document_source_has_contributors_field():
    """
    Edge case: DocumentSource should have a contributors ManyToMany field.
    Validates: Design document - sources have contributors for access control
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()

    # Create a user
    user = User.objects.create_user(username="testuser", password="test123")

    # Create a source
    source = create_document_source_with_entities(
        title="Valid Title", description="Valid description", related_entity_ids=[]
    )
    source.save()

    # Add contributor
    source.contributors.add(user)

    # Verify contributor is assigned
    assert user in source.contributors.all(), "User should be in source contributors"

    # Verify reverse relationship
    assert (
        source in user.assigned_sources.all()
    ), "Source should be in user's assigned_sources"


@pytest.mark.django_db
def test_media_news_source_requires_publication_date():
    """MEDIA_NEWS DocumentSource must be rejected when publication_date is absent."""
    source = create_document_source_with_entities(
        title="Gorkhapatra report",
        description="News article",
        related_entity_ids=[],
    )
    source.source_type = SourceType.MEDIA_NEWS
    source.publication_date = None

    with pytest.raises(ValidationError) as exc_info:
        source.save()

    assert "publication_date" in exc_info.value.message_dict


@pytest.mark.django_db
def test_media_news_source_accepts_valid_publication_date():
    """MEDIA_NEWS DocumentSource with a publication_date must save successfully."""
    import datetime

    source = create_document_source_with_entities(
        title="Kantipur daily",
        description="News article",
        related_entity_ids=[],
    )
    source.source_type = SourceType.MEDIA_NEWS
    source.publication_date = datetime.date(2024, 3, 15)

    # Must not raise
    source.save()
    source.refresh_from_db()
    assert source.publication_date == datetime.date(2024, 3, 15)
