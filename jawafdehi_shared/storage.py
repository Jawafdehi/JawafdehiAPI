"""Shared object-storage helpers for uploaded-file ingestion.

The ONE place both the Jawafdehi cases app and the NGM materials app persist an
uploaded file to object storage and turn it into a permanent public link. Lifted
here (from ``cases.storage`` / ``cases.services``) so both surfaces ingest
identically instead of duplicating the storage mechanism.

Contents:
- :class:`HashedFilenameS3Boto3Storage` — the production S3 backend that hashes
  the client-supplied filename (neutralizing path-traversal) and prefixes it
  (``case_uploads/``), yielding a deterministic permanent URL.
- :func:`absolute_media_url` — make a possibly-relative media URL absolute so it
  validates against ``URLValidator`` (a no-op in prod where MEDIA_URL is absolute).
- :func:`store_file_as_link` — stream an ``UploadedFile`` to ``default_storage``
  and return its ``{"link", "role"}`` dict.

Settings drive it: ``AWS_S3_*``, ``MEDIA_URL``, ``FILE_STORAGE_PREFIX``,
``FILE_HASH_SALT``, ``MEDIA_PUBLIC_BASE``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from urllib.parse import urljoin

from django.conf import settings
from django.core.files.storage import default_storage
from storages.backends.s3boto3 import S3Boto3Storage

#: The default source-link role for a stored upload. Matches
#: ``cases.models.SourceLinkRole.RAW`` (kept as a bare literal here so this
#: module has no dependency on the cases app).
DEFAULT_LINK_ROLE = "RAW"


class HashedFilenameS3Boto3Storage(S3Boto3Storage):
    """
    S3 storage backend that hashes file names for security and uniqueness.

    This storage backend automatically generates a hash of the original filename
    and uses it as the stored filename, while preserving the original filename
    in a separate field for display purposes.

    The hash is generated using SHA-256 and includes a salt for additional security.
    """

    def __init__(self, *args, **kwargs):
        # Get hash salt from environment or use a default
        self.hash_salt = os.getenv("FILE_HASH_SALT", "jawafdehi-file-salt")
        # Get file prefix from environment or use a default
        self.file_prefix = os.getenv("FILE_STORAGE_PREFIX", "case_uploads/")
        super().__init__(*args, **kwargs)

    def _get_hashed_filename(self, name):
        """
        Generate a hashed filename from the original filename.

        Args:
            name: Original filename

        Returns:
            str: Hashed filename with original extension
        """
        if not name:
            return name

        # Split filename into name and extension
        name_part, ext = os.path.splitext(name)

        # Create hash of the original name with salt
        hash_input = f"{self.hash_salt}:{name_part}"
        hashed_name = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

        # Return hashed name with prefix and original extension
        return f"{self.file_prefix}{hashed_name}{ext}"

    def save(self, name, content, max_length=None):
        """
        Save file with hashed filename.

        Args:
            name: Original filename
            content: File content
            max_length: Maximum length for filename

        Returns:
            str: The hashed filename used for storage
        """
        # Generate hashed filename
        hashed_name = self._get_hashed_filename(name)

        # Call parent save with hashed name
        return super().save(hashed_name, content, max_length)

    def get_object_parameters(self, name):
        """
        Ensure text uploads carry an explicit UTF-8 charset.

        django-storages derives ContentType from ``mimetypes.guess_type`` when
        none is supplied, which yields bare types like ``text/markdown`` with no
        charset. Browsers opening such a ``text/*`` response with no charset fall
        back to a legacy locale encoding (Latin-1), turning UTF-8 content (e.g.
        Devanagari) into mojibake. We append ``; charset=utf-8`` to any text type
        that lacks a charset so the bytes are decoded correctly.
        """
        params = super().get_object_parameters(name)

        content_type = params.get("ContentType")
        if content_type is None:
            content_type, _encoding = mimetypes.guess_type(name)

        if (
            content_type
            and content_type.startswith("text/")
            and "charset=" not in content_type.lower()
        ):
            params["ContentType"] = f"{content_type}; charset=utf-8"

        return params

    def get_valid_filename(self, name):
        """
        Return a filename that's valid for the storage system.

        For hashed storage, we return the hashed version.
        """
        return self._get_hashed_filename(name)

    def get_available_name(self, name, max_length=None):
        """
        Return a filename that's available for the storage system.

        Since the filename is a deterministic hash of the original name, it is
        always the same for a given input.  We return it directly without
        delegating to super(), which would append a numeric/random suffix when
        the object already exists and thereby break the deterministic guarantee.
        """
        return self._get_hashed_filename(name)


def absolute_media_url(url: str) -> str:
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


def store_file_as_link(uploaded_file, role=DEFAULT_LINK_ROLE) -> dict:
    """Persist ``uploaded_file`` to storage and return its ``{link, role}`` dict.

    The default storage backend (HashedFilenameS3Boto3Storage in prod) hashes the
    file name (neutralizing any path-traversal in the client-supplied name) and
    prefixes it (``case_uploads/``), yielding the canonical permanent URL.
    ``role`` defaults to RAW but the caller may pass any source-link role.
    """
    name = default_storage.save(uploaded_file.name, uploaded_file)
    return {"link": absolute_media_url(default_storage.url(name)), "role": role}
