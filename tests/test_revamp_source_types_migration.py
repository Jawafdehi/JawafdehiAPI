"""Tests for the 0027_revamp_source_types data migration.

Exercises the migration's forward data function directly (with the live app
registry) rather than re-running the migration graph, which is sufficient since
the function only reads/writes DocumentSource rows.
"""

import datetime
import importlib

import pytest
from django.apps import apps

from cases.models import DocumentSource, SourceType

# Module name starts with a digit, so it can't be a normal `import`; load it by
# string name via importlib.
_migration = importlib.import_module("cases.migrations.0027_revamp_source_types")


def _run_migration():
    """Run the migration's forward data function against the live registry."""
    _migration.reclassify_and_fix_roles(apps, None)


def _create_with_legacy_type(legacy_type, **kwargs):
    """Create a source carrying a *legacy* (now-removed) source_type value.

    The legacy values are no longer valid model choices, so they can't be set
    through ``save()`` (clean_fields rejects them). Setting via ``.update()``
    bypasses validation and reproduces the real pre-migration DB state.
    """
    source = DocumentSource.objects.create(**kwargs)
    DocumentSource.objects.filter(pk=source.pk).update(source_type=legacy_type)
    source.refresh_from_db()
    return source


@pytest.mark.django_db
def test_reclassifies_legacy_types():
    """Legacy labels are re-derived into the new taxonomy."""
    # OFFICIAL_GOVERNMENT charge sheet → AG_ABHIYOG_PATRA (by title keyword).
    abhiyog = _create_with_legacy_type(
        "OFFICIAL_GOVERNMENT",
        source_id="m:abhiyog",
        title="CIAA अभियोग पत्र — मुद्दा नं ०८१-CR-०१२१",
        description="",
        url=[],
    )
    # MEDIA_NEWS whose only URL is now an S3 upload → NEWS via legacy fallback.
    news = _create_with_legacy_type(
        "MEDIA_NEWS",
        source_id="m:news",
        title="Online Khabar",
        description="",
        url=[{"link": "https://s3.jawafdehi.org/case_uploads/x.pdf", "role": "RAW"}],
        publication_date=datetime.date(2024, 1, 1),
    )

    _run_migration()

    abhiyog.refresh_from_db()
    news.refresh_from_db()
    assert abhiyog.source_type == SourceType.AG_ABHIYOG_PATRA
    assert news.source_type == SourceType.NEWS


@pytest.mark.django_db
def test_fixes_markdown_link_roles():
    """A .md link stored as RAW/PERMALINK is re-roled to MARKDOWN."""
    source = _create_with_legacy_type(
        "MEDIA_NEWS",
        source_id="m:md",
        title="Some news article",
        description="",
        url=[
            {"link": "https://kathmandupost.com/story", "role": "RAW"},
            {
                "link": "https://s3.jawafdehi.org/case_uploads/abc.md",
                "role": "PERMALINK",
            },
        ],
        publication_date=datetime.date(2024, 1, 1),
    )

    _run_migration()

    source.refresh_from_db()
    roles = {u["link"]: u["role"] for u in source.url}
    assert roles["https://s3.jawafdehi.org/case_uploads/abc.md"] == "MARKDOWN"
    # Non-markdown link is left untouched.
    assert roles["https://kathmandupost.com/story"] == "RAW"


@pytest.mark.django_db
def test_is_idempotent():
    """Running the migration twice yields the same result."""
    source = _create_with_legacy_type(
        "LEGAL_COURT_ORDER",
        source_id="m:idem",
        title="विशेष अदालतको फैसला",
        description="",
        url=[{"link": "https://s3.jawafdehi.org/case_uploads/o.md", "role": "RAW"}],
    )

    _run_migration()
    source.refresh_from_db()
    first_type = source.source_type
    first_roles = [u["role"] for u in source.url]

    _run_migration()
    source.refresh_from_db()
    assert source.source_type == first_type == SourceType.COURT_ORDER
    assert [u["role"] for u in source.url] == first_roles == ["MARKDOWN"]


@pytest.mark.django_db
def test_skips_deleted_sources():
    """Soft-deleted sources are not reclassified."""
    deleted = _create_with_legacy_type(
        "OFFICIAL_GOVERNMENT",
        source_id="m:deleted",
        title="विशेष अदालतको फैसला",
        description="",
        url=[],
        is_deleted=True,
    )

    _run_migration()

    # Skipped by the migration (filter is is_deleted=False), so its legacy
    # value is left untouched rather than re-classified to COURT_ORDER.
    deleted.refresh_from_db()
    assert deleted.source_type == "OFFICIAL_GOVERNMENT"
