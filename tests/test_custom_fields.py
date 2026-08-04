"""
Property-based tests for custom list field validation.

Feature: accountability-platform-core
Property 2: Draft validation is lenient, In Review validation is strict
Validates: Requirements 1.2
"""

import pytest
from django.core.exceptions import ValidationError
from hypothesis import given, settings

from cases.fields import HttpsURLField, TextListField, TimelineListField
from tests.strategies import text_list, timeline_list

# ============================================================================
# TextListField Tests
# ============================================================================


@settings(max_examples=100)
@given(texts=text_list(min_size=1, max_size=10))
def test_text_list_field_accepts_valid_text_list(texts):
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient, In Review validation is strict

    For any list of text strings, TextListField should accept them without raising ValidationError.
    """
    field = TextListField()

    # Should not raise ValidationError
    try:
        field.validate(texts, None)
    except ValidationError:
        pytest.fail(f"TextListField rejected valid text list: {texts}")


def test_text_list_field_rejects_empty_strings():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient, In Review validation is strict

    TextListField should reject lists containing empty strings.
    """
    field = TextListField()

    with pytest.raises(ValidationError):
        field.validate(["valid text", "", "another valid"], None)


def test_text_list_field_rejects_non_string_items():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient, In Review validation is strict

    TextListField should reject lists containing non-string items.
    """
    field = TextListField()

    with pytest.raises(ValidationError):
        field.validate(["valid text", 123, "another valid"], None)


# ============================================================================
# TimelineListField Tests
# ============================================================================


@settings(max_examples=100)
@given(timeline=timeline_list(min_size=0, max_size=10))
def test_timeline_list_field_accepts_valid_timeline(timeline):
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient, In Review validation is strict

    For any list of valid timeline entries, TimelineListField should accept them without raising ValidationError.
    """
    field = TimelineListField()

    # Should not raise ValidationError
    try:
        field.validate(timeline, None)
    except ValidationError:
        pytest.fail(f"TimelineListField rejected valid timeline: {timeline}")


def test_timeline_list_field_rejects_missing_required_fields():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient, In Review validation is strict

    TimelineListField should reject entries missing required fields (date, title).
    Description is optional.
    """
    field = TimelineListField()

    # Missing 'date'
    with pytest.raises(ValidationError):
        field.validate([{"title": "Event", "description": "Description"}], None)

    # Missing 'title'
    with pytest.raises(ValidationError):
        field.validate([{"date": "2024-01-01", "description": "Description"}], None)


def test_timeline_list_field_rejects_invalid_date_format():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient, In Review validation is strict

    TimelineListField should reject entries with invalid date formats.
    """
    field = TimelineListField()

    with pytest.raises(ValidationError):
        field.validate(
            [{"date": "invalid-date", "title": "Event", "description": "Description"}],
            None,
        )


def test_timeline_list_field_accepts_missing_description():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient, In Review validation is strict

    TimelineListField should accept entries without description (description is optional).
    """
    field = TimelineListField()

    # Should not raise ValidationError
    try:
        field.validate([{"date": "2024-01-01", "title": "Event"}], None)
    except ValidationError as e:
        pytest.fail(
            f"TimelineListField should accept missing description, but raised: {e}"
        )


def test_timeline_list_field_accepts_empty_description():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient, In Review validation is strict

    TimelineListField should accept entries with empty description (description is optional).
    """
    field = TimelineListField()

    # Should not raise ValidationError
    try:
        field.validate(
            [{"date": "2024-01-01", "title": "Event", "description": ""}], None
        )
    except ValidationError as e:
        pytest.fail(
            f"TimelineListField should accept empty description, but raised: {e}"
        )


def test_timeline_list_field_accepts_date_bs():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient

    TimelineListField should accept an optional date_bs (Bikram Sambat) field
    when shaped as YYYY-MM-DD.
    """
    field = TimelineListField()

    try:
        field.validate(
            [{"date": "2025-02-09", "date_bs": "2081-10-27", "title": "मुद्दा दर्ता"}],
            None,
        )
    except ValidationError as e:
        pytest.fail(f"TimelineListField should accept valid date_bs, but raised: {e}")


def test_timeline_list_field_rejects_malformed_date_bs():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient

    TimelineListField should reject a date_bs that is not a YYYY-MM-DD string.
    """
    field = TimelineListField()

    with pytest.raises(ValidationError):
        field.validate(
            [{"date": "2025-02-09", "date_bs": "2081/10/27", "title": "Event"}],
            None,
        )

    with pytest.raises(ValidationError):
        field.validate(
            [{"date": "2025-02-09", "date_bs": 20811027, "title": "Event"}],
            None,
        )


