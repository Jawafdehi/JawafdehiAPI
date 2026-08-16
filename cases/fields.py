"""
Custom field types for the Case model.

These fields provide structured validation and storage for list-based data,
plus ``HttpsURLField`` (a Django-6-ready ``URLField``).
"""

import re
from datetime import date, datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models


class HttpsURLField(models.URLField):
    """``URLField`` that pins the form-layer ``assume_scheme`` to ``https``.

    Django 5.x warns (``RemovedInDjango60Warning``) whenever a
    ``forms.URLField`` is built without an explicit ``assume_scheme``, because
    the scheme prepended to a scheme-less value changes from ``http`` to
    ``https`` in Django 6.0. ``models.URLField.formfield()`` does not forward
    the argument, so every ModelForm over such a field trips the warning — the
    Case admin form alone accounts for the overwhelming majority of the test
    suite's warning volume.

    Passing ``assume_scheme`` here (rather than setting the
    ``FORMS_URLFIELD_ASSUME_HTTPS`` transitional setting project-wide) is the
    fix that does not trade one deprecation for another: that setting is ITSELF
    deprecated and emits ``RemovedInDjango60Warning`` on assignment
    (``django/conf/__init__.py``), so it would silence nothing.

    Adopting the Django 6.0 behaviour NOW is also the safer default: it only
    affects scheme-less input typed into a form, and ``https`` is what these
    image URLs should be. ``deconstruct()` is inherited, so this subclass is
    a no-op at the database level (same ``varchar``) — but it IS a distinct
    field path in migration state, hence the accompanying AlterField.
    """

    def formfield(self, **kwargs):
        # Bound with an explicit `dict[str, Any]` rather than splatted inline: the
        # merged literal otherwise infers a `str`-ish value type and every one of
        # `formfield`'s differently-typed parameters is reported against it.
        merged: dict[str, Any] = {"assume_scheme": "https", **kwargs}
        return super().formfield(**merged)


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
            if "description" in entry and not isinstance(entry["description"], str):
                raise ValidationError(f"Timeline description must be a string: {entry}")

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


EDIT_HISTORY_DATE_ERROR = "Invalid date format (expected ISO format YYYY-MM-DD)"

#: Extended YYYY-MM-DD only. A shape check is required IN ADDITION to
#: ``date.fromisoformat``, not instead of it: on Python 3.11+ that parser also
#: accepts ISO basic format ("20260814") and ISO week dates ("2026-W33-5"), both
#: of which would be stored verbatim in ``public_edit_history`` and rendered on
#: the public case page in a shape the frontend does not expect.
#: ``datetime.fromisoformat`` is looser still (timestamps, UTC offsets).
#: Mirrors ``TimelineListField._BS_DATE_RE``, which shape-checks for the same
#: reason.
_EDIT_HISTORY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_edit_history_date(value):
    """Return the ``date`` for an edit-history entry, or raise ValueError.

    Shared by ``EditHistoryListField`` (model layer) and
    ``EditHistoryItemSerializer`` (API layer) so the rule and its error text
    cannot drift apart.
    """
    if not isinstance(value, str) or not _EDIT_HISTORY_DATE_RE.match(value):
        raise ValueError(EDIT_HISTORY_DATE_ERROR)
    # The regex fixes the shape; the parser rejects impossible dates (2026-02-31).
    return date.fromisoformat(value)


class AuthorLinkListField(models.JSONField):
    """
    Stores an author's social links: a list of ``{type, value}`` entries.

    The ``type`` vocabulary deliberately mirrors ``ContactType`` in the
    frontend's ``src/data/team.ts``, so the author card and the team page speak
    the same language and can share icon rendering. ``email`` is NOT one of them
    — it is its own field on ``AuthorProfile``, because a personal address is
    worth handling (and withholding) separately from a public profile link.
    """

    #: Kept in sync with `ContactType` in src/data/team.ts.
    LINK_TYPES = frozenset(
        {"facebook", "instagram", "linkedin", "github", "website", "twitter"}
    )

    def __init__(self, *args, **kwargs):
        kwargs["default"] = list
        kwargs["blank"] = True
        super().__init__(*args, **kwargs)

    def validate(self, value, model_instance):
        """Validate that value is a list of known-type links with https URLs."""
        super().validate(value, model_instance)

        if not isinstance(value, list):
            raise ValidationError("Value must be a list")

        for entry in value:
            if not isinstance(entry, dict):
                raise ValidationError(f"Author link must be a dictionary: {entry}")

            for field in ("type", "value"):
                if field not in entry:
                    raise ValidationError(
                        f"Author link missing required field '{field}': {entry}"
                    )

            link_type = entry["type"]
            if link_type not in self.LINK_TYPES:
                raise ValidationError(
                    f"Unknown author link type '{link_type}'. Must be one of: "
                    f"{', '.join(sorted(self.LINK_TYPES))}"
                )

            url = entry["value"]
            if not isinstance(url, str) or not url.strip():
                raise ValidationError(
                    f"Author link value must be a non-empty string: {entry}"
                )
            # These render as outbound anchors on a public page; an unscheme'd or
            # javascript: value must not reach the DOM.
            if not url.startswith("https://"):
                raise ValidationError(
                    f"Author link value must be an https:// URL: {url}"
                )


class EditHistoryListField(models.JSONField):
    """
    Stores the case's PUBLIC edit history: a list of ``{date, remarks}`` entries.

    Each entry must have: date (AD ISO format), remarks (non-empty string).

    This is the caseworker-curated, publicly rendered counterpart to
    ``CaseStateChange`` — which is machine-written, carries moderator names and
    send-back reasons, and is gated for every state. The two are deliberately
    separate: a public "corrected the bigo figure" line is not the same record
    as "sent back to draft by <moderator>".
    """

    def __init__(self, *args, **kwargs):
        kwargs["default"] = list
        kwargs["blank"] = True
        super().__init__(*args, **kwargs)

    def validate(self, value, model_instance):
        """Validate that value is a list of valid edit-history entries."""
        super().validate(value, model_instance)

        if not isinstance(value, list):
            raise ValidationError("Value must be a list")

        for entry in value:
            if not isinstance(entry, dict):
                raise ValidationError(
                    f"Edit history entry must be a dictionary: {entry}"
                )

            for field in ("date", "remarks"):
                if field not in entry:
                    raise ValidationError(
                        f"Edit history entry missing required field '{field}': {entry}"
                    )

            date_str = entry["date"]
            try:
                parse_edit_history_date(date_str)
            except (ValueError, TypeError):
                raise ValidationError(f"{EDIT_HISTORY_DATE_ERROR}: {date_str}")

            if not isinstance(entry["remarks"], str) or not entry["remarks"].strip():
                raise ValidationError(
                    f"Edit history remarks must be a non-empty string: {entry}"
                )


