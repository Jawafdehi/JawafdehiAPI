# Choices-only: adds ``remanded`` (बदर गरी पुनः इन्साफ) to the outcome vocabulary.
# No data changes, no constraint changes -- ``outcome_only_on_accused`` tests the
# role, not the value.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0064_rename_case_type_to_offence_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="caseentityrelationship",
            name="outcome",
            field=models.CharField(
                blank=True,
                choices=[
                    ("charged", "Charged / undecided"),
                    ("convicted", "Convicted"),
                    ("acquitted", "Acquitted"),
                    ("abated", "Abated / discontinued"),
                    ("remanded", "Remanded for retrial"),
                ],
                db_index=True,
                help_text=(
                    "Verdict outcome for this ACCUSED entity (NULL for every other "
                    "role). 'charged' = formally charged, verdict pending. Distinct "
                    "from relationship_type (the role); terminal verdicts are set only "
                    "from a primary court order."
                ),
                max_length=20,
                null=True,
            ),
        ),
    ]
