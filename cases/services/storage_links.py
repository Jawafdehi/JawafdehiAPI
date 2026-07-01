"""Shared helpers for turning stored files into source-link URLs."""

from urllib.parse import urljoin

from django.conf import settings


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
