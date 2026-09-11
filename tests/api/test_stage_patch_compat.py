"""Writing stages through PATCH, and the deprecated date paths the SPA sends.

The deployed caseworker admin emits ``/case_start_date`` and ``/case_end_date``.
Those addressed columns; the case's dates now live inside a stage record, and
RFC-6902 cannot rewrite a path onto a field inside a list that may not exist.
So the two paths are transformed rather than rewritten, and the requirement is
that the admin never loses date editing.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"


def _case(**kwargs) -> Case:
    defaults = dict(
        title="Stage patch case",
        offence_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="d",
        short_description="s",
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _client():
    client = APIClient()
    client.force_authenticate(user=create_user_with_role(
        f"cw{_client.n}", f"cw{_client.n}@example.com", "Caseworker"))
    _client.n += 1
    return client


_client.n = 0


def _patch(case, ops):
    return _client().patch(URL.format(case.slug), ops, format="json")


# ── the new path ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_patching_dates_persists_the_stage_list():
    case = _case()

    response = _patch(case, [{
        "op": "replace",
        "path": "/dates",
        "value": {"stages": [
            {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
        ]},
    }])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.dates["stages"][0]["stage"] == "initial"


@pytest.mark.django_db
def test_patching_dates_recomputes_the_derived_columns():
    """The scalar write path is a bulk UPDATE that skips save(), so the derived
    columns have to be recomputed explicitly or they silently misorder the
    archive on every date edit."""
    case = _case()

    _patch(case, [{
        "op": "replace",
        "path": "/dates",
        "value": {"stages": [
            {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
        ]},
    }])

    case.refresh_from_db()
    assert str(case.proceedings_started_on) == "2024-02-25"
    assert str(case.proceedings_decided_on) == "2025-08-13"


@pytest.mark.django_db
def test_an_open_appeal_clears_the_derived_decision_through_patch():
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
    ]})

    _patch(case, [{
        "op": "replace",
        "path": "/dates",
        "value": {"stages": [
            {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"},
            {"stage": "appeal", "start": "2025-09-20"},
        ]},
    }])

    case.refresh_from_db()
    assert case.proceedings_decided_on is None


@pytest.mark.django_db
def test_an_unknown_stage_is_refused_with_422_naming_it():
    case = _case()

    response = _patch(case, [{
        "op": "replace", "path": "/dates",
        "value": {"stages": [{"stage": "first_instance"}]},
    }])

    assert response.status_code == 422, response.data
    assert "first_instance" in str(response.data)


# ── the deprecated paths ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_deprecated_start_path_updates_the_initial_stage():
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
    ]})

    response = _patch(case, [
        {"op": "replace", "path": "/case_start_date", "value": "2024-03-01"}
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.dates["stages"][0]["start"] == "2024-03-01"
    assert case.dates["stages"][0]["end"] == "2025-08-13"
    assert str(case.proceedings_started_on) == "2024-03-01"


@pytest.mark.django_db
def test_the_deprecated_path_creates_the_stage_when_there_is_none():
    """RFC-6902 replace on a missing path is an error, which is exactly why
    this is a transform and not a path rewrite."""
    case = _case()

    response = _patch(case, [
        {"op": "replace", "path": "/case_start_date", "value": "2024-02-25"}
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.dates["stages"] == [{"stage": "initial", "start": "2024-02-25"}]


@pytest.mark.django_db
def test_the_deprecated_path_can_clear_a_date():
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
    ]})

    response = _patch(case, [
        {"op": "replace", "path": "/case_end_date", "value": None}
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert "end" not in case.dates["stages"][0]
    assert case.proceedings_decided_on is None


@pytest.mark.django_db
def test_the_deprecated_path_is_refused_when_the_case_has_several_first_instances():
    """A case with parallel first instances cannot be expressed by the old
    form at all, so guessing which stage it meant would corrupt data."""
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-01-10"},
        {"stage": "initial", "start": "2024-01-10"},
    ]})

    response = _patch(case, [
        {"op": "replace", "path": "/case_start_date", "value": "2024-03-01"}
    ])

    assert response.status_code == 422, response.data
    assert "case_start_date" in response.data


@pytest.mark.django_db
def test_a_backwards_deprecated_write_is_refused():
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
    ]})

    response = _patch(case, [
        {"op": "replace", "path": "/case_end_date", "value": "2020-01-01"}
    ])

    assert response.status_code == 422, response.data


# ── the read aliases ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_read_aliases_come_from_the_initial_stage():
    from cases.serializers import CaseSerializer

    case = _case(dates={"stages": [
        {"stage": "investigation", "start": "2023-01-12", "end": "2024-02-25"},
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"},
    ]})

    data = CaseSerializer(case).data

    assert data["case_start_date"] == "2024-02-25", "investigation must not leak"
    assert data["case_end_date"] == "2025-08-13"


@pytest.mark.django_db
def test_the_read_aliases_are_null_without_a_first_instance():
    from cases.serializers import CaseSerializer

    data = CaseSerializer(_case()).data

    assert data["case_start_date"] is None
    assert data["case_end_date"] is None


@pytest.mark.django_db
def test_creating_with_the_deprecated_dates_lands_a_first_instance_stage():
    """Otherwise the case reads as having no proceedings at all and sorts by
    created_at, which is routinely wrong by months."""
    from cases.caseworker_serializers import CaseCreateSerializer

    serializer = CaseCreateSerializer(
        data={
            "title": "Created with old dates",
            "offence_type": CaseType.CORRUPTION,
            "case_start_date": "2024-02-25",
            "case_end_date": "2025-08-13",
        }
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["dates"] == {
        "stages": [{"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}]
    }


@pytest.mark.django_db
def test_creating_with_backwards_deprecated_dates_is_refused():
    from cases.caseworker_serializers import CaseCreateSerializer

    serializer = CaseCreateSerializer(
        data={
            "title": "Backwards",
            "offence_type": CaseType.CORRUPTION,
            "case_start_date": "2024-02-25",
            "case_end_date": "2020-01-01",
        }
    )

    assert not serializer.is_valid()
    assert "case_end_date" in serializer.errors


# ── the shape the live court-record enricher actually sends ──────────────────
#
# ``casework.enrich_court_record`` writes both dates in ONE conditional PATCH
# (``CaseworkApi.patch_case`` batches them precisely so the second request
# cannot 412 on the ETag the first one moved). Both land on the same stage, so
# the transform is order-sensitive in a way patching one date at a time is not.


@pytest.mark.django_db
def test_both_deprecated_dates_in_one_patch_land_on_one_stage():
    case = _case()

    response = _patch(case, [
        {"op": "replace", "path": "/case_start_date", "value": "2023-06-22"},
        {"op": "replace", "path": "/case_end_date", "value": "2024-06-04"},
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    stages = case.dates["stages"]
    assert len(stages) == 1, "one write, one first instance -- not one per date"
    assert stages[0] == {
        "stage": "initial", "start": "2023-06-22", "end": "2024-06-04"}
    assert str(case.proceedings_started_on) == "2023-06-22"
    assert str(case.proceedings_decided_on) == "2024-06-04"


@pytest.mark.django_db
def test_a_start_that_precedes_an_existing_end_is_validated_after_both_apply():
    """The enricher fills only what is empty, so this shape is reachable.

    A case migrated from an end date alone (0068 emits a stage with ``end``
    and no ``start``) gets its start written later. Validating each date as it
    is applied would reject the batch on the intermediate state -- a start of
    2023 against a stale end of 2020 -- even though the state the client asked
    for is ordered and valid. Both apply, then the document is validated once.
    """
    case = _case()
    _patch(case, [{"op": "replace", "path": "/case_end_date", "value": "2020-01-01"}])

    response = _patch(case, [
        {"op": "replace", "path": "/case_start_date", "value": "2023-06-22"},
        {"op": "replace", "path": "/case_end_date", "value": "2024-06-04"},
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.dates["stages"] == [
        {"stage": "initial", "start": "2023-06-22", "end": "2024-06-04"}]


# ── the court-case bind and the stage citing it, in one PATCH ────────────────

SPECIAL_IRI = "https://jawafdehi.org/courtcase/special/081-cr-0060"
SUPREME_IRI = "https://jawafdehi.org/courtcase/supreme/081-ns-1234"


@pytest.mark.django_db
def test_a_bind_and_a_stage_citing_it_save_together():
    """The admin sends /court_cases and /dates in ONE ops array.

    Validating the stage against the binds as they stand BEFORE the patch
    rejects the save with "... is not one of this case's court_cases",
    naming an IRI that is in the very patch the caseworker just sent.
    """
    case = _case()

    response = _patch(case, [
        {"op": "replace", "path": "/court_cases", "value": [SPECIAL_IRI]},
        {"op": "replace", "path": "/dates", "value": {"stages": [
            {"stage": "initial", "start": "2022-01-10",
             "courtcase_iri": SPECIAL_IRI},
        ]}},
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.court_cases == [SPECIAL_IRI]
    assert case.dates["stages"][0]["courtcase_iri"] == SPECIAL_IRI


@pytest.mark.django_db
def test_unbinding_a_court_case_a_stage_still_cites_is_refused():
    """The reverse, which is the worse half.

    Validating against the OLD binds lets the unbind through, the join is
    rewritten, and the case keeps a stage pointing at a docket it no longer
    holds. Nothing fails until the next ``save()`` -- i.e. the next state
    transition -- so the case becomes unpublishable at a point that says
    nothing about what caused it.
    """
    case = _case()
    _patch(case, [
        {"op": "replace", "path": "/court_cases", "value": [SPECIAL_IRI]},
        {"op": "replace", "path": "/dates", "value": {"stages": [
            {"stage": "initial", "start": "2022-01-10",
             "courtcase_iri": SPECIAL_IRI},
        ]}},
    ])

    response = _patch(case, [
        {"op": "replace", "path": "/court_cases", "value": [SUPREME_IRI]},
    ])

    assert response.status_code == 422, response.data
    assert "dates" in response.data
    case.refresh_from_db()
    assert case.court_cases == [SPECIAL_IRI], "the unbind must not have applied"


@pytest.mark.django_db
def test_unbinding_a_court_case_no_stage_cites_still_works():
    """The guard must not block an ordinary unbind."""
    case = _case()
    _patch(case, [
        {"op": "replace", "path": "/court_cases",
         "value": [SPECIAL_IRI, SUPREME_IRI]},
        {"op": "replace", "path": "/dates", "value": {"stages": [
            {"stage": "initial", "start": "2022-01-10",
             "courtcase_iri": SPECIAL_IRI},
        ]}},
    ])

    response = _patch(case, [
        {"op": "replace", "path": "/court_cases", "value": [SPECIAL_IRI]},
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.court_cases == [SPECIAL_IRI]


# ── create ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_creating_with_a_stage_list_persists_it():
    """POST must take what PATCH takes.

    Without this the stage editor cannot appear on the create form at all --
    only the two deprecated date inputs can, which is precisely the shape this
    rework exists to retire. A caseworker would have to create the case, save,
    and reopen it before recording that it went to appeal.
    """
    from cases.caseworker_serializers import CaseCreateSerializer

    serializer = CaseCreateSerializer(data={
        "title": "Created with stages",
        "offence_type": CaseType.CORRUPTION,
        "dates": {"stages": [
            {"stage": "investigation", "start": "2023-01-12", "end": "2024-02-25"},
            {"stage": "initial", "start": "2024-02-25"},
        ]},
    })

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["dates"]["stages"][1]["stage"] == "initial"


@pytest.mark.django_db
def test_creating_with_an_unknown_stage_is_refused():
    from cases.caseworker_serializers import CaseCreateSerializer

    serializer = CaseCreateSerializer(data={
        "title": "Bad stage",
        "offence_type": CaseType.CORRUPTION,
        "dates": {"stages": [{"stage": "first_instance"}]},
    })

    assert not serializer.is_valid()
    assert "dates" in serializer.errors


@pytest.mark.django_db
def test_posting_a_stage_list_is_accepted_by_the_create_view():
    """The serializer taking it is not enough.

    The create view rejects any key absent from ``CaseCreateSerializer().fields``
    with "This field is not allowed", so a field that validates fine can still
    be refused at the door. ``dates`` is inherited from
    ``CaseWriteFieldsSerializer`` rather than declared on the create serializer,
    which makes it easy to read the class and conclude it is not accepted.
    """
    response = _client().post(
        "/api/cases/",
        {
            "title": "Posted with stages",
            "offence_type": CaseType.CORRUPTION,
            "description": "d",
            "short_description": "s",
            "dates": {"stages": [{"stage": "initial", "start": "2024-02-25"}]},
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    case = Case.objects.get(pk=response.data["id"])
    assert case.dates["stages"] == [{"stage": "initial", "start": "2024-02-25"}]
    assert str(case.proceedings_started_on) == "2024-02-25"


@pytest.mark.django_db
def test_posting_the_deprecated_dates_lands_a_stage_through_the_view():
    """The serializer folds them into an `initial` stage; the view must keep it.

    Otherwise the case is created with the legacy COLUMN set and no stage, and
    the read alias -- which reads off the stage list -- serves null for a date
    the caseworker just typed.
    """
    response = _client().post(
        "/api/cases/",
        {
            "title": "Posted with old dates",
            "offence_type": CaseType.CORRUPTION,
            "description": "d",
            "short_description": "s",
            "case_start_date": "2024-02-25",
            "case_end_date": "2025-08-13",
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    case = Case.objects.get(pk=response.data["id"])
    assert case.dates["stages"] == [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}]
    assert str(case.proceedings_decided_on) == "2025-08-13"


@pytest.mark.django_db
def test_posting_a_track_and_an_override_keeps_them():
    """Same allowlist, same silent drop."""
    response = _client().post(
        "/api/cases/",
        {
            "title": "Posted with a track",
            "offence_type": CaseType.CORRUPTION,
            "description": "d",
            "short_description": "s",
            "case_track": "ciaa",
            "status_override": "dormant",
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    case = Case.objects.get(pk=response.data["id"])
    assert case.case_track == "ciaa"
    assert case.status_override == "dormant"


@pytest.mark.django_db
def test_patching_the_track_and_the_override_persists_them():
    """Both are Case columns added by this rework, and neither was reachable.

    A field missing from `_PATCH_SCALAR_FIELDS` validates, returns 200, and is
    then dropped when the bulk UPDATE is assembled -- the same silent drop the
    list already records for `notes` (BB-28). The model had the columns, the
    index read them and the frontend typed them, but no client could set one.
    """
    case = _case()

    response = _patch(case, [
        {"op": "replace", "path": "/case_track", "value": "ciaa"},
        {"op": "replace", "path": "/status_override", "value": "dormant"},
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.case_track == "ciaa"
    assert case.status_override == "dormant"
    assert case.status == "dormant", "the override must win over the derivation"


@pytest.mark.django_db
def test_an_unknown_track_is_refused():
    case = _case()

    response = _patch(case, [
        {"op": "replace", "path": "/case_track", "value": "not_a_track"}
    ])

    assert response.status_code == 422, response.data
    assert "case_track" in response.data
