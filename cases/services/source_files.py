"""Store an uploaded file and return it as an external source link (thin shim).

A DocumentSource's links live solely in its ``url`` JSON list. File upload is an
ingestion convenience: we persist the bytes to the configured (S3) storage and
record the resulting permanent public URL as a ``{link, role}`` entry in ``url``.
There is no separate uploaded-file DB row — the URL is the single source of truth.

The storage mechanism itself was lifted to :mod:`jawafdehi_shared.storage` so the
cases app and the NGM materials app ingest identically. This shim re-exports
``store_file_as_link`` (with the cases-app RAW default) so both the create API and
the admin keep their existing import path.
"""

from cases.models import SourceLinkRole
from jawafdehi_shared.storage import store_file_as_link as _store_file_as_link


def store_file_as_link(uploaded_file, role=SourceLinkRole.RAW.value) -> dict:
    """Persist ``uploaded_file`` to storage and return its ``{link, role}`` dict.

    Thin wrapper over :func:`jawafdehi_shared.storage.store_file_as_link` that
    pins the default ``role`` to the cases ``SourceLinkRole.RAW`` enum value.
    """
    return _store_file_as_link(uploaded_file, role=role)
