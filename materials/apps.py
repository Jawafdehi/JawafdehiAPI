from django.apps import AppConfig


class MaterialsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "materials"
    label = "materials"

    def ready(self):
        # Audit NGM materials. ``visibility`` is EXCLUDED: it is system-derived
        # (recomputed as the MAX over referring case states, see
        # materials/visibility.py), so auditing it would be pure machine noise —
        # human/content edits to ``data`` etc. are what we track. ``updated_at``
        # is likewise excluded as a non-substantive touch column. The IRI PK is
        # why cases migration 0051 widens ``object_pk`` to TEXT; LogEntry rows
        # are written cross-DB to ``default``.
        from jawafdehi_shared.db.audited import register_audited

        from materials.models import Material

        register_audited(Material, exclude_fields=["visibility", "updated_at"])

        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
