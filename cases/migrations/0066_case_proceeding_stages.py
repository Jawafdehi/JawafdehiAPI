# ``Case.dates`` -- the proceeding-stage list -- plus the two columns derived
# from it. Additive: no existing column is touched and no data is migrated
# here, so this is safe to deploy before anything reads the new shape.
#
# Deliberately NO check constraint on the stage dates. A Supreme Court remand
# restarts first instance after the appeal ended, so a schema-level ordering
# rule fires on correct data; the rule lives in ``cases/stages.py`` where it can
# be relaxed in a patch release.

import cases.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0065_relationshipoutcome_remanded"),
    ]

    operations = [
        migrations.AddField(
            model_name="case",
            name="dates",
            field=models.JSONField(
                blank=True,
                default=cases.models._empty_stage_document,
                help_text=(
                    "Proceeding stages: {'stages': [{stage, start, end, "
                    "courtcase_iri, body, label, notes}]}. AD dates only; "
                    "Bikram Sambat is derived on display."
                ),
            ),
        ),
        migrations.AddField(
            model_name="case",
            name="proceedings_started_on",
            field=models.DateField(
                blank=True,
                db_index=True,
                help_text=(
                    "Derived: earliest start among COURT stages. Investigation "
                    "is excluded on purpose -- including it would move every "
                    "case's archive sort position backwards."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="case",
            name="proceedings_decided_on",
            field=models.DateField(
                blank=True,
                db_index=True,
                help_text=(
                    "Derived: the last court stage's end, NULL while any court "
                    "stage is still open."
                ),
                null=True,
            ),
        ),
    ]
