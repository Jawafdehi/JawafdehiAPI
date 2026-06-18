from django.db import migrations, models


def backfill_case_id(apps, schema_editor):
    """Resolve each review's stable case_id from its slug via the Case table.

    The review system used the free-text slug as the case key; case_id is now the
    primary identifier. Map every distinct slug to its Case.case_id in one query,
    then bulk-update the reviews. Reviews whose slug no longer resolves keep
    case_id="" (they fall back to slug-based grouping and are skipped by regrade).

    Any pending/running review that can't be resolved is marked failed: without an
    identity it can't be processed, and leaving several with case_id="" would also
    collide on the new partial-unique constraint.
    """
    CaseReview = apps.get_model("review", "CaseReview")
    Case = apps.get_model("cases", "Case")

    slugs = set(
        CaseReview.objects.exclude(slug="").values_list("slug", flat=True).distinct()
    )
    if not slugs:
        return
    slug_to_case_id = dict(
        Case.objects.filter(slug__in=slugs).values_list("slug", "case_id")
    )

    to_update = []
    for review in CaseReview.objects.exclude(slug="").iterator():
        case_id = slug_to_case_id.get(review.slug, "")
        if case_id == review.case_id:
            continue
        review.case_id = case_id
        to_update.append(review)
    if to_update:
        CaseReview.objects.bulk_update(to_update, ["case_id"], batch_size=500)

    # Fail any still-unidentified active review so the new constraint is satisfiable.
    CaseReview.objects.filter(case_id="", status__in=["pending", "running"]).update(
        status="failed",
        stage="failed",
        error="Could not resolve case identity (case_id) from slug.",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("review", "0003_casereview_uniq_active_review_per_case"),
        ("cases", "0036_delete_chatuseridentity"),
    ]

    operations = [
        migrations.AddField(
            model_name="casereview",
            name="case_id",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=100
            ),
        ),
        migrations.AddField(
            model_name="casereview",
            name="reviewers",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_case_id, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="casereview",
            name="uniq_active_review_per_case",
        ),
        migrations.AddConstraint(
            model_name="casereview",
            constraint=models.UniqueConstraint(
                condition=models.Q(status__in=["pending", "running"]),
                fields=("case_id",),
                name="uniq_active_review_per_case",
            ),
        ),
    ]
