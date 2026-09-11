# Populate ``Case.dates`` from the legacy ``case_start_date`` / ``case_end_date``
# pair. Data only: the two columns stay for the compatibility release, which
# serves them as aliases off the single ``initial`` stage.
#
# Three decisions worth knowing before editing this file:
#
# 1. A date makes a stage; a docket does not. An ``initial`` record with no
#    ``end`` reads as an open court proceeding, so seeding one from a bare
#    docket would flip the derived status of every dateless draft to ONGOING on
#    no evidence.
# 2. Rows whose decision date precedes their registration date keep the
#    registration date and DROP the impossible decision date from the stage.
#    Writing the pair verbatim would produce a record ``validate_stages``
#    refuses, and ``Case.save()`` validates unconditionally -- so submit,
#    publish and the soft-delete would all 422 on a field the caseworker never
#    touched, while the scalar-edit path (``queryset.update()``) kept working.
#    No evidence is destroyed: ``case_end_date`` is not touched by this
#    migration or by anything after it, so the entered date is still on the
#    row, and the correction needs the court order, which this migration
#    cannot read. Those rows are NAMED in the printed summary, never written
#    to the case: every text field on Case -- ``missing_details`` and
#    ``notes`` included -- is served by the public serializer, and both hold
#    Nepali prose written for readers.
# 3. One stage per case regardless of how many dockets it cites. The
#    multi-docket cases (one published case cites 12 across 8 courts) are a
#    per-case human decision, not a rule in a script.
#
# ``cases.stages`` is a pure module with no model imports, so importing it here
# is safe -- and duplicating the derivation would let the two drift.

from django.db import migrations
from django.db.models import F, Q

from cases.stages import STAGE_INITIAL, derived_proceeding_dates

BATCH_SIZE = 500

#: How many out-of-order rows the summary names before it truncates. A
#: surprise on a big table must not flood the deploy log.
SUMMARY_LIST_CAP = 20

UPDATED_FIELDS = ["dates", "proceedings_started_on", "proceedings_decided_on"]


def _primary_iris(Reference, db):
    """``{case_id: courtcase_iri}`` for each case's first-listed docket.

    Lowest ordinal wins, which is the ordinal-0 reference on every case that
    has one (0046 numbered from 0 and ``_sync_courtcase_references`` still
    enumerates from 0). Falling back to the lowest rather than requiring
    literally 0 costs nothing and keeps a hand-inserted row from silently
    losing its IRI.
    """
    primary = {}
    rows = (
        Reference.objects.using(db)
        .order_by("case_id", "ordinal", "created_at", "pk")
        .values_list("case_id", "courtcase_iri")
        .iterator(chunk_size=BATCH_SIZE)
    )
    for case_id, iri in rows:
        primary.setdefault(case_id, iri)
    return primary


def _legacy_stage(case, iri):
    """The ONE stage record this migration derives from a row's legacy pair.

    Shared with the reverse, which uses it to recognise its own work: a row
    whose stage list is exactly this is one the forward pass wrote, and a row
    whose list is anything else was edited by hand and must survive.
    """
    stage = {"stage": STAGE_INITIAL}
    if case.case_start_date:
        stage["start"] = case.case_start_date.isoformat()
    # Decision #2: an end before the start is a record the model refuses, so
    # it is left off the stage rather than written and then hit on the next
    # save. The column keeps it.
    if case.case_end_date and not (
        case.case_start_date and case.case_end_date < case.case_start_date
    ):
        stage["end"] = case.case_end_date.isoformat()
    if iri:
        stage["courtcase_iri"] = iri
    return stage


