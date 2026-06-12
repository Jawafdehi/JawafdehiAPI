"""Tests for the backfill_upload_links_into_url management command."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from cases.models import DocumentSource, DocumentSourceUpload
from tests.conftest import create_document_source_with_entities


@pytest.mark.django_db
def test_backfill_adds_upload_link_and_tolerates_legacy_string_urls():
    """A DocumentSourceUpload whose URL isn't in `url` gets backfilled, even when
    the source's existing `url` holds legacy plain-string entries."""
    source = create_document_source_with_entities(
        title="Legacy source", related_entity_ids=[]
    )
    # Simulate legacy stored data: a plain string URL (pre-normalization).
    DocumentSource.objects.filter(pk=source.pk).update(
        url=["https://example.com/legacy-article"]
    )
    upload = DocumentSourceUpload.objects.create(
        source=source,
        file=SimpleUploadedFile(
            "eviden.pdf", b"%PDF-1.4", content_type="application/pdf"
        ),
    )

    call_command("backfill_upload_links_into_url", "--apply")

    source.refresh_from_db()
    # Legacy string was normalized, not dropped, and the upload link was added.
    links = {u["link"]: u["role"] for u in source.url}
    assert any("legacy-article" in link for link in links)
    base = upload.file.name.rsplit("/", 1)[-1]
    assert any(link.rsplit("/", 1)[-1] == base for link in links)


@pytest.mark.django_db
def test_backfill_is_idempotent():
    """Running the backfill twice does not duplicate links."""
    source = create_document_source_with_entities(
        title="Idempotent source", related_entity_ids=[]
    )
    DocumentSourceUpload.objects.create(
        source=source,
        file=SimpleUploadedFile("a.pdf", b"%PDF-1.4", content_type="application/pdf"),
    )

    call_command("backfill_upload_links_into_url", "--apply")
    source.refresh_from_db()
    first = list(source.url)

    call_command("backfill_upload_links_into_url", "--apply")
    source.refresh_from_db()
    assert source.url == first  # no change on the second run


@pytest.mark.django_db
def test_backfill_dry_run_writes_nothing():
    source = create_document_source_with_entities(
        title="Dry source", related_entity_ids=[]
    )
    DocumentSourceUpload.objects.create(
        source=source,
        file=SimpleUploadedFile("b.pdf", b"%PDF-1.4", content_type="application/pdf"),
    )

    call_command("backfill_upload_links_into_url")  # no --apply

    source.refresh_from_db()
    assert source.url == []  # dry run made no writes
