"""Store an uploaded file and return it as an external source link.

A DocumentSource's links live solely in its ``url`` JSON list. File upload is an
ingestion convenience: we persist the bytes to the configured (S3) storage and
record the resulting permanent public URL as a ``{link, role}`` entry in ``url``.
There is no separate uploaded-file DB row — the URL is the single source of truth.

This is shared by the create API and the admin so both ingest identically.
"""

from urllib.parse import urljoin

from django.conf import settings
from django.core.files.storage import default_storage

from cases.models import SourceLinkRole


def _absolute(url: str) -> str:
    """Make a possibly-relative media URL absolute (validators require a scheme).

    In production MEDIA_URL is an absolute S3 URL (AWS_S3_CUSTOM_DOMAIN), so file
    URLs come back absolute and this is a no-op. Locally (FileSystemStorage) URLs
    are like ``/media/...``; we prefix MEDIA_PUBLIC_BASE so the stored link
    validates against URLValidator.
    """
    if url and url.startswith(("http://", "https://")):
        return url
    base = getattr(settings, "MEDIA_PUBLIC_BASE", "") or ""
    return urljoin(base + "/", url.lstrip("/")) if base else url


def store_file_as_link(uploaded_file, role=SourceLinkRole.RAW.value) -> dict:
    """Persist ``uploaded_file`` to storage and return its ``{link, role}`` dict.

    The default storage backend (HashedFilenameS3Boto3Storage in prod) hashes the
    name and prefixes it (``case_uploads/``), yielding the canonical permanent
    URL. ``role`` defaults to RAW but the caller may pass any SourceLinkRole.
    """
    name = default_storage.save(uploaded_file.name, uploaded_file)
    return {"link": _absolute(default_storage.url(name)), "role": role}
