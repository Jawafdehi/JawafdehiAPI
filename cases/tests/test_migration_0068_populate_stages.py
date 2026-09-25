"""Migration 0068: the legacy date pair becomes one ``initial`` stage.

Runs the real migration with ``MigrationExecutor`` against historical model
state, because that is the only thing that proves the data migration works:
the concrete ``Case`` class has a ``save()`` that would derive the columns for
free, and a test written against it would pass with an empty migration.
"""

from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = [("cases", "0067_case_track_and_status_override")]
MIGRATE_TO = [("cases", "0068_populate_case_stages")]

IRI_SPECIAL = "https://jawafdehi.org/courtcase/special/078-CR-0048"
IRI_SUPREME = "https://jawafdehi.org/courtcase/supreme/079-RB-0123"
IRI_PATAN = "https://jawafdehi.org/courtcase/patan/080-CR-0007"


def _migrate(targets):
    """Run the migrations and return the app registry for the target state."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


@pytest.fixture
def old_apps():
    """Historical state just before 0068 — where the fixtures are written.

    ``transaction=True`` on every test in this module is load-bearing: sqlite
    refuses a schema editor inside an open atomic block, and the executor opens
    one even for a data-only migration. Leaves the database back at head so a
    later module never runs against an un-applied 0068.
    """
    yield _migrate(MIGRATE_FROM)
    _migrate(MIGRATE_TO)


def _case(apps, slug, **kwargs):
    Case = apps.get_model("cases", "Case")
    fields = {
        "title": f"Case {slug}",
        "offence_type": "CORRUPTION",
        "state": "DRAFT",
        "slug": slug,
    }
    fields.update(kwargs)
    return Case.objects.create(**fields)


def _docket(apps, case, iri, ordinal):
    Reference = apps.get_model("cases", "CaseCourtCaseReference")
    return Reference.objects.create(case=case, courtcase_iri=iri, ordinal=ordinal)


def _reload(apps, pk):
    return apps.get_model("cases", "Case").objects.get(pk=pk)


@pytest.mark.django_db(transaction=True)
def test_both_dates_become_one_initial_stage(old_apps):
    case = _case(
        old_apps,
        "both-dates",
        case_start_date=date(2021, 4, 12),
        case_end_date=date(2023, 11, 30),
    )

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates == {
        "stages": [{"stage": "initial", "start": "2021-04-12", "end": "2023-11-30"}]
    }
    assert migrated.proceedings_started_on == date(2021, 4, 12)
    assert migrated.proceedings_decided_on == date(2023, 11, 30)


@pytest.mark.django_db(transaction=True)
def test_the_legacy_columns_are_left_untouched(old_apps):
    case = _case(
        old_apps,
        "legacy-kept",
        case_start_date=date(2021, 4, 12),
        case_end_date=date(2023, 11, 30),
    )

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.case_start_date == date(2021, 4, 12)
    assert migrated.case_end_date == date(2023, 11, 30)


@pytest.mark.django_db(transaction=True)
def test_only_a_start_omits_the_end_key(old_apps):
    """An open stage, not a stage with ``end: null`` — and no decision date."""
    case = _case(old_apps, "start-only", case_start_date=date(2022, 1, 5))

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates == {"stages": [{"stage": "initial", "start": "2022-01-05"}]}
    assert migrated.proceedings_started_on == date(2022, 1, 5)
    assert migrated.proceedings_decided_on is None


@pytest.mark.django_db(transaction=True)
def test_only_an_end_omits_the_start_key(old_apps):
    """A verdict with no known registration date still carries the verdict."""
    case = _case(old_apps, "end-only", case_end_date=date(2022, 9, 9))

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates == {"stages": [{"stage": "initial", "end": "2022-09-09"}]}
    assert migrated.proceedings_started_on is None
    assert migrated.proceedings_decided_on == date(2022, 9, 9)


@pytest.mark.django_db(transaction=True)
def test_a_case_with_neither_date_gets_no_stage(old_apps):
    case = _case(old_apps, "no-dates")

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates == {"stages": []}
    assert migrated.proceedings_started_on is None
    assert migrated.proceedings_decided_on is None


@pytest.mark.django_db(transaction=True)
def test_a_docket_alone_invents_no_stage(old_apps):
    """A docket is not a date: without one there is nothing to say about when
    the proceeding ran, and an open ``initial`` stage would flip the derived
    status of thousands of drafts to ONGOING on no evidence."""
    case = _case(old_apps, "docket-no-dates")
    _docket(old_apps, case, IRI_SPECIAL, 0)

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates == {"stages": []}


@pytest.mark.django_db(transaction=True)
def test_the_stage_cites_the_ordinal_zero_docket(old_apps):
    """Several dockets, created out of order: the ordinal decides, not the pk."""
    case = _case(
        old_apps,
        "many-dockets",
        case_start_date=date(2020, 6, 1),
        case_end_date=date(2021, 6, 1),
    )
    _docket(old_apps, case, IRI_SUPREME, 2)
    _docket(old_apps, case, IRI_PATAN, 1)
    _docket(old_apps, case, IRI_SPECIAL, 0)

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates["stages"] == [
        {
            "stage": "initial",
            "start": "2020-06-01",
            "end": "2021-06-01",
            "courtcase_iri": IRI_SPECIAL,
        }
    ]


@pytest.mark.django_db(transaction=True)
def test_a_case_without_a_docket_gets_no_iri(old_apps):
    """The IRI map is per case — the 5 docket-less published cases must not
    pick up a neighbour's docket."""
    with_docket = _case(old_apps, "has-docket", case_start_date=date(2020, 1, 1))
    _docket(old_apps, with_docket, IRI_SPECIAL, 0)
    without = _case(old_apps, "has-none", case_start_date=date(2020, 1, 1))

    new_apps = _migrate(MIGRATE_TO)

    assert "courtcase_iri" not in _reload(new_apps, without.pk).dates["stages"][0]
    assert (
        _reload(new_apps, with_docket.pk).dates["stages"][0]["courtcase_iri"]
        == IRI_SPECIAL
    )


