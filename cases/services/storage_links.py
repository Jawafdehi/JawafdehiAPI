"""Shared helpers for turning stored files into source-link URLs."""

from urllib.parse import urljoin, urlparse

from django.conf import settings

# Raw Cloudflare R2 *endpoint* hosts look like
# ``<account-id>.r2.cloudflarestorage.com`` and carry the bucket name as the
# first path segment (``/<bucket>/<key>``). These are an internal storage
# endpoint, never the public link host — see ``normalize_storage_host``.
R2_ENDPOINT_HOST_SUFFIX = ".r2.cloudflarestorage.com"


def normalize_storage_host(url: str) -> str:
    """Rewrite a raw R2 endpoint URL to the public storage host.

    File/markdown links are derived from ``default_storage.url()``, whose host
    depends on ``AWS_S3_CUSTOM_DOMAIN``. When that env var is unset/lapsed,
    django-storages falls back to the raw R2 *endpoint*
    (``https://<account>.r2.cloudflarestorage.com/<bucket>/<key>``) instead of
    the public custom-domain host. That raw URL then gets frozen into a source's
    ``url`` JSON and is not publicly resolvable as a clean link.

    Because these links are persisted at write time, a transient misconfig would
    otherwise leave permanent bad data. We defend at the single chokepoint: any
    R2 endpoint host is rewritten to ``JAWAFDEHI_S3_BASE`` (the public custom
    domain), dropping the leading ``/<bucket>/`` segment so the key is preserved
    (``.../<bucket>/case_uploads/x.pdf`` -> ``<base>/case_uploads/x.pdf``). Both
    forms resolve to the same object. Non-R2 URLs are returned unchanged.
    """
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.netloc.endswith(R2_ENDPOINT_HOST_SUFFIX):
        return url
    base = (getattr(settings, "JAWAFDEHI_S3_BASE", "") or "").rstrip("/")
    if not base:
        return url
    # path is ``/<bucket>/<key...>``; drop the bucket segment, keep the key.
    segments = parsed.path.lstrip("/").split("/", 1)
    if len(segments) != 2 or not segments[1]:
        return url
    return f"{base}/{segments[1]}"


def absolute_media_url(url: str) -> str:
    """Make a possibly-relative media URL absolute (validators require a scheme).

    In production MEDIA_URL is an absolute S3 URL (AWS_S3_CUSTOM_DOMAIN), so file
    URLs come back absolute and this is a no-op. Locally (FileSystemStorage) URLs
    are like ``/media/...``; we prefix MEDIA_PUBLIC_BASE so the stored link
    validates against URLValidator.

    As a final safety net we normalize any raw R2 endpoint host to the public
    storage host (see ``normalize_storage_host``), so a missing
    ``AWS_S3_CUSTOM_DOMAIN`` can never bake an internal endpoint URL into a
    source's persisted links.
    """
    if url and url.startswith(("http://", "https://")):
        return normalize_storage_host(url)
    base = getattr(settings, "MEDIA_PUBLIC_BASE", "") or ""
    return urljoin(base + "/", url.lstrip("/")) if base else url
