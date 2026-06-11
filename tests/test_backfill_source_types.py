"""Integration tests for the backfill_source_types management command.

Classification correctness lives in tests/test_source_classifier.py; these tests
exercise the command's plumbing (queryset filtering, dry-run, persistence,
flags, summary) against the shared classifier.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from cases.models import DocumentSource, SourceType


@pytest.fixture
def sources(db):
    """Create NULL-source_type sources spanning several new types."""
    objs = [
        DocumentSource.objects.create(
            source_id="t:abhiyog",
            title="CIAA अभियोग पत्र — मुद्दा नं ०८१-CR-०१२१",
            description="",
            url=[],
        ),
        DocumentSource.objects.create(
            source_id="t:press",
            title="अख्तियारको प्रेस विज्ञप्ति",
            description="",
            url=["https://ciaa.gov.np/pressrelease/3173"],
        ),
        DocumentSource.objects.create(
            source_id="t:court",
            title="विशेष अदालतको फैसला",
            description="",
            url=[],
        ),
        DocumentSource.objects.create(
            source_id="t:news",
            title="Ncell ruling",
            description="Report in Kathmandu Post",
            url=["https://kathmandupost.com/national/2023/06/10/ncell"],
        ),
        DocumentSource.objects.create(
            source_id="t:misc",
            title="Bidding Document",
            description="Some uploaded file",
            url=[],
        ),
    ]
    return objs


def test_dry_run_does_not_persist(sources):
    out = StringIO()
    call_command(
        "backfill_source_types", dry_run=True, allow_production=True, stdout=out
    )
    null_count = DocumentSource.objects.filter(source_type__isnull=True).count()
    assert null_count == len(sources)


def test_live_run_persists(sources):
    out = StringIO()
    call_command("backfill_source_types", allow_production=True, stdout=out)

    assert DocumentSource.objects.filter(source_type__isnull=True).count() == 0
    assert (
        DocumentSource.objects.get(source_id="t:abhiyog").source_type
        == SourceType.AG_ABHIYOG_PATRA
    )
    assert (
        DocumentSource.objects.get(source_id="t:press").source_type
        == SourceType.CIAA_PRESS_RELEASE
    )
    assert (
        DocumentSource.objects.get(source_id="t:court").source_type
        == SourceType.COURT_ORDER
    )
    assert DocumentSource.objects.get(source_id="t:news").source_type == SourceType.NEWS
    assert DocumentSource.objects.get(source_id="t:misc").source_type == SourceType.MISC


def test_limit(sources):
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        limit=3,
        allow_production=True,
        stdout=out,
    )
    classified = sum(1 for ln in out.getvalue().splitlines() if "[DRY-RUN]" in ln)
    assert classified <= 3


def test_source_id(sources):
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        source_id="t:abhiyog",
        verbose=True,
        allow_production=True,
        stdout=out,
    )
    output = out.getvalue()
    assert "[DRY-RUN] t:abhiyog: → AG_ABHIYOG_PATRA" in output
    assert "t:press" not in output


def test_skips_already_classified_sources(db):
    DocumentSource.objects.create(
        source_id="t:already-set",
        title="Already classified",
        description="",
        url=[],
        source_type=SourceType.MISC,
    )
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=True,
        allow_production=True,
        stdout=out,
    )
    assert "No sources found with NULL source_type." in out.getvalue()


def test_verbose_vs_non_verbose(sources):
    verbose = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=True,
        allow_production=True,
        stdout=verbose,
    )
    assert "[DRY-RUN]" in verbose.getvalue()

    quiet = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=False,
        allow_production=True,
        stdout=quiet,
    )
    assert "[DRY-RUN]" not in quiet.getvalue()


def test_priority_order(db):
    """A CIAA press-release URL + court-filing keywords still classifies by the
    title's document name (charge sheet here is most specific)."""
    DocumentSource.objects.create(
        source_id="t:priority",
        title="अभियोग पत्र",
        description="पुनरावेदन सम्बन्धी",
        url=["https://ciaa.gov.np/pressrelease/9999"],
    )
    out = StringIO()
    call_command(
        "backfill_source_types",
        dry_run=True,
        verbose=True,
        allow_production=True,
        stdout=out,
    )
    assert "[DRY-RUN] t:priority: → AG_ABHIYOG_PATRA" in out.getvalue()


def test_summary_line(sources):
    out = StringIO()
    call_command(
        "backfill_source_types", dry_run=True, allow_production=True, stdout=out
    )
    output = out.getvalue()
    assert "Dry-run=True" in output
    assert "Breakdown:" in output
