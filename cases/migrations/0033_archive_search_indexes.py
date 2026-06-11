import django.db.models.deletion
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import migrations, models

INDEX_NAMES = (
    "case_archive_fts_idx",
    "case_title_trgm_idx",
    "case_id_trgm_idx",
    "case_archive_filter_idx",
    "entity_name_trgm_idx",
    "entity_nes_id_trgm_idx",
    "source_archive_fts_idx",
    "source_title_trgm_idx",
    "source_id_trgm_idx",
    "source_archive_filter_idx",
)


def create_postgres_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    statements = [
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS case_archive_fts_idx
        ON cases_case USING GIN (
            to_tsvector(
                'simple',
                coalesce(title, '') || ' ' ||
                coalesce(short_description, '') || ' ' ||
                coalesce(description, '') || ' ' ||
                coalesce(case_id, '')
            )
        )
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS case_title_trgm_idx
        ON cases_case USING GIN (title gin_trgm_ops)
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS case_id_trgm_idx
        ON cases_case USING GIN (case_id gin_trgm_ops)
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS case_archive_filter_idx
        ON cases_case (state, case_type, created_at DESC)
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS entity_name_trgm_idx
        ON cases_jawafentity USING GIN (display_name gin_trgm_ops)
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS entity_nes_id_trgm_idx
        ON cases_jawafentity USING GIN (nes_id gin_trgm_ops)
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS source_archive_fts_idx
        ON cases_documentsource USING GIN (
            to_tsvector(
                'simple',
                coalesce(title, '') || ' ' ||
                coalesce(description, '') || ' ' ||
                coalesce(source_id, '')
            )
        )
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS source_title_trgm_idx
        ON cases_documentsource USING GIN (title gin_trgm_ops)
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS source_id_trgm_idx
        ON cases_documentsource USING GIN (source_id gin_trgm_ops)
        """,
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS source_archive_filter_idx
        ON cases_documentsource (is_deleted, created_at DESC)
        """,
    ]
    for statement in statements:
        schema_editor.execute(statement)


def drop_postgres_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for name in INDEX_NAMES:
        schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def backfill_case_evidence_links(apps, schema_editor):
    Case = apps.get_model("cases", "Case")
    DocumentSource = apps.get_model("cases", "DocumentSource")
    CaseEvidenceSource = apps.get_model("cases", "CaseEvidenceSource")

    batch = []
    for case in Case.objects.order_by("pk").iterator(chunk_size=500):
        batch.append(case)
        if len(batch) >= 500:
            _backfill_case_evidence_batch(batch, DocumentSource, CaseEvidenceSource)
            batch = []
    if batch:
        _backfill_case_evidence_batch(batch, DocumentSource, CaseEvidenceSource)


def _backfill_case_evidence_batch(cases, DocumentSource, CaseEvidenceSource):
    source_ids = {
        (item.get("source_id") or "").strip()
        for case in cases
        for item in (case.evidence or [])
        if isinstance(item, dict) and item.get("source_id")
    }
    source_id_to_pk = dict(
        DocumentSource.objects.filter(source_id__in=source_ids).values_list(
            "source_id", "pk"
        )
    )
    links = []
    for case in cases:
        seen = set()
        for index, item in enumerate(case.evidence or []):
            if not isinstance(item, dict):
                continue
            source_id = (item.get("source_id") or "").strip()
            source_pk = source_id_to_pk.get(source_id)
            if not source_pk or source_pk in seen:
                continue
            seen.add(source_pk)
            links.append(
                CaseEvidenceSource(
                    case_id=case.pk,
                    document_source_id=source_pk,
                    evidence_index=index,
                    description=item.get("description") or "",
                )
            )
            if len(links) >= 1000:
                CaseEvidenceSource.objects.bulk_create(links, ignore_conflicts=True)
                links = []
    if links:
        CaseEvidenceSource.objects.bulk_create(links, ignore_conflicts=True)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("cases", "0032_backfill_source_link_role_raw"),
    ]

    operations = [
        migrations.CreateModel(
            name="CaseEvidenceSource",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("evidence_index", models.PositiveIntegerField(default=0)),
                ("description", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "case",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="evidence_links",
                        to="cases.case",
                    ),
                ),
                (
                    "document_source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="case_links",
                        to="cases.documentsource",
                    ),
                ),
            ],
            options={
                "ordering": ["case_id", "evidence_index", "id"],
                "indexes": [
                    models.Index(
                        fields=["case", "document_source"],
                        name="case_evidence_case_source_idx",
                    ),
                    models.Index(
                        fields=["document_source", "case"],
                        name="case_evidence_source_case_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("case", "document_source"),
                        name="unique_case_evidence_source",
                    )
                ],
            },
        ),
        migrations.RunPython(
            backfill_case_evidence_links,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    create_postgres_indexes,
                    drop_postgres_indexes,
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="case",
                    index=GinIndex(
                        SearchVector(
                            "title",
                            "short_description",
                            "description",
                            "case_id",
                            config="simple",
                        ),
                        name="case_archive_fts_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="case",
                    index=GinIndex(
                        fields=["title"],
                        name="case_title_trgm_idx",
                        opclasses=["gin_trgm_ops"],
                    ),
                ),
                migrations.AddIndex(
                    model_name="case",
                    index=GinIndex(
                        fields=["case_id"],
                        name="case_id_trgm_idx",
                        opclasses=["gin_trgm_ops"],
                    ),
                ),
                migrations.AddIndex(
                    model_name="case",
                    index=models.Index(
                        fields=["state", "case_type", "-created_at"],
                        name="case_archive_filter_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="jawafentity",
                    index=GinIndex(
                        fields=["display_name"],
                        name="entity_name_trgm_idx",
                        opclasses=["gin_trgm_ops"],
                    ),
                ),
                migrations.AddIndex(
                    model_name="jawafentity",
                    index=GinIndex(
                        fields=["nes_id"],
                        name="entity_nes_id_trgm_idx",
                        opclasses=["gin_trgm_ops"],
                    ),
                ),
                migrations.AddIndex(
                    model_name="documentsource",
                    index=GinIndex(
                        SearchVector(
                            "title",
                            "description",
                            "source_id",
                            config="simple",
                        ),
                        name="source_archive_fts_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="documentsource",
                    index=GinIndex(
                        fields=["title"],
                        name="source_title_trgm_idx",
                        opclasses=["gin_trgm_ops"],
                    ),
                ),
                migrations.AddIndex(
                    model_name="documentsource",
                    index=GinIndex(
                        fields=["source_id"],
                        name="source_id_trgm_idx",
                        opclasses=["gin_trgm_ops"],
                    ),
                ),
                migrations.AddIndex(
                    model_name="documentsource",
                    index=models.Index(
                        fields=["is_deleted", "-created_at"],
                        name="source_archive_filter_idx",
                    ),
                ),
            ],
        ),
    ]
