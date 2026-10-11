"""Drop the document-extraction tables introduced by 0006.

The read plane and the ingest that filled these tables are reverted (#514, #520):
the extraction is being redesigned, with a different API shape and a different
ingest, so the schema goes too.

This is a forward migration rather than a deletion of 0006. Production has 0006
recorded in ``django_migrations`` and the four tables exist with the CIAA corpus
in them, so removing the file would leave a recorded-but-absent migration and the
next ``makemigrations`` would emit a ``CreateModel`` for tables that are already
there — failing, weeks later, with "relation already exists".

**This drops data.** At the time of writing the tables hold the 35 CIAA annual
reports: 1,735 tables, 477 figures and 6,116 figure points. That corpus is
reproducible — it was ingested from ``damo-da/ciaa-annual-reports`` at revision
``c3c6c6730892e885785484536c1160e2fdf7c0e7`` — but it is not recoverable from
this database once this runs. Migrations are applied by hand on this platform, so
this takes effect when an operator runs it, not when the image deploys.

Children are dropped before their parents so the FK constraints come down in
dependency order.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("materials", "0006_documentextraction_extractedfigure_and_more"),
    ]

    operations = [
        migrations.DeleteModel(name="ExtractedFigurePoint"),
        migrations.DeleteModel(name="ExtractedTable"),
        migrations.DeleteModel(name="ExtractedFigure"),
        migrations.DeleteModel(name="DocumentExtraction"),
    ]
