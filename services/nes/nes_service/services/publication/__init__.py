"""Publication service — JSON-LD entity lifecycle + automatic versioning.

Operates on schema.org JSON-LD documents keyed by ``@id`` IRI: create/update with
an automatic version bump + snapshot row, author get-or-create + attribution, and
minimal JSON-LD validation (@type known, @id valid IRI, name present).
"""

from .service import PublicationService

__all__ = ["PublicationService"]
