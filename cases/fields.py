"""
Custom field types for the Case model.

These fields provide structured validation and storage for list-based data.
"""

from django.core.exceptions import ValidationError
from django.db import models


class EntityListField(models.JSONField):
    """
    Stores and validates a list of entity ID strings.

    Entity IDs are validated using the NES validate_entity_id() function.
    """

    def __init__(self, *args, **kwargs):
        kwargs["default"] = list
        super().__init__(*args, **kwargs)

    def validate(self, value, model_instance):
        """Validate that value is a list of valid entity IDs using NES validator."""
        from nes.core.identifiers.validators import validate_entity_id

        super().validate(value, model_instance)

        if not isinstance(value, list):
            raise ValidationError("Value must be a list")

        # Allow empty lists for optional fields (blank=True)
        # The Case model's validate() method will check if alleged_entities is empty
        if len(value) == 0:
            # Only raise error if this is a required field (not blank)
            if not self.blank:
                raise ValidationError("At least one entity ID is required")
            return

        for entity_id in value:
            if not isinstance(entity_id, str):
                raise ValidationError(f"Entity ID must be a string: {entity_id}")

            # Use NES validator for consistent validation
            try:
                validate_entity_id(entity_id)
            except ValueError as e:
                raise ValidationError(str(e))


class TextListField(models.JSONField):
    """
    Stores a list of text strings.

    Used for key_allegations and tags fields.
    """

    def __init__(self, *args, **kwargs):
        kwargs["default"] = list
        super().__init__(*args, **kwargs)

    def validate(self, value, model_instance):
        """Validate that value is a list of non-empty strings."""
        super().validate(value, model_instance)

        if not isinstance(value, list):
            raise ValidationError("Value must be a list")

        for item in value:
            if not isinstance(item, str):
                raise ValidationError(
                    f"All items must be strings, got {type(item).__name__}: {item}"
                )

            if not item or not item.strip():
                raise ValidationError("Text items cannot be empty or whitespace-only")


class TimelineListField(models.JSONField):
    """
    Stores a list of timeline entry objects.

    Each entry must have: date (ISO format), title, description
    """

    def __init__(self, *args, **kwargs):
        kwargs["default"] = list
        kwargs["blank"] = True
        super().__init__(*args, **kwargs)

    def validate(self, value, model_instance):
        """Validate that value is a list of valid timeline entries."""
        super().validate(value, model_instance)

        if not isinstance(value, list):
            raise ValidationError("Value must be a list")

        for entry in value:
            if not isinstance(entry, dict):
                raise ValidationError(f"Timeline entry must be a dictionary: {entry}")

            # Check required fields (description is optional)
            required_fields = ["date", "title"]
            for field in required_fields:
                if field not in entry:
                    raise ValidationError(
                        f"Timeline entry missing required field '{field}': {entry}"
                    )

            # Validate date format (ISO format: YYYY-MM-DD)
            date_str = entry["date"]
            if not isinstance(date_str, str):
                raise ValidationError(f"Timeline date must be a string: {date_str}")

            try:
                from datetime import datetime

                datetime.fromisoformat(date_str)
            except (ValueError, TypeError):
                raise ValidationError(
                    f"Invalid date format (expected ISO format YYYY-MM-DD): {date_str}"
                )

            # Validate title is non-empty string
            if not isinstance(entry["title"], str) or not entry["title"].strip():
                raise ValidationError(
                    f"Timeline title must be a non-empty string: {entry}"
                )

            # Validate description if present (optional field)
            if "description" in entry:
                if not isinstance(entry["description"], str):
                    raise ValidationError(
                        f"Timeline description must be a string: {entry}"
                    )


class EvidenceListField(models.JSONField):
    """
    Stores a list of evidence entry objects.

    Each entry must have: source_id, description
    """

    def __init__(self, *args, **kwargs):
        kwargs["default"] = list
        kwargs["blank"] = True
        super().__init__(*args, **kwargs)

    def validate(self, value, model_instance):
        """Validate that value is a list of valid evidence entries."""
        super().validate(value, model_instance)

        if not isinstance(value, list):
            raise ValidationError("Value must be a list")

        for entry in value:
            if not isinstance(entry, dict):
                raise ValidationError(f"Evidence entry must be a dictionary: {entry}")

            # Check required fields
            required_fields = ["source_id", "description"]
            for field in required_fields:
                if field not in entry:
                    raise ValidationError(
                        f"Evidence entry missing required field '{field}': {entry}"
                    )

            # Validate source_id is non-empty string
            source_id = entry["source_id"]
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValidationError(
                    f"Evidence source_id must be a non-empty string: {entry}"
                )

            # Validate description is non-empty string
            description = entry["description"]
            if not isinstance(description, str) or not description.strip():
                raise ValidationError(
                    f"Evidence description must be a non-empty string: {entry}"
                )
