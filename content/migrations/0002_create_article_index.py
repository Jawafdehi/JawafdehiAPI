from django.db import migrations

# The article index page is created by a post_migrate handler
# (content.permissions.ensure_article_index), not here: creating a Wagtail page
# inside a migration uses real-model save/treebeard logic that aborts the
# migration transaction on PostgreSQL. This migration is kept (now a no-op) so
# the existing migration graph / dependencies stay intact.


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0001_initial"),
    ]

    operations = []
