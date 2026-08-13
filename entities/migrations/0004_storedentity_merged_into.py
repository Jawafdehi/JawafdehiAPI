# APPLY THIS FOR REAL — do not --fake it. The nes migrations are normally faked in
# prod because the SQLAlchemy side owns those tables (see the managed-table note in
# entities/models.py), but entities.merged_into is a genuinely new column. Faking it
# leaves the column missing, so every entity read fails on it and no merged entity's
# URL redirects to its survivor.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("entities", "0003_storedentity_is_deleted"),
    ]

    operations = [
        migrations.AddField(
            model_name="storedentity",
            name="merged_into",
            field=models.TextField(blank=True, null=True),
        ),
    ]
