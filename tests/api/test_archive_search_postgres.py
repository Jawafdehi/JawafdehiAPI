"""PostgreSQL-specific archive search infrastructure tests."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from cases.models import Case, CaseEvidenceSource, CaseState, CaseType, DocumentSource


@pytest.mark.django_db
def test_archive_search_indexes_are_installed():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-specific index assertion")

    expected = {
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
    }
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
            """)
        installed = {row[0] for row in cursor.fetchall()}

    assert expected <= installed


@pytest.mark.django_db
def test_case_save_syncs_evidence_source_links():
    source = DocumentSource.objects.create(
        source_id="source:sync:test",
        title="Sync source",
        description="Evidence source.",
    )
    case = Case.objects.create(
        case_id="case-evidence-sync",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        title="Evidence sync",
        evidence=[{"source_id": source.source_id, "description": "Initial"}],
    )

    assert list(case.evidence_links.values_list("document_source_id", flat=True)) == [
        source.id
    ]

    case.evidence = [{"source_id": "source:missing", "description": "Missing"}]
    case.save(update_fields=["evidence"])

    assert not CaseEvidenceSource.objects.filter(case=case).exists()


@pytest.mark.django_db
def test_rebuild_case_evidence_links_command_checks_and_repairs_drift():
    source = DocumentSource.objects.create(
        source_id="source:sync:command",
        title="Command source",
        description="Evidence source.",
    )
    case = Case.objects.create(
        case_id="case-evidence-command",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        title="Evidence command",
        evidence=[{"source_id": source.source_id, "description": "Initial"}],
    )
    CaseEvidenceSource.objects.filter(case=case).delete()

    with pytest.raises(CommandError, match="source:sync:command"):
        call_command("rebuild_case_evidence_links", "--check")

    output = StringIO()
    call_command("rebuild_case_evidence_links", stdout=output)

    assert "Rebuilt" in output.getvalue()
    assert list(case.evidence_links.values_list("document_source_id", flat=True)) == [
        source.id
    ]
