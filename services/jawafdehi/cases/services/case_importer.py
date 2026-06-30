"""
Service for importing scraped case data into Django models.

Handles entity deduplication, source deduplication, and data transformation
from scraped JSON format to Django Case model.
"""

import json
from datetime import datetime

from django.db import transaction

from jawafdehi_shared.entities.ids import is_valid_entity_iri

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    DocumentSource,
    RelationshipType,
)


class CaseImporter:
    """Service for importing scraped case data into Django models."""

    def __init__(self, logger=None):
        """
        Initialize the case importer.

        Args:
            logger: Optional logger for output (e.g., command stderr)
        """
        self.logger = logger
        self.stats = {
            # Number of Case<->NES-entity binds created from a valid NES id.
            "entities_bound": 0,
            # Entries skipped because they had no valid NES id (NES owns the
            # entity data; binds cannot be created from a bare name).
            "entities_skipped_no_nes_id": 0,
            "sources_created": 0,
            "sources_reused": 0,
        }

    def log(self, message):
        """Log a message if logger is available."""
        if self.logger:
            if hasattr(self.logger, "write"):
                self.logger.write(message)
            else:
                self.logger(message)

    def resolve_nes_id(self, value):
        """Extract a valid canonical NES id from an import entry.

        NES is the single source of truth for entities and a bind requires a
        valid canonical entity @id IRI (``https://jawafdehi.org/entity/<prefix>/
        <slug>``) — there is no name fallback. An entry may be the id string
        itself, or a dict carrying a ``nes_id`` key. Entries without a valid NES
        id return ``None`` (the caller skips them).
        """
        if isinstance(value, dict):
            value = value.get("nes_id")
        nes_id = (value or "").strip() if isinstance(value, str) else ""
        if not is_valid_entity_iri(nes_id):
            return None
        return nes_id

    def bind_entity(self, case, value, relationship_type):
        """Bind ``case`` to a NES entity by id; skip entries without a valid id."""
        nes_id = self.resolve_nes_id(value)
        if nes_id is None:
            self.stats["entities_skipped_no_nes_id"] += 1
            self.log(f"  Skipping entity without a valid NES id: {value!r}")
            return None
        CaseEntityRelationship.objects.get_or_create(
            case=case,
            nes_id=nes_id,
            relationship_type=relationship_type,
            defaults={"notes": ""},
        )
        self.stats["entities_bound"] += 1
        self.log(f"  Bound entity: {nes_id} ({relationship_type})")
        return nes_id

    def get_or_create_source(self, source_data):
        """
        Get or create DocumentSource with deduplication by URL.

        Uses PostgreSQL JSON containment query for efficient URL lookup when available.
        Falls back to Python-based filtering for SQLite.

        Args:
            source_data: Dict with 'title', 'url', 'description' keys

        Returns:
            DocumentSource instance or None if title is empty
        """
        # Guard against None values and handle both string and list URLs
        url_raw = source_data.get("url", "")

        # Handle URL as string, list, or dict. Normalize every entry to a
        # canonical {link, role} dict — role defaults to RAW when the input is
        # a bare string or omits it (only external URLs reach here).
        from cases.models import SourceLinkRole

        def _make_link(raw):
            if isinstance(raw, str):
                stripped = raw.strip()
                return (
                    {"link": stripped, "role": SourceLinkRole.RAW.value}
                    if stripped
                    else None
                )
            if isinstance(raw, dict):
                link = raw.get("link") or raw.get("url")
                if isinstance(link, str) and link.strip():
                    role = raw.get("role") or SourceLinkRole.RAW.value
                    return {"link": link.strip(), "role": role}
            return None

        if isinstance(url_raw, list):
            url_list = [entry for entry in (_make_link(i) for i in url_raw) if entry]
        elif isinstance(url_raw, (str, dict)):
            entry = _make_link(url_raw)
            url_list = [entry] if entry else []
        else:
            url_list = []

        title_raw = source_data.get("title", "")
        title = title_raw.strip() if isinstance(title_raw, str) else ""

        description_raw = source_data.get("description", "")
        description = (
            description_raw.strip() if isinstance(description_raw, str) else ""
        )

        if not title:
            return None

        # Try to find existing source by URL
        # TODO: Consider adding GIN index on url field for better performance:
        #   CREATE INDEX idx_documentsource_url_gin ON cases_documentsource USING gin (url);
        if url_list:
            # Check if any URL in our list matches existing sources.
            # Stored URL entries are dicts with 'link' key (post-migration).
            # Accept both str and dict lookups for backward compat during transition.
            from django.db import connection

            # Match on the link only (role-agnostic) so a reused source isn't
            # duplicated just because its stored role differs.
            candidate_links = [entry["link"] for entry in url_list]

            # Use PostgreSQL JSON containment if available, otherwise fall back to Python filtering
            if connection.vendor == "postgresql":
                for link in candidate_links:
                    source = (
                        DocumentSource.objects.filter(
                            is_deleted=False,
                        )
                        .filter(
                            # Match dict entry with matching link
                            url__contains=[{"link": link}]
                        )
                        .only("source_id", "title")
                        .first()
                    )

                    if source:
                        self.stats["sources_reused"] += 1
                        self.log(f"  Reusing source: {title}")
                        return source
            else:
                # SQLite fallback: fetch all non-deleted sources and check URLs in Python
                for link in candidate_links:
                    for source in DocumentSource.objects.filter(is_deleted=False).only(
                        "source_id", "title", "url"
                    ):
                        if isinstance(source.url, list):
                            for stored in source.url:
                                stored_link = (
                                    stored
                                    if isinstance(stored, str)
                                    else (
                                        stored.get("link")
                                        if isinstance(stored, dict)
                                        else None
                                    )
                                )
                                if stored_link == link:
                                    self.stats["sources_reused"] += 1
                                    self.log(f"  Reusing source: {title}")
                                    return source

        # Try to find by title (excluding soft-deleted sources)
        source = DocumentSource.objects.filter(title=title, is_deleted=False).first()
        if source:
            self.stats["sources_reused"] += 1
            self.log(f"  Reusing source: {title}")
            return source

        # Create new source with URL as list
        source = DocumentSource.objects.create(
            title=title, description=description, url=url_list
        )

        self.stats["sources_created"] += 1
        self.log(f"  Created source: {title}")
        return source

    def parse_date(self, date_str):
        """
        Parse date string to date object.

        Args:
            date_str: Date string in YYYY-MM-DD format

        Returns:
            date object or None if parsing fails
        """
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    def import_from_json(self, json_file, case_type="CORRUPTION", case_state="DRAFT"):
        """
        Import a case from JSON file.

        Args:
            json_file: Path to case-result.json file
            case_type: Case type (CORRUPTION)
            case_state: Initial case state (DRAFT, IN_REVIEW, or PUBLISHED)

        Returns:
            Created Case instance

        Raises:
            ValueError: If JSON is invalid or required fields are missing
            ValidationError: If case data fails validation
        """
        # Read and parse JSON
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        title = data.get("title", "").strip()
        if not title:
            raise ValueError("Case title is required")

        # Check if case already exists by title
        existing_case = Case.objects.filter(title=title).first()
        if existing_case:
            raise ValueError(
                f"Case with title '{title}' already exists (ID: {existing_case.slug})"
            )

        self.log(f"Importing case: {title}")

        # Create case with transaction
        with transaction.atomic():
            # Create case
            case = Case(
                case_type=getattr(CaseType, case_type),
                state=getattr(CaseState, case_state),
                title=title,
                description=data.get("description", ""),
                case_start_date=self.parse_date(data.get("case_start_date")),
                case_end_date=self.parse_date(data.get("case_end_date")),
                tags=data.get("tags", []),
                key_allegations=data.get("key_allegations", []),
                timeline=data.get("timeline", []),
            )
            case.save()

            self.log(f"Created case: {case.slug}")

            # Add alleged entity binds (by NES id)
            self.log("Processing alleged entities...")
            for entry in data.get("alleged_entities", []):
                self.bind_entity(case, entry, RelationshipType.ACCUSED)

            # Add related entity binds (by NES id)
            self.log("Processing related entities...")
            for entry in data.get("related_entities", []):
                self.bind_entity(case, entry, RelationshipType.RELATED)

            # Add location binds (by NES id)
            self.log("Processing locations...")
            for location in data.get("locations", []):
                self.bind_entity(case, location, RelationshipType.LOCATION)

            # Build evidence list from sources
            self.log("Processing sources...")
            evidence = []
            for source_data in data.get("sources", []):
                source = self.get_or_create_source(source_data)
                if source:
                    evidence.append(
                        {
                            "source_id": source.source_id,
                            "description": source_data.get("description", ""),
                        }
                    )

            case.evidence = evidence
            case.save()

            self.log("\nImport statistics:")
            self.log(f"  Entities bound: {self.stats['entities_bound']}")
            self.log(
                "  Entities skipped (no NES id): "
                f"{self.stats['entities_skipped_no_nes_id']}"
            )
            self.log(f"  Sources created: {self.stats['sources_created']}")
            self.log(f"  Sources reused: {self.stats['sources_reused']}")

            return case
