"""``Case.dates`` and the two derived columns it maintains."""

from datetime import date

import pytest
from django.core.exceptions import ValidationError

from cases.models import Case, CaseState, CaseType


def _case(**kwargs) -> Case:
    defaults = dict(
        title="Dates case", offence_type=CaseType.CORRUPTION, state=CaseState.DRAFT
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


@pytest.mark.django_db
def test_a_new_case_has_an_empty_stage_list():
    case = _case()

    assert case.dates == {"stages": []}
    assert case.proceedings_started_on is None
    assert case.proceedings_decided_on is None


@pytest.mark.django_db
def test_saving_recomputes_the_derived_columns():
    case = _case(
        dates={
            "stages": [
                {"stage": "investigation", "start": "2023-01-12", "end": "2024-02-25"},
                {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"},
            ]
        }
    )

    case.refresh_from_db()
    assert case.proceedings_started_on == date(2024, 2, 25)
    assert case.proceedings_decided_on == date(2025, 8, 13)


@pytest.mark.django_db
def test_a_pending_appeal_clears_the_decision_date():
    case = _case(
        dates={"stages": [{"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}]}
    )
    assert case.proceedings_decided_on == date(2025, 8, 13)

    case.dates["stages"].append({"stage": "appeal", "start": "2025-09-20"})
    case.save()

    case.refresh_from_db()
    assert case.proceedings_decided_on is None
    assert case.proceedings_started_on == date(2024, 2, 25)


@pytest.mark.django_db
def test_an_invalid_stage_is_refused_on_save():
    with pytest.raises(ValidationError) as exc:
        _case(dates={"stages": [{"stage": "first_instance"}]})
    assert "first_instance" in str(exc.value)


@pytest.mark.django_db
def test_a_backwards_stage_is_refused_on_save():
    with pytest.raises(ValidationError):
        _case(dates={"stages": [{"stage": "initial", "start": "2020-02-25", "end": "2020-01-17"}]})


@pytest.mark.django_db
def test_a_legacy_backwards_row_still_reads_and_still_derives():
    """§8.12: the two production rows with a verdict before the registration
    are migrated unchanged and flagged, so the derived columns must be a pure
    function that tolerates them rather than a second place they can fail."""
    case = _case()
    Case.objects.filter(pk=case.pk).update(
        dates={"stages": [{"stage": "initial", "start": "2020-02-25", "end": "2020-01-17"}]}
    )

    case.refresh_from_db()
    assert case.dates["stages"][0]["end"] == "2020-01-17"
    from cases.stages import derived_proceeding_dates

    assert derived_proceeding_dates(case.dates["stages"]) == (
        date(2020, 2, 25),
        date(2020, 1, 17),
    )


@pytest.mark.django_db
def test_a_stage_iri_must_be_one_of_the_case_binds():
    case = _case()
    case.dates = {
        "stages": [
            {
                "stage": "initial",
                "courtcase_iri": "https://jawafdehi.org/courtcase/special/080-cr-0111",
            }
        ]
    }

    with pytest.raises(ValidationError):
        case.save()
