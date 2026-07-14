"""Widen ``auditlog_logentry.object_pk`` from ``varchar(255)`` to ``text``.

django-auditlog stores a non-integer primary key in ``LogEntry.object_pk``, a
``CharField(max_length=255)``. Expanding audit coverage to the IRI-keyed models
(``entities.StoredEntity``, ``entities.HeldEntity``, ``materials.Material``)
means those primary keys — full URLs that routinely exceed 255 chars — land
there, which would truncate/raise. PostgreSQL ``varchar(255)`` -> ``text`` is a
cheap, no-rewrite change that lifts the limit; Django never enforces
``CharField.max_length`` on ``.save()``, so auditlog's model definition is left
untouched (DB-only change, no state operation) and its dependent index is
rebuilt automatically by the ``ALTER``.

Guarded to PostgreSQL: on the SQLite test/CI database ``varchar`` length is not
enforced, so the column already accepts long IRIs and this is a no-op. Lives in
the ``cases`` app (a ``default``-DB app) because auditlog is a third-party app
we do not add migrations to; the router keeps ``cases`` migrations on
``default`` only, which is where ``auditlog_logentry`` lives.
"""

from django.db import migrations

FORWARD = "ALTER TABLE auditlog_logentry ALTER COLUMN object_pk TYPE text"
REVERSE = "ALTER TABLE auditlog_logentry ALTER COLUMN object_pk TYPE varchar(255)"


def widen_object_pk(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD)


def narrow_object_pk(apps, schema_editor):
    # Best-effort reverse; fails loudly if a stored object_pk exceeds 255 chars.
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE)


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0051_caseslughistory"),
        ("auditlog", "0017_add_actor_email"),
    ]

    operations = [
        migrations.RunPython(widen_object_pk, narrow_object_pk),
    ]