def populate_stages(apps, schema_editor):
    Case = apps.get_model("cases", "Case")
    Reference = apps.get_model("cases", "CaseCourtCaseReference")
    db = schema_editor.connection.alias

    primary_iri = _primary_iris(Reference, db)
    out_of_order = set(
        Case.objects.using(db)
        .filter(case_end_date__lt=F("case_start_date"))
        .values_list("pk", flat=True)
    )

    created = with_iri = kept = 0
    backwards = []
    batch = []

    dated = (
        Case.objects.using(db)
        .filter(Q(case_start_date__isnull=False) | Q(case_end_date__isnull=False))
        .only("pk", "slug", "case_start_date", "case_end_date", "dates")
        .order_by("pk")
    )
    for case in dated.iterator(chunk_size=BATCH_SIZE):
        if case.pk in out_of_order:
            docket = primary_iri.get(case.pk) or "none"
            backwards.append(f"pk={case.pk} slug={case.slug} docket={docket}")

        stored = case.dates if isinstance(case.dates, dict) else {}
        stages = stored.get("stages")

        if stages:
            # The compatibility layer writes stages on create and through
            # PATCH, so a row can already carry a list this backfill would
            # only impoverish -- an appeal stage a caseworker entered by hand,
            # for one. It is left exactly as it is; only the two derived
            # columns are refreshed, which the scalar-edit path
            # (`Case.objects.filter().update()`) is known to leave stale.
            kept += 1
        else:
            stage = _legacy_stage(case, primary_iri.get(case.pk))
            if "courtcase_iri" in stage:
                with_iri += 1

            stages = [stage]
            case.dates = {"stages": stages}
            created += 1

        started, decided = derived_proceeding_dates(stages)
        case.proceedings_started_on = started
        case.proceedings_decided_on = decided

        batch.append(case)
        if len(batch) >= BATCH_SIZE:
            Case.objects.using(db).bulk_update(batch, UPDATED_FIELDS)
            batch = []

    if batch:
        Case.objects.using(db).bulk_update(batch, UPDATED_FIELDS)

    # Migrations run before logging is configured, so these are prints.
    print(
        f"  [0068] {created} case(s) given an initial stage; "
        f"{with_iri} cite a court case; {kept} already had stages"
    )
    if backwards:
        print(
            f"  [0068] {len(backwards)} row(s) hold a decision date before "
            "the registration date; the stage keeps the registration date "
            "only, and case_end_date still holds the entered value:"
        )
        for line in backwards[:SUMMARY_LIST_CAP]:
            print(f"  [0068]   {line}")
        if len(backwards) > SUMMARY_LIST_CAP:
            print(f"  [0068]   ... and {len(backwards) - SUMMARY_LIST_CAP} more")


def clear_stages(apps, schema_editor):
    """Drop back to an empty stage document -- on the rows THIS wrote, only.

    An unfiltered update would delete stage lists the forward pass
    deliberately left alone: the appeal a caseworker entered by hand, the
    second first instance on a remanded case. Those are not this migration's
    to remove, and nothing else would bring them back.

    A row is recognised as this migration's work when its stage list is
    exactly the one record ``_legacy_stage`` derives from that row's legacy
    columns. Anything else -- an added stage, an edited date, a note -- is a
    human edit and is left intact.

    ``case_start_date`` / ``case_end_date`` are never touched going forward,
    so there is nothing to restore.
    """
    Case = apps.get_model("cases", "Case")
    Reference = apps.get_model("cases", "CaseCourtCaseReference")
    db = schema_editor.connection.alias
    primary_iri = _primary_iris(Reference, db)

    batch = []
    kept = 0
    rows = (
        Case.objects.using(db)
        .exclude(dates={"stages": []})
        .only("pk", "case_start_date", "case_end_date", "dates")
        .order_by("pk")
    )
    for case in rows.iterator(chunk_size=BATCH_SIZE):
        stored = case.dates if isinstance(case.dates, dict) else {}
        if (stored.get("stages") or []) != [_legacy_stage(case, primary_iri.get(case.pk))]:
            kept += 1
            continue
        case.dates = {"stages": []}
        case.proceedings_started_on = None
        case.proceedings_decided_on = None
        batch.append(case)
        if len(batch) >= BATCH_SIZE:
            Case.objects.using(db).bulk_update(batch, UPDATED_FIELDS)
            batch = []
    if batch:
        Case.objects.using(db).bulk_update(batch, UPDATED_FIELDS)

    if kept:
        print(f"  [0068] reverse kept {kept} hand-edited stage list(s)")


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0067_case_track_and_status_override"),
    ]

    operations = [
        migrations.RunPython(populate_stages, clear_stages, atomic=True),
    ]