def test_timeline_list_field_accepts_end_date_span():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient

    TimelineListField should accept an optional end_date for events that span a
    period, as long as it is on or after the start date.
    """
    field = TimelineListField()

    try:
        field.validate(
            [
                {
                    "date": "1989-07-14",
                    "date_bs": "2046-03-30",
                    "end_date": "2020-07-15",
                    "end_date_bs": "2077-03-31",
                    "title": "जाँच अवधि",
                }
            ],
            None,
        )
    except ValidationError as e:
        pytest.fail(f"TimelineListField should accept valid end_date, but raised: {e}")


def test_timeline_list_field_rejects_malformed_end_date_bs():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient

    TimelineListField should reject an end_date_bs that is not a YYYY-MM-DD
    Bikram Sambat string.
    """
    field = TimelineListField()

    with pytest.raises(ValidationError):
        field.validate(
            [
                {
                    "date": "1989-07-14",
                    "end_date": "2020-07-15",
                    "end_date_bs": "2077/03/31",
                    "title": "Event",
                }
            ],
            None,
        )


def test_timeline_list_field_rejects_end_date_before_date():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient

    TimelineListField should reject an end_date that is before the start date.
    """
    field = TimelineListField()

    with pytest.raises(ValidationError):
        field.validate(
            [{"date": "2020-07-15", "end_date": "1989-07-14", "title": "Event"}],
            None,
        )


def test_timeline_list_field_rejects_malformed_end_date():
    """
    Feature: accountability-platform-core, Property 2: Draft validation is lenient

    TimelineListField should reject an end_date that is not an ISO date string.
    """
    field = TimelineListField()

    with pytest.raises(ValidationError):
        field.validate(
            [{"date": "2020-07-15", "end_date": "not-a-date", "title": "Event"}],
            None,
        )


# ============================================================================
# HttpsURLField Tests
# ============================================================================
#
# The whole point of this subclass is the ``assume_scheme`` it forwards at the
# FORM layer -- that is what stops Django 5.x emitting RemovedInDjango60Warning
# for every ModelForm over a URLField, which was ~98% of this suite's warning
# volume. Nothing else asserts it, and a silent regression here (dropping the
# formfield override, or the migration drifting back to models.URLField) would
# only show up as warning noise creeping back in.


def test_https_url_field_formfield_pins_assume_scheme():
    """The form field assumes ``https``, which is what silences the deprecation."""
    assert HttpsURLField().formfield().assume_scheme == "https"


def test_https_url_field_prepends_https_to_a_scheme_less_value():
    """A scheme-less value cleans to https://, i.e. Django 6.0 behaviour today."""
    assert (
        HttpsURLField().formfield().clean("example.com/banner.png")
        == "https://example.com/banner.png"
    )
    # An explicit scheme is left alone -- only the ABSENT scheme is filled in.
    assert (
        HttpsURLField().formfield().clean("http://example.com/x.png")
        == "http://example.com/x.png"
    )


def test_https_url_field_caller_can_still_override_assume_scheme():
    """``assume_scheme`` is a default, not a lock -- the ``**kwargs`` order matters."""
    assert HttpsURLField().formfield(assume_scheme="http").assume_scheme == "http"


def test_https_url_field_is_a_database_level_noop():
    """``deconstruct()`` is inherited, so the column is an ordinary URLField.

    This is why migration 0055 is state-only: same path-independent kwargs, same
    ``varchar``. If this subclass ever grows a ``deconstruct()`` or a different
    ``db_type``, that migration stops being a no-op and needs revisiting.
    """
    from django.db import connection, models

    _, _, _, kwargs = HttpsURLField(max_length=500, blank=True).deconstruct()
    _, _, _, plain = models.URLField(max_length=500, blank=True).deconstruct()
    assert kwargs == plain
    assert HttpsURLField(max_length=500).db_type(connection) == models.URLField(
        max_length=500
    ).db_type(connection)
