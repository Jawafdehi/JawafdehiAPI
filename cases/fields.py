"""
Custom field types for the Case model.

These fields provide structured validation and storage for list-based data.
"""

import re
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import models


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

    Each entry must have: date (AD ISO format), title.
    Optional: description, date_bs (Bikram Sambat YYYY-MM-DD), end_date
    (AD ISO format, for events that span a period — must be >= date),
    end_date_bs (Bikram Sambat YYYY-MM-DD for the span's end).
    """

    # Bikram Sambat dates are not Gregorian-parseable, so they are validated
    # by shape only (YYYY-MM-DD with a 4-digit year). AD<->BS conversion is
    # done on the fly elsewhere (frontend display, enrichment commands).
    _BS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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

            # Validate date format (AD ISO format: YYYY-MM-DD)
            date_str = entry["date"]
            if not isinstance(date_str, str):
                raise ValidationError(f"Timeline date must be a string: {date_str}")

            try:
                date_obj = datetime.fromisoformat(date_str)
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

            # Validate Bikram Sambat dates if present (optional, shape-checked
            # only — see _BS_DATE_RE)
            for bs_field in ("date_bs", "end_date_bs"):
                if bs_field in entry:
                    bs_value = entry[bs_field]
                    if not isinstance(bs_value, str) or not self._BS_DATE_RE.match(
                        bs_value
                    ):
                        raise ValidationError(
                            f"Timeline {bs_field} must be a Bikram Sambat date "
                            f"string in YYYY-MM-DD format: {entry}"
                        )

            # Validate end_date if present (optional AD ISO date for spans;
            # must parse and be on or after the start date)
            if "end_date" in entry:
                end_date_str = entry["end_date"]
                if not isinstance(end_date_str, str):
                    raise ValidationError(
                        f"Timeline end_date must be a string: {end_date_str}"
                    )
                try:
                    end_date_obj = datetime.fromisoformat(end_date_str)
                except (ValueError, TypeError):
                    raise ValidationError(
                        "Invalid end_date format (expected ISO format YYYY-MM-DD): "
                        f"{end_date_str}"
                    )
                if end_date_obj < date_obj:
                    raise ValidationError(
                        f"Timeline end_date must be on or after date: {entry}"
                    )


