"""The proceeding-stage list: vocabulary, validation and the derived dates."""

from datetime import date

import pytest

from cases.stages import (
    COURT_STAGES,
    STAGE_VALUES,
    StageError,
    derived_proceeding_dates,
    validate_stages,
)


# ── vocabulary ───────────────────────────────────────────────────────────────


def test_the_vocabulary_is_closed():
    assert STAGE_VALUES == (
        "investigation",
        "initial",
        "appeal",
        "review",
        "other",
    )


def test_investigation_and_other_are_not_court_stages():
    """The derived dates count court stages only."""
    assert COURT_STAGES == frozenset({"initial", "appeal", "review"})


# ── validation ───────────────────────────────────────────────────────────────


def test_a_minimal_stage_validates():
    assert validate_stages([{"stage": "initial"}]) == [{"stage": "initial"}]


def test_an_unknown_stage_is_refused_not_dropped():
    """§9.1: a caseworker writing `first_instance` must be told, not ignored."""
    with pytest.raises(StageError) as exc:
        validate_stages([{"stage": "first_instance"}])
    assert "first_instance" in str(exc.value)


def test_an_unknown_key_is_refused():
    with pytest.raises(StageError) as exc:
        validate_stages([{"stage": "initial", "verdict": "acquitted"}])
    assert "verdict" in str(exc.value)


def test_other_requires_a_label():
    with pytest.raises(StageError):
        validate_stages([{"stage": "other"}])
    assert validate_stages([{"stage": "other", "label": "ICSID arbitration"}])


def test_a_label_is_only_for_other():
    with pytest.raises(StageError):
        validate_stages([{"stage": "initial", "label": "nope"}])


def test_end_before_start_is_refused():
    with pytest.raises(StageError) as exc:
        validate_stages([{"stage": "initial", "start": "2024-02-25", "end": "2024-01-17"}])
    assert "end" in str(exc.value)


def test_end_equal_to_start_is_allowed():
    """§8.8: the charge sheet closes the investigation and opens the court
    stage on the same day."""
    assert validate_stages(
        [{"stage": "investigation", "start": "2024-02-25", "end": "2024-02-25"}]
    )


def test_an_end_without_a_start_is_allowed():
    """§8.6: the verdict date is often known from a court order when the
    registration date is not."""
    assert validate_stages([{"stage": "initial", "end": "2025-08-13"}])


def test_stages_are_not_globally_ordered():
    """§8.5: after a remand the new first instance starts AFTER the appeal
    ended, and §8.2 means parallel stages are not linearly orderable at all."""
    assert validate_stages(
        [
            {"stage": "appeal", "start": "2025-04-25", "end": "2025-09-01"},
            {"stage": "initial", "start": "2025-10-01"},
        ]
    )


def test_a_courtcase_iri_must_be_one_of_the_case_binds():
    iri = "https://jawafdehi.org/courtcase/special/080-cr-0111"
    other = "https://jawafdehi.org/courtcase/supreme/081-cr-2130"

    assert validate_stages([{"stage": "initial", "courtcase_iri": iri}], binds=[iri])
    with pytest.raises(StageError):
        validate_stages([{"stage": "initial", "courtcase_iri": other}], binds=[iri])


def test_notes_are_capped():
    with pytest.raises(StageError):
        validate_stages([{"stage": "appeal", "notes": "x" * 501}])
    assert validate_stages([{"stage": "appeal", "notes": "मिसिल जलेको"}])


def test_the_forum_and_the_other_label_are_capped_too():
    """Both are public and both live in a JSONField, which bounds nothing."""
    with pytest.raises(StageError):
        validate_stages([{"stage": "investigation", "body": "अ" * 201}])
    with pytest.raises(StageError):
        validate_stages([{"stage": "other", "label": "x" * 201}])

    assert validate_stages(
        [{"stage": "investigation", "body": "अख्तियार दुरुपयोग अनुसन्धान आयोग"}]
    )
    assert validate_stages([{"stage": "other", "label": "राजस्व न्यायाधिकरण"}])


def test_the_list_itself_must_be_a_list_of_objects():
    with pytest.raises(StageError):
        validate_stages({"stage": "initial"})
    with pytest.raises(StageError):
        validate_stages(["initial"])


# ── derived dates ────────────────────────────────────────────────────────────


def test_the_start_is_the_earliest_court_stage():
    started, decided = derived_proceeding_dates(
        [
            {"stage": "investigation", "start": "2023-01-12", "end": "2024-02-25"},
            {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"},
        ]
    )
    assert started == date(2024, 2, 25), "investigation must not move the sort key"
    assert decided == date(2025, 8, 13)


def test_an_open_court_stage_leaves_the_decision_null():
    started, decided = derived_proceeding_dates(
        [
            {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"},
            {"stage": "appeal", "start": "2025-09-20"},
        ]
    )
    assert started == date(2024, 2, 25)
    assert decided is None


def test_parallel_first_instances_wait_for_the_last_one():
    """§8.2: status must not read concluded off the first docket to finish."""
    started, decided = derived_proceeding_dates(
        [
            {"stage": "initial", "start": "2024-01-10", "end": "2024-06-01"},
            {"stage": "initial", "start": "2024-01-10"},
        ]
    )
    assert started == date(2024, 1, 10)
    assert decided is None


def test_a_stage_without_a_start_is_skipped_for_the_start():
    started, decided = derived_proceeding_dates([{"stage": "initial", "end": "2025-08-13"}])
    assert started is None
    assert decided == date(2025, 8, 13)


def test_no_court_stage_means_both_are_null():
    started, decided = derived_proceeding_dates(
        [{"stage": "investigation", "start": "2023-01-12"}]
    )
    assert started is None and decided is None


def test_an_empty_list_is_null_not_an_error():
    assert derived_proceeding_dates([]) == (None, None)
