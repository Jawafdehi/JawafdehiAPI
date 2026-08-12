"""Entity merge service — fold duplicate entities into one survivor."""

from .service import MAX_DUPLICATES, MAX_REFERENCES, EntityMergeService, MergeError

__all__ = ["EntityMergeService", "MergeError", "MAX_DUPLICATES", "MAX_REFERENCES"]