@pytest.mark.django_db(transaction=True)
def test_a_backwards_row_keeps_its_start_and_drops_only_the_bad_end(old_apps):
    """The stage a backwards row migrates to has to be one the model accepts.

    ``Case.save()`` validates the stage list unconditionally, and
    ``validate_stages`` refuses ``end`` before ``start`` -- so writing the pair
    verbatim would leave the row un-savable, and every state transition
    (submit/publish/soft-delete) would 422 on a field the caseworker never
    touched.

    The evidence is not destroyed: the legacy ``case_end_date`` column is
    untouched, and the row is named in the printed summary. The defect is
    never written to ``missing_details``, which the public CaseSerializer
    serves and which holds Nepali, reader-facing prose.
    """
    reader_note = "क) प्रतिवादीहरूले अदालतमा गरेको बयानको ब्याहोरा"
    case = _case(
        old_apps,
        "backwards",
        case_start_date=date(2023, 5, 20),
        case_end_date=date(2022, 2, 2),
        missing_details=reader_note,
    )

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates == {
        "stages": [{"stage": "initial", "start": "2023-05-20"}]
    }
    assert migrated.case_start_date == date(2023, 5, 20)
    assert migrated.case_end_date == date(2022, 2, 2)
    assert migrated.missing_details == reader_note


@pytest.mark.django_db(transaction=True)
def test_a_backwards_row_is_savable_through_the_real_model(old_apps):
    """The point of the rule above, asserted against the live model.

    Without it a caseworker gets ``stages[0].end: ... is before the stage
    start`` on a publish that never mentioned dates.
    """
    case = _case(
        old_apps,
        "backwards-savable",
        case_start_date=date(2023, 5, 20),
        case_end_date=date(2022, 2, 2),
    )
    _migrate(MIGRATE_TO)

    from cases.models import Case as LiveCase

    live = LiveCase.objects.get(pk=case.pk)
    live.save()  # must not raise

    live.refresh_from_db()
    assert live.dates == {"stages": [{"stage": "initial", "start": "2023-05-20"}]}


@pytest.mark.django_db(transaction=True)
def test_a_backwards_row_is_named_in_the_summary(old_apps, capsys):
    """Whoever runs the migration gets the rows to fix, not just a count."""
    case = _case(
        old_apps,
        "backwards-named",
        case_start_date=date(2023, 5, 20),
        case_end_date=date(2022, 2, 2),
    )
    _docket(old_apps, case, IRI_SPECIAL, 0)

    _migrate(MIGRATE_TO)

    out = capsys.readouterr().out
    assert "hold a decision date before" in out
    assert f"pk={case.pk} slug=backwards-named docket={IRI_SPECIAL}" in out


@pytest.mark.django_db(transaction=True)
def test_an_ordered_row_is_not_named_in_the_summary(old_apps, capsys):
    case = _case(
        old_apps,
        "ordered",
        case_start_date=date(2021, 4, 12),
        case_end_date=date(2023, 11, 30),
    )

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    out = capsys.readouterr().out
    assert "hold a decision date before" not in out
    assert f"pk={case.pk}" not in out
    assert not (migrated.missing_details or "")


