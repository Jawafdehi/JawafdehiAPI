from django.db import migrations, models


class Migration(migrations.Migration):
    """Split an author's public prose into a one-line title and a longer bio.

    ``description`` is renamed to ``title`` because that is what it holds — a
    role line ("Caseworker", "BALLB 4th Year Student") that rides along on the
    compact author card on every case page. ``RenameField`` rather than
    remove-and-add so nothing already written is lost.

    ``bio`` is new and renders only on the profile page, so the card never has to
    carry a paragraph.
    """

    dependencies = [
        ("cases", "0060_author_profiles"),
    ]

    operations = [
        migrations.RenameField(
            model_name="authorprofile",
            old_name="description",
            new_name="title",
        ),
        migrations.AlterField(
            model_name="authorprofile",
            name="title",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "One-line role shown under the name and on the author card "
                    "on every case page, e.g. 'Caseworker' or 'BALLB 4th Year "
                    "Student'. Keep it short — the card truncates. Longer prose "
                    "goes in ``bio``."
                ),
            ),
        ),
        migrations.AddField(
            model_name="authorprofile",
            name="bio",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Longer public biography shown on the author's profile page "
                    "(markdown). Empty = the About section is not rendered."
                ),
            ),
        ),
    ]
