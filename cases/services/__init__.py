"""
Services for the Jawafdehi cases app.

This package contains business logic services that can be reused
across management commands, views, and other parts of the application.
"""

from .case_importer import CaseImporter
from .case_scraper import CaseScraper

# NOTE: the JawafEntity merge service (entity_merge.py / merge_entities command)
# was removed with the JawafEntity model. Entities are owned by the Nepal Entity
# Service (NES) now, so entity merging is a NES concern, not Jawafdehi's.
__all__ = [
    "CaseScraper",
    "CaseImporter",
]
