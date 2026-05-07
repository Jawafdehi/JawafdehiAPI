from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("caseworker", "0008_rag_skill_profiles"),
        ("knowledge", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ragskillprofile",
            name="collections",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Optional indexed public knowledge cache searched when this skill "
                    "matches."
                ),
                related_name="rag_skill_profiles",
                to="knowledge.knowledgecollection",
            ),
        ),
    ]
