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


# ── what the deprecated columns actually hold ────────────────────────────────


def test_the_deprecated_date_columns_do_not_claim_to_be_incident_dates():
    """They hold COURT dates, and the help text said otherwise for years.

    Measured 2026-09-10 against NGM over ten published CIAA cases:
    ``case_start_date`` equals the special court's registration date on seven
    exactly and is one day out on three (one batch, one press release).
    Nothing resembling an incident date, which for these is years earlier.
    ``casework.enrich_court_record`` agrees from the writing side -- it
    derives the value FROM court registration dates.

    This matters because the wrong text was load-bearing: it is what the admin
    form shows caseworkers and what OpenAPI publishes, and reasoning from it
    produced a false claim in the search indexer during this very rework. The
    assertion is on the WORD, not the sentence, so rewording stays free while
    reinstating the incident reading does not.
    """
    for name in ("case_start_date", "case_end_date"):
        help_text = Case._meta.get_field(name).help_text
        assert "incident" not in help_text.lower(), (
            f"{name}: holds the court registration/decision date, not an "
            "incident date -- see measurements.md M5"
        )
        assert "deprecated" in help_text.lower(), (
            f"{name}: superseded by dates.stages; say so where a caseworker "
            "reads it"
        )
