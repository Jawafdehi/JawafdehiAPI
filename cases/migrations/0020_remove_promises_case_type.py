from django.db import migrations, models


def convert_promises_to_corruption(apps, schema_editor):
    """Convert any existing PROMISES cases to CORRUPTION."""
    Case = apps.get_model("cases", "Case")
    updated = Case.objects.filter(case_type="PROMISES").update(case_type="CORRUPTION")
    if updated:
        print(f"  Converted {updated} PROMISES case(s) to CORRUPTION.")


def reverse_convert(apps, schema_editor):
    """No reverse conversion — PROMISES is being removed."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0019_add_publication_date_to_documentsource"),
    ]

    operations = [
        migrations.RunPython(
            convert_promises_to_corruption,
            reverse_code=reverse_convert,
        ),
        migrations.AlterField(
            model_name="case",
            name="case_type",
            field=models.CharField(
                choices=[
                    ("CORRUPTION", "Corruption"),
                ],
                help_text="Type of case",
                max_length=20,
            ),
        ),
    ]
