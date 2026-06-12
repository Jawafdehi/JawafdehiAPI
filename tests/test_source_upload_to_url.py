"""Uploads become `url` links (single source of truth).

Covers the WS0.5 behavior: the create API and the admin both store an uploaded
file to storage and append its URL to `DocumentSource.url`. A source's links
live solely in `url` — there is no separate uploaded-file storage.
"""

import pytest
from django.contrib import admin
from django.core.files.uploadedfile import SimpleUploadedFile

from cases.admin import DocumentSourceAdmin
from cases.models import DocumentSource, SourceLinkRole
from cases.serializers import DocumentSourceCreateSerializer
from tests.conftest import (
    create_document_source_with_entities,
    create_mock_request,
    create_user_with_role,
)


@pytest.fixture
def admin_user(db):
    return create_user_with_role("upload_admin", "upload_admin@test.com", "Admin")


@pytest.mark.django_db
def test_create_serializer_stores_upload_as_url_link():
    """Creating a source with uploaded_file appends a RAW link to `url`."""
    pdf = SimpleUploadedFile(
        "evidence.pdf", b"%PDF-1.4 data", content_type="application/pdf"
    )
    serializer = DocumentSourceCreateSerializer(
        data={"title": "Uploaded source", "source_type": "MISC", "uploaded_file": pdf}
    )
    assert serializer.is_valid(), serializer.errors
    source = serializer.save()

    raw_links = [u for u in source.url if u.get("role") == SourceLinkRole.RAW.value]
    assert len(raw_links) == 1
    assert raw_links[0]["link"].endswith(".pdf")


@pytest.mark.django_db
def test_create_serializer_rejects_disallowed_upload_extension():
    """The API upload path enforces the same extension/size/mimetype rules as
    the admin (the field carries the model upload validators)."""
    bad = SimpleUploadedFile(
        "malware.exe", b"MZ\x90\x00", content_type="application/octet-stream"
    )
    serializer = DocumentSourceCreateSerializer(
        data={"title": "Bad upload", "source_type": "MISC", "uploaded_file": bad}
    )
    assert not serializer.is_valid()
    assert "uploaded_file" in serializer.errors


@pytest.mark.django_db
def test_create_serializer_honors_upload_role():
    """A caller-supplied upload_role is applied to the stored link."""
    md = SimpleUploadedFile("notes.md", b"# hi", content_type="text/markdown")
    serializer = DocumentSourceCreateSerializer(
        data={
            "title": "Markdown upload",
            "source_type": "MISC",
            "uploaded_file": md,
            "upload_role": SourceLinkRole.MARKDOWN.value,
        }
    )
    assert serializer.is_valid(), serializer.errors
    source = serializer.save()
    roles = {u["role"] for u in source.url}
    assert roles == {SourceLinkRole.MARKDOWN.value}


@pytest.mark.django_db
def test_admin_upload_appends_url_link(admin_user):
    """Admin save_model stores the uploaded file and appends its link to `url`."""
    admin_instance = DocumentSourceAdmin(DocumentSource, admin.site)
    request = create_mock_request(admin_user, method="post")

    source = create_document_source_with_entities(
        title="Admin upload", description="x", related_entity_ids=[]
    )
    before = len(source.url or [])

    pdf = SimpleUploadedFile("a.pdf", b"%PDF-1.4", content_type="application/pdf")

    class _Form:
        cleaned_data = {"upload_file": pdf, "upload_role": SourceLinkRole.RAW.value}

    admin_instance.save_model(request, source, _Form(), change=True)
    source.refresh_from_db()

    assert len(source.url) == before + 1
    assert source.url[-1]["role"] == SourceLinkRole.RAW.value
    assert source.url[-1]["link"].endswith(".pdf")
