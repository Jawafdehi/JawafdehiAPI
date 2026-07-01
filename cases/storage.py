"""Custom storage backends for Jawafdehi (thin shim).

The storage backend was lifted to :mod:`jawafdehi_shared.storage` so the cases
and NGM materials apps share ONE upload mechanism. This module re-exports it so
the existing ``cases.storage.HashedFilenameS3Boto3Storage`` import path (referenced
by ``settings.STORAGES`` and any migrations/consumers) keeps working unchanged.
"""

from jawafdehi_shared.storage import HashedFilenameS3Boto3Storage

__all__ = ["HashedFilenameS3Boto3Storage"]
