"""Store an uploaded file and return it as an external source link.

A DocumentSource's links live solely in its ``url`` JSON list. File upload is an
ingestion convenience: we persist the bytes to the configured (S3) storage and
record the resulting permanent public URL as a ``{link, role}`` entry in ``url``.
There is no separate uploaded-file DB row — the URL is the single source of truth.

This is shared by the create API and the admin so both ingest identically.
"""

from django.core.files.storage import default_storage

from cases.models import SourceLinkRole
from cases.services.storage_links import absolute_media_url


def store_file_as_link(uploaded_file, role=SourceLinkRole.RAW.value) -> dict:
    """Persist ``uploaded_file`` to storage and return its ``{link, role}`` dict.

    The default storage backend (HashedFilenameS3Boto3Storage in prod) hashes the
    file name (neutralizing any path-traversal in the client-supplied name) and
    prefixes it (``case_uploads/``), yielding the canonical permanent URL.
    ``role`` defaults to RAW but the caller may pass any SourceLinkRole.
    """
    name = default_storage.save(uploaded_file.name, uploaded_file)
    return {"link": absolute_media_url(default_storage.url(name)), "role": role}
