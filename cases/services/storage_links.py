"""Shared helpers for turning stored files into source-link URLs (thin shim).

Lifted to :mod:`jawafdehi_shared.storage`; re-exported here so the existing
``cases.services.storage_links.absolute_media_url`` import path keeps working.
"""

from jawafdehi_shared.storage import absolute_media_url

__all__ = ["absolute_media_url"]
