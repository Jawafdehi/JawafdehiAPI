from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("caseworker", "0010_remove_publicchatconfig_classifier_provider"),
    ]

    operations = [
        migrations.AlterField(
            model_name="publicchatconfig",
            name="max_tool_calls",
            field=models.PositiveIntegerField(default=8),
        ),
    ]
