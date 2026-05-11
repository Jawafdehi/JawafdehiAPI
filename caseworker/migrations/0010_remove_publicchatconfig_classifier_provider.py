from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("caseworker", "0009_rag_skill_optional_collections"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="publicchatconfig",
            name="classifier_llm_provider",
        ),
    ]
