from django.db import migrations

NEWSROOM = "Jawafdehi Newsroom"
WAGTAIL_DEFAULT = "Welcome to your new Wagtail site!"


def _rename(site_name, page_title):
    def _run(apps, schema_editor):
        from wagtail.models import Site

        site = Site.objects.filter(is_default_site=True).first()
        if not site:
            return
        if site.site_name != site_name:
            site.site_name = site_name
            site.save(update_fields=["site_name"])
        root = site.root_page
        if root and root.title != page_title:
            root.title = page_title
            root.draft_title = page_title
            root.save(update_fields=["title", "draft_title"])

    return _run


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0003_alter_articlepage_body"),
    ]

    operations = [
        migrations.RunPython(
            _rename(NEWSROOM, NEWSROOM),
            _rename("localhost", WAGTAIL_DEFAULT),
        ),
    ]
