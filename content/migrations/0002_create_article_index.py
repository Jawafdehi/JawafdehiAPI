from django.db import migrations


def create_article_index(apps, schema_editor):
    """Create a single live ArticleIndexPage under the default site root.

    Guarded so it is a no-op if the Wagtail root/site isn't present yet or an
    index already exists; the index can otherwise be created in the admin.
    """
    from wagtail.models import Page, Site

    from content.models import ArticleIndexPage

    if ArticleIndexPage.objects.exists():
        return

    site = Site.objects.filter(is_default_site=True).first()
    root = site.root_page if site else Page.objects.filter(depth=2).first()
    if root is None:
        root = Page.objects.filter(depth=1).first()
    if root is None:
        return

    root.add_child(
        instance=ArticleIndexPage(title="Articles", slug="articles", live=True)
    )


def remove_article_index(apps, schema_editor):
    from content.models import ArticleIndexPage

    ArticleIndexPage.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_article_index, remove_article_index),
    ]