@pytest.mark.django_db(transaction=True)
def test_the_out_of_order_listing_is_capped(old_apps, capsys):
    """A surprise on a big table must not flood the deploy log."""
    for n in range(22):
        _case(
            old_apps,
            f"backwards-{n:02d}",
            case_start_date=date(2023, 5, 20),
            case_end_date=date(2022, 2, 2),
        )

    _migrate(MIGRATE_TO)

    out = capsys.readouterr().out
    assert out.count("pk=") == 20
    assert "... and 2 more" in out


@pytest.mark.django_db(transaction=True)
def test_equal_dates_are_not_out_of_order(old_apps, capsys):
    """Charge sheet and verdict on one day is legal, not a defect."""
    case = _case(
        old_apps,
        "same-day",
        case_start_date=date(2021, 4, 12),
        case_end_date=date(2021, 4, 12),
    )

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert "hold a decision date before" not in capsys.readouterr().out
    assert migrated.proceedings_decided_on == date(2021, 4, 12)


@pytest.mark.django_db(transaction=True)
def test_a_case_that_already_has_stages_keeps_them(old_apps):
    """The compat layer writes stages on create and through PATCH, so by the
    time this runs a row can already hold a richer list than the legacy pair
    can express. Only the derived columns are refreshed."""
    already = {
        "stages": [
            {"stage": "initial", "start": "2019-01-01", "end": "2020-01-01"},
            {"stage": "appeal", "start": "2020-03-01"},
        ]
    }
    case = _case(
        old_apps,
        "already-staged",
        case_start_date=date(2019, 1, 1),
        case_end_date=date(2020, 1, 1),
        dates=already,
    )

    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates == already
    assert migrated.proceedings_started_on == date(2019, 1, 1)
    assert migrated.proceedings_decided_on is None


@pytest.mark.django_db(transaction=True)
def test_the_summary_is_printed(old_apps, capsys):
    _case(old_apps, "sum-a", case_start_date=date(2021, 1, 1))
    b = _case(old_apps, "sum-b", case_start_date=date(2021, 1, 1))
    _docket(old_apps, b, IRI_SPECIAL, 0)
    _case(
        old_apps,
        "sum-c",
        case_start_date=date(2023, 1, 1),
        case_end_date=date(2022, 1, 1),
    )
    _case(old_apps, "sum-d")

    _migrate(MIGRATE_TO)

    out = capsys.readouterr().out
    assert (
        "[0068] 3 case(s) given an initial stage; 1 cite a court case; "
        "0 already had stages" in out
    )
    assert "1 row(s)" in out and "hold a decision date before" in out


@pytest.mark.django_db(transaction=True)
def test_the_reverse_clears_the_new_columns_and_keeps_the_legacy_ones(old_apps):
    case = _case(
        old_apps,
        "reversible",
        case_start_date=date(2021, 4, 12),
        case_end_date=date(2023, 11, 30),
    )
    assert _reload(_migrate(MIGRATE_TO), case.pk).dates["stages"]

    reverted = _reload(_migrate(MIGRATE_FROM), case.pk)

    assert reverted.dates == {"stages": []}
    assert reverted.proceedings_started_on is None
    assert reverted.proceedings_decided_on is None
    assert reverted.case_start_date == date(2021, 4, 12)
    assert reverted.case_end_date == date(2023, 11, 30)


@pytest.mark.django_db(transaction=True)
def test_the_reverse_keeps_a_hand_entered_stage_list(old_apps):
    """The rows the forward pass left alone are not the reverse's to delete.

    A caseworker's appeal stage has no other copy anywhere; an unfiltered
    reverse would drop it on a rollback that was only meant to undo a
    backfill.
    """
    hand_edited = {
        "stages": [
            {"stage": "initial", "start": "2021-04-12", "end": "2023-11-30"},
            {"stage": "appeal", "start": "2024-01-15"},
        ]
    }
    kept = _case(
        old_apps,
        "hand-edited",
        case_start_date=date(2021, 4, 12),
        case_end_date=date(2023, 11, 30),
        dates=hand_edited,
    )
    backfilled = _case(old_apps, "backfilled", case_start_date=date(2022, 2, 2))
    _migrate(MIGRATE_TO)

    reverted_apps = _migrate(MIGRATE_FROM)

    assert _reload(reverted_apps, kept.pk).dates == hand_edited
    assert _reload(reverted_apps, backfilled.pk).dates == {"stages": []}


@pytest.mark.django_db(transaction=True)
def test_the_migration_is_re_appliable_after_a_reverse(old_apps):
    case = _case(old_apps, "round-trip", case_start_date=date(2021, 4, 12))

    _migrate(MIGRATE_TO)
    _migrate(MIGRATE_FROM)
    migrated = _reload(_migrate(MIGRATE_TO), case.pk)

    assert migrated.dates == {"stages": [{"stage": "initial", "start": "2021-04-12"}]}
