"""Add ciaa_case_number field with unique constraint for CIAA idempotency."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add a dedicated unique CIAA case number field for cross-pipeline dedup."""

    dependencies = [
        ("cases", "0035_add_corruption_derived_case_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="case",
            name="ciaa_case_number",
            field=models.CharField(
                max_length=50,
                unique=True,
                null=True,
                blank=True,
                db_index=True,
                help_text=(
                    "CIAA case reference from the special court "
                    "(e.g. 'special:081-CR-0127'). Set automatically from "
                    "court_cases on save. Used as the canonical idempotency "
                    "key for deduplication across case-creation pipelines."
                ),
            ),
        ),
    ]
