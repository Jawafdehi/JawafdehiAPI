"""Store an uploaded file and return it as an external source link.

A DocumentSource's links live solely in its ``url`` JSON list. File upload is an
ingestion convenience: we persist the bytes to the configured (S3) storage and
record the resulting permanent public URL as a ``{link, role}`` entry in ``url``.
There is no separate uploaded-file DB row — the URL is the single source of truth.

This is shared by the create API and the admin so both ingest identically.
"""

import re
import urllib.parse

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


# Document-file extensions that can serve as a canonical RAW document.
_DOC_EXTS = (".pdf", ".doc", ".docx", ".odt", ".rtf")


def _is_ciaa_page(link: str) -> bool:
    """True if ``link`` is a ciaa.gov.np press-release landing page (not a file)."""
    return urllib.parse.urlparse(link or "").netloc.lower().endswith("ciaa.gov.np")


def _link_ext(link: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlparse(link or "").path).lower()
    m = re.search(r"(\.[a-z0-9]{1,5})$", path)
    return m.group(1) if m else ""


def classify_ciaa_links(links):
    """Assign source-link roles to a CIAA press-release's links.

    Encodes the stored convention so exactly ONE link is RAW:
      - the canonical document (preferring a ``.pdf`` upload, else the first
        uploaded document file) -> RAW
      - the ciaa.gov.np landing page -> SOURCE_PAGE
      - any other uploaded document file (e.g. a ``.doc`` export) -> ALTERNATE
      - anything else -> ALTERNATE

    Exception: if there is NO uploaded document file (the page is the only link,
    as when a draft case is first created before files are mapped), the page
    stays RAW so the source still has a canonical link. Order is preserved.

    Args:
        links: an iterable of link strings.

    Returns:
        list of ``{link, role}`` dicts, in the original order, with exactly one
        RAW whenever at least one link is present.
    """
    links = [link for link in (links or []) if link and str(link).strip()]
    if not links:
        return []

    files = [link for link in links if not _is_ciaa_page(link)]
    # Pick the canonical RAW: a .pdf file first, else the first non-page file.
    raw = None
    if files:
        pdfs = [link for link in files if _link_ext(link) == ".pdf"]
        raw = pdfs[0] if pdfs else files[0]
    else:
        # No uploaded file — keep the (page) link as RAW so there's a canonical one.
        raw = links[0]

    out = []
    for link in links:
        if link == raw:
            role = SourceLinkRole.RAW.value
        elif _is_ciaa_page(link):
            role = SourceLinkRole.SOURCE_PAGE.value
        else:
            role = SourceLinkRole.ALTERNATE.value
        out.append({"link": link, "role": role})
    return out
