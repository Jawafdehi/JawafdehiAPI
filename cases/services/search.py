"""Local, deterministic discovery across cases, entities, and documents."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any

from django.db.models import Prefetch, Q, TextField
from django.db.models.functions import Cast
from django.utils import timezone

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    DocumentSource,
    JawafEntity,
    RelationshipType,
)

ENTITY_TYPE_PATTERN = re.compile(r"^entity:([^/]+)/")
ENTITY_TYPES = ("person", "organization", "location")
ENTITY_TYPE_DISPLAY_NAMES = {
    "person": "People",
    "organization": "Organizations",
    "location": "Locations",
    "unknown": "Unknown",
}
TYPE_DISPLAY_NAMES = {
    "case": "Cases",
    "entity": "Entities",
    "document": "Documents",
}


def extract_entity_type(nes_id: str | None) -> str:
    """Derive a local entity type without contacting NES."""
    match = ENTITY_TYPE_PATTERN.match(nes_id or "")
    if not match:
        return "unknown"
    entity_type = match.group(1)
    return entity_type if entity_type in ENTITY_TYPES else "unknown"


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(
            f"{_flatten_text(key)} {_flatten_text(item)}" for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)


def _normalize(value: Any) -> str:
    return " ".join(_flatten_text(value).casefold().split())


def _matched_fields(query: str, searchable: dict[str, Any]) -> list[str]:
    if not query:
        return []
    terms = query.split()
    normalized_fields = {name: _normalize(value) for name, value in searchable.items()}
    haystack = " ".join(normalized_fields.values())
    if not all(term in haystack for term in terms):
        return []
    return [
        name
        for name, value in normalized_fields.items()
        if any(term in value for term in terms)
    ]


def _field_matches(query: str, value: Any) -> bool:
    normalized = _normalize(value)
    return bool(
        query
        and (query in normalized or any(term in normalized for term in query.split()))
    )


def _recent_boost(updated_at: datetime) -> int:
    days_old = max(0, (timezone.now() - updated_at).days)
    return max(0, 10 - min(10, days_old // 30))


def _excerpt(value: str | None, fallback: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    normalized = " ".join(text.split())
    if not normalized:
        return fallback
    return normalized[:237] + ("..." if len(normalized) > 237 else "")


class UnifiedSearchService:
    """Build one normalized archive search response for API consumers."""

    def search(
        self,
        *,
        request,
        q: str,
        type: list[str],
        entity_type: list[str],
        role: list[str],
        case_type: list[str],
        tags: list[str],
        sort: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        query = _normalize(q)
        page_size = min(page_size, 50)

        del request  # Public archive search is intentionally published-only.
        visible_cases = list(self._visible_cases(entity_type, role, case_type))
        cases = (
            list(self._visible_cases(entity_type, role, case_type, query))
            if query
            else visible_cases
        )
        documents = list(self._visible_documents(visible_cases, query))
        source_case_ids = self._source_case_ids(visible_cases, documents)
        case_entity_ids = {
            relationship.entity_id
            for case in visible_cases
            for relationship in case.entity_relationships.all()
        }
        visible_case_ids = {case.id for case in visible_cases}
        entities = list(
            self._visible_entities(case_entity_ids, visible_case_ids, query)
        )
        cases_by_id = {case.id: case for case in visible_cases}

        records = []
        for case in cases:
            if self._case_passes_filters(case, entity_type, role, case_type, tags):
                record = self._case_record(case, query)
                if record:
                    records.append(record)
        for entity in entities:
            if self._entity_passes_filters(
                entity, cases_by_id, entity_type, role, case_type, tags
            ):
                record = self._entity_record(entity, query, cases_by_id)
                if record:
                    records.append(record)
        for source in documents:
            if self._document_passes_filters(
                source,
                cases_by_id,
                source_case_ids,
                case_entity_ids,
                entity_type,
                role,
                case_type,
                tags,
            ):
                record = self._document_record(
                    source, query, cases_by_id, source_case_ids, case_entity_ids
                )
                if record:
                    records.append(record)
        counts = self._counts(records)
        facets = self._facets(records, cases_by_id)
        selected_records = [
            record for record in records if self._matches_type(record, type)
        ]
        self._sort(selected_records, sort)

        count = len(selected_records)
        start = (page - 1) * page_size
        results = [
            self._public_result(record)
            for record in selected_records[start : start + page_size]
        ]
        return {
            "query": q.strip(),
            "page": page,
            "page_size": page_size,
            "count": count,
            "counts": counts,
            "facets": facets,
            "results": results,
        }

    def _visible_cases(self, entity_types, roles, case_types, query=""):
        queryset = Case.objects.filter(state=CaseState.PUBLISHED)
        if case_types:
            queryset = queryset.filter(case_type__in=case_types)
        relationship_filter = self._case_relationship_queryset_filter(
            entity_types, roles
        )
        if relationship_filter:
            queryset = queryset.filter(relationship_filter)
        queryset = self._filter_cases_by_query(queryset, query)
        return queryset.prefetch_related("entity_relationships__entity").distinct()

    def _case_relationship_queryset_filter(self, entity_types, roles):
        relationship_filter = Q()
        if roles:
            relationship_filter &= Q(entity_relationships__relationship_type__in=roles)
        if entity_types and "unknown" not in entity_types:
            entity_filter = Q()
            for entity_type in entity_types:
                entity_filter |= Q(
                    entity_relationships__entity__nes_id__startswith=(
                        f"entity:{entity_type}/"
                    )
                )
            relationship_filter &= entity_filter
        return relationship_filter

    def _filter_cases_by_query(self, queryset, query):
        if not query:
            return queryset

        queryset = queryset.annotate(
            tags_text=Cast("tags", output_field=TextField()),
            key_allegations_text=Cast("key_allegations", output_field=TextField()),
            court_cases_text=Cast("court_cases", output_field=TextField()),
        )
        for term in query.split():
            queryset = queryset.filter(
                Q(title__icontains=term)
                | Q(short_description__icontains=term)
                | Q(description__icontains=term)
                | Q(case_id__icontains=term)
                | Q(tags_text__icontains=term)
                | Q(key_allegations_text__icontains=term)
                | Q(court_cases_text__icontains=term)
                | Q(entity_relationships__entity__display_name__icontains=term)
                | Q(entity_relationships__entity__nes_id__icontains=term)
                | Q(entity_relationships__notes__icontains=term)
                | Q(entity_relationships__relationship_type__icontains=term)
            )
        return queryset

    def _visible_entities(self, entity_ids, visible_case_ids, query):
        relationships = CaseEntityRelationship.objects.filter(
            case_id__in=visible_case_ids
        ).select_related("case")
        queryset = JawafEntity.objects.filter(id__in=entity_ids)
        for term in query.split():
            queryset = queryset.filter(
                Q(display_name__icontains=term)
                | Q(nes_id__icontains=term)
                | (
                    Q(case_relationships__case_id__in=visible_case_ids)
                    & (
                        Q(case_relationships__case__title__icontains=term)
                        | Q(case_relationships__relationship_type__icontains=term)
                        | Q(case_relationships__notes__icontains=term)
                    )
                )
            )
        return queryset.prefetch_related(
            Prefetch("case_relationships", queryset=relationships)
        ).distinct()

    def _visible_documents(self, cases, query):
        source_ids = {
            item["source_id"]
            for case in cases
            for item in (case.evidence or [])
            if isinstance(item, dict) and item.get("source_id")
        }
        queryset = DocumentSource.objects.filter(
            source_id__in=source_ids, is_deleted=False
        )
        for term in query.split():
            queryset = queryset.filter(
                Q(title__icontains=term)
                | Q(description__icontains=term)
                | Q(source_id__icontains=term)
                | Q(source_type__icontains=term)
                | Q(related_entities__display_name__icontains=term)
                | Q(related_entities__nes_id__icontains=term)
            )
        return queryset.prefetch_related("related_entities").distinct()

    def _source_case_ids(self, cases, documents):
        source_ids = {source.source_id for source in documents}
        mapping = {source_id: set() for source_id in source_ids}
        for case in cases:
            for item in case.evidence or []:
                if isinstance(item, dict) and item.get("source_id") in mapping:
                    mapping[item["source_id"]].add(case.id)
        return mapping

    def _case_passes_filters(self, case, entity_types, roles, case_types, tags):
        relationships = list(case.entity_relationships.all())
        if case_types and case.case_type not in case_types:
            return False
        if tags and not any(tag in (case.tags or []) for tag in tags):
            return False
        if any((roles, entity_types)) and not any(
            self._relationship_passes_filters(relationship, entity_types, roles)
            for relationship in relationships
        ):
            return False
        return True

    def _relationship_passes_filters(self, relationship, entity_types, roles):
        return (not roles or relationship.relationship_type in roles) and (
            not entity_types
            or extract_entity_type(relationship.entity.nes_id) in entity_types
        )

    def _entity_passes_filters(
        self, entity, cases_by_id, entity_types, roles, case_types, tags
    ):
        if entity_types and extract_entity_type(entity.nes_id) not in entity_types:
            return False
        has_case_filter = any((roles, case_types, tags))
        if not has_case_filter:
            return True
        return any(
            relationship.case_id in cases_by_id
            and (not roles or relationship.relationship_type in roles)
            and self._case_passes_filters(relationship.case, [], [], case_types, tags)
            for relationship in entity.case_relationships.all()
        )

    def _document_passes_filters(
        self,
        source,
        cases_by_id,
        source_case_ids,
        visible_entity_ids,
        entity_types,
        roles,
        case_types,
        tags,
    ):
        related_cases = [
            cases_by_id[case_id]
            for case_id in source_case_ids.get(source.source_id, set())
            if case_id in cases_by_id
        ]
        if any((case_types, tags)) and not any(
            self._case_passes_filters(case, [], [], case_types, tags)
            for case in related_cases
        ):
            return False
        related_entities = self._visible_document_entities(source, visible_entity_ids)
        if not any((entity_types, roles)):
            return True
        related_entity_ids = {entity.id for entity in related_entities}
        if not any(
            relationship.entity_id in related_entity_ids
            and self._relationship_passes_filters(relationship, entity_types, roles)
            for case in related_cases
            for relationship in case.entity_relationships.all()
        ):
            return False
        return True

    def _case_record(self, case, query):
        relationships = list(case.entity_relationships.all())
        searchable = {
            "title": case.title,
            "short_description": case.short_description,
            "description": case.description,
            "key_allegations": case.key_allegations,
            "tags": case.tags,
            "case_id": case.case_id,
            "court_cases": case.court_cases,
            "entities": [
                (relationship.entity.display_name, relationship.entity.nes_id)
                for relationship in relationships
            ],
            "relationship_notes": [
                relationship.notes for relationship in relationships
            ],
        }
        matched_fields = _matched_fields(query, searchable)
        if query and not matched_fields:
            return None
        score = self._score_case(case, query, searchable)
        return {
            "kind": "case",
            "case_ids": {case.id},
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "result": {
                "result_type": "case",
                "id": case.id,
                "title": case.title,
                "description": _excerpt(
                    case.short_description or case.description,
                    "Published accountability case.",
                ),
                "url": f"/case/{case.slug}",
                "api_url": f"/api/cases/{case.slug}/",
                "slug": case.slug,
                "state": case.state,
                "case_type": case.case_type,
                "date": (
                    case.case_start_date.isoformat()
                    if case.case_start_date
                    else case.created_at.date().isoformat()
                ),
                "tags": list(case.tags or []),
                "entities": [
                    {
                        "id": relationship.entity_id,
                        "display_name": relationship.entity.display_name,
                        "nes_id": relationship.entity.nes_id,
                        "relationship_type": relationship.relationship_type,
                    }
                    for relationship in relationships
                ],
                "matched_fields": matched_fields,
                "score": score,
            },
        }

    def _entity_record(self, entity, query, cases_by_id):
        relationships = [
            relationship
            for relationship in entity.case_relationships.all()
            if relationship.case_id in cases_by_id
        ]
        searchable = {
            "display_name": entity.display_name,
            "nes_id": entity.nes_id,
            "related_case_titles": [
                relationship.case.title for relationship in relationships
            ],
            "relationship_type": [
                relationship.relationship_type for relationship in relationships
            ],
            "relationship_notes": [
                relationship.notes for relationship in relationships
            ],
        }
        matched_fields = _matched_fields(query, searchable)
        if query and not matched_fields:
            return None
        role_counts = Counter(
            relationship.relationship_type for relationship in relationships
        )
        score = self._score_entity(entity, query, searchable)
        return {
            "kind": "entity",
            "case_ids": {relationship.case_id for relationship in relationships},
            "created_at": entity.created_at,
            "updated_at": entity.updated_at,
            "entity_type": extract_entity_type(entity.nes_id),
            "result": {
                "result_type": "entity",
                "id": entity.id,
                "title": entity.display_name or entity.nes_id or "Unknown entity",
                "description": "Tracked public entity connected to accountability records.",
                "url": f"/entity/{entity.id}",
                "api_url": f"/api/entities/{entity.id}/",
                "entity_type": extract_entity_type(entity.nes_id),
                "nes_id": entity.nes_id,
                "role_counts": dict(role_counts),
                "related_case_count": len(
                    {relationship.case_id for relationship in relationships}
                ),
                "matched_fields": matched_fields,
                "score": score,
            },
        }

    def _document_record(
        self, source, query, cases_by_id, source_case_ids, visible_entity_ids
    ):
        del cases_by_id
        related_entities = self._visible_document_entities(source, visible_entity_ids)
        searchable = {
            "title": source.title,
            "description": source.description,
            "source_id": source.source_id,
            "source_type": source.source_type,
            "related_entities": [
                (entity.display_name, entity.nes_id) for entity in related_entities
            ],
        }
        matched_fields = _matched_fields(query, searchable)
        if query and not matched_fields:
            return None
        score = self._score_document(source, query, searchable)
        return {
            "kind": "document",
            "case_ids": set(source_case_ids.get(source.source_id, set())),
            "created_at": source.created_at,
            "updated_at": source.updated_at,
            "result": {
                "result_type": "document",
                "id": source.id,
                "title": source.title,
                "description": _excerpt(
                    source.description, "Evidence document linked to an archive case."
                ),
                "url": f"/api/sources/{source.source_id}/",
                "api_url": f"/api/sources/{source.source_id}/",
                "source_id": source.source_id,
                "source_type": source.source_type,
                "related_entities": [
                    {
                        "id": entity.id,
                        "display_name": entity.display_name,
                        "nes_id": entity.nes_id,
                    }
                    for entity in related_entities
                ],
                "matched_fields": matched_fields,
                "score": score,
            },
        }

    def _score_case(self, case, query, searchable):
        score = _recent_boost(case.updated_at)
        if query == _normalize(case.title):
            score += 100
        elif _field_matches(query, case.title):
            score += 70
        weights = {
            "entities": 50,
            "key_allegations": 45,
            "tags": 40,
            "case_id": 35,
            "court_cases": 35,
            "short_description": 20,
            "description": 20,
            "relationship_notes": 15,
        }
        return score + sum(
            weight
            for field, weight in weights.items()
            if _field_matches(query, searchable[field])
        )

    def _score_entity(self, entity, query, searchable):
        score = _recent_boost(entity.updated_at)
        if query == _normalize(entity.display_name):
            score += 100
        elif _field_matches(query, entity.display_name):
            score += 70
        weights = {
            "nes_id": 50,
            "related_case_titles": 35,
            "relationship_type": 20,
            "relationship_notes": 15,
        }
        return score + sum(
            weight
            for field, weight in weights.items()
            if _field_matches(query, searchable[field])
        )

    def _score_document(self, source, query, searchable):
        score = _recent_boost(source.updated_at)
        if query == _normalize(source.title):
            score += 100
        elif _field_matches(query, source.title):
            score += 70
        weights = {
            "related_entities": 50,
            "source_id": 35,
            "source_type": 35,
            "description": 20,
        }
        return score + sum(
            weight
            for field, weight in weights.items()
            if _field_matches(query, searchable[field])
        )

    def _counts(self, records):
        counter = Counter(record["kind"] for record in records)
        return {
            "all": len(records),
            "cases": counter["case"],
            "entities": counter["entity"],
            "documents": counter["document"],
        }

    def _facets(self, records, cases_by_id):
        type_counts = Counter(record["kind"] for record in records)
        entity_type_counts = Counter(
            record["entity_type"] for record in records if record["kind"] == "entity"
        )
        related_case_ids = set().union(
            *(record["case_ids"] for record in records), set()
        )
        related_cases = [
            cases_by_id[case_id]
            for case_id in related_case_ids
            if case_id in cases_by_id
        ]
        case_type_counts = Counter(case.case_type for case in related_cases)
        tag_counts = Counter(tag for case in related_cases for tag in (case.tags or []))
        relationships = {
            relationship.id: relationship
            for case in related_cases
            for relationship in case.entity_relationships.all()
        }
        role_counts = Counter(
            relationship.relationship_type for relationship in relationships.values()
        )
        return {
            "type": [
                self._facet_item(name, TYPE_DISPLAY_NAMES[name], type_counts[name])
                for name in ("case", "entity", "document")
            ],
            "entity_type": [
                self._facet_item(
                    name, ENTITY_TYPE_DISPLAY_NAMES[name], entity_type_counts[name]
                )
                for name in ("person", "organization", "location", "unknown")
            ],
            "role": [
                self._facet_item(name, label, role_counts[name])
                for name, label in RelationshipType.choices
            ],
            "case_type": [
                self._facet_item(name, label, case_type_counts[name])
                for name, label in CaseType.choices
            ],
            "tags": [
                self._facet_item(name, self._display_tag(name), count)
                for name, count in sorted(tag_counts.items())
            ],
        }

    def _matches_type(self, record, search_type):
        if not search_type:
            return True
        return record["kind"] in search_type

    def _sort(self, records, sort):
        if sort == "newest":
            records.sort(
                key=lambda record: (
                    -record["created_at"].timestamp(),
                    record["kind"],
                    record["result"]["id"],
                )
            )
        elif sort == "oldest":
            records.sort(
                key=lambda record: (
                    record["created_at"].timestamp(),
                    record["kind"],
                    record["result"]["id"],
                )
            )
        elif sort == "title":
            records.sort(
                key=lambda record: (
                    record["result"]["title"].casefold(),
                    record["kind"],
                    record["result"]["id"],
                )
            )
        else:
            records.sort(
                key=lambda record: (
                    -record["result"]["score"],
                    -record["updated_at"].timestamp(),
                    record["kind"],
                    record["result"]["id"],
                )
            )

    def _facet_item(self, name, display_name, count):
        return {"name": name, "display_name": display_name, "count": count}

    def _visible_document_entities(self, source, visible_entity_ids):
        return [
            entity
            for entity in source.related_entities.all()
            if entity.id in visible_entity_ids
        ]

    def _display_tag(self, tag):
        return tag.replace("-", " ").capitalize()

    def _public_result(self, record):
        return record["result"]
