# ``case_track`` (the route into court) and ``status_override``.
#
# Both default to NULL and are NOT backfilled. The ~2,900 drafts have no
# sources to classify a track from, and a value produced by the inference in
# ``review/casetype.py`` -- the one that misclassified Giribandhu -- is worse
# than null, because null is honest and queryable while a wrong guess is
# neither. The 82 published cases get a hand backfill; the drafts get the field
# filled at publish, by the caseworker already reading the sources.
#
# There is no ``status`` column: the derivation reads entity outcomes, so a
# stored value would be invalidated by an edit on a different endpoint. See
# ``Case.status``.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0066_case_proceeding_stages"),
    ]

    operations = [
        migrations.AddField(
            model_name="case",
            name="case_track",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ciaa", "अख्तियार / CIAA"),
                    ("money_laundering", "सम्पत्ति शुद्धीकरण / Money laundering"),
                    ("public_prosecutor", "सरकारवादी / Public prosecutor"),
                    ("writ", "रिट / Writ"),
                    ("arbitration", "मध्यस्थता / Arbitration"),
                    ("other", "Other"),
                ],
                db_index=True,
                help_text=(
                    "The route into court. Null until a caseworker who has read "
                    "the sources sets it -- an inferred value is worse than "
                    "null, because null is honest and queryable."
                ),
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="case",
            name="status_override",
            field=models.CharField(
                blank=True,
                choices=[("withdrawn", "Withdrawn"), ("dormant", "Dormant")],
                help_text=(
                    "Overrides the derived status for the two lifecycles the "
                    "stage list cannot express (withdrawn, dormant)."
                ),
                max_length=20,
                null=True,
            ),
        ),
    ]
