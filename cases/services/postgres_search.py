"""PostgreSQL-native archive search with bounded result hydration."""

from __future__ import annotations

from typing import Any

from django.contrib.postgres.search import (
    SearchQuery,
    SearchRank,
    SearchVector,
    TrigramWordSimilarity,
)
from django.db.models import (
    Case as DatabaseCase,
)
from django.db.models import (
    CharField,
    Count,
    Exists,
    F,
    FloatField,
    Func,
    OuterRef,
    Prefetch,
    Q,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Greatest, Lower

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseEvidenceSource,
    CaseState,
    CaseType,
    DocumentSource,
    JawafEntity,
    RelationshipType,
)
from cases.services.search import (
    ENTITY_TYPE_DISPLAY_NAMES,
    TYPE_DISPLAY_NAMES,
    LegacyUnifiedSearchService,
)


class PostgresUnifiedSearchService(LegacyUnifiedSearchService):
    """Search archive candidates in PostgreSQL and hydrate one page."""

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
        del request
        query = self._normalize_query(q)
        page_size = min(page_size, 50)

        cases = self._case_candidates(query, entity_type, role, case_type, tags)
        entities = self._entity_candidates(query, entity_type, role, case_type, tags)
        documents = self._document_candidates(query, entity_type, role, case_type, tags)

        counts = {
            "cases": cases.count(),
            "entities": entities.count(),
            "documents": documents.count(),
        }
        counts["all"] = sum(counts.values())
        facets = self._database_facets(
            cases,
            entities,
            documents,
            counts=counts,
            entity_types=entity_type,
            roles=role,
            case_types=case_type,
            tags=tags,
        )

        selected = {
            "case": cases,
            "entity": entities,
            "document": documents,
        }
        selected_types = type or list(selected)
        combined = self._combine_candidates(
            [selected[name] for name in selected_types], sort
        )
        count_keys = {
            "case": "cases",
            "entity": "entities",
            "document": "documents",
        }
        count = sum(counts[count_keys[name]] for name in selected_types)
        start = (page - 1) * page_size
        rows = list(combined[start : start + page_size])
        results = self._hydrate_page(
            rows,
            query=query,
            entity_types=entity_type,
            roles=role,
            case_types=case_type,
            tags=tags,
        )

        return {
            "query": q.strip(),
            "page": page,
            "page_size": page_size,
            "count": count,
            "counts": counts,
            "facets": facets,
            "results": results,
        }

    def _normalize_query(self, query):
        return " ".join((query or "").casefold().split())

    def _base_cases(self, entity_types, roles, case_types, tags):
        queryset = Case.objects.filter(state=CaseState.PUBLISHED)
        if case_types:
            queryset = queryset.filter(case_type__in=case_types)
        if tags:
            tag_filter = Q()
            for tag in tags:
                tag_filter |= Q(tags__contains=[tag])
            queryset = queryset.filter(tag_filter)

        relationship_filter = self._case_relationship_queryset_filter(
            entity_types, roles
        )
        if relationship_filter:
            queryset = queryset.filter(relationship_filter)
        return queryset.distinct()

    def _case_candidates(self, query, entity_types, roles, case_types, tags):
        queryset = self._base_cases(entity_types, roles, case_types, tags)
        vector = SearchVector(
            "title",
            "short_description",
            "description",
            "case_id",
            config="simple",
        )
        queryset = queryset.annotate(
            search_vector=vector,
            tags_text=Cast("tags", output_field=TextField()),
            allegations_text=Cast("key_allegations", output_field=TextField()),
            court_cases_text=Cast("court_cases", output_field=TextField()),
        )
        if query:
            queryset = queryset.annotate(
                title_similarity=Coalesce(
                    TrigramWordSimilarity(Value(query), "title"),
                    Value(0.0),
                ),
                identifier_similarity=Coalesce(
                    TrigramWordSimilarity(Value(query), "case_id"),
                    Value(0.0),
                ),
            )
            relation_match = self._case_relation_match(query)
            queryset = queryset.annotate(relation_match=Exists(relation_match))
            for index, term in enumerate(query.split()):
                term_relation = self._case_relation_match(term)
                annotation = f"relation_term_{index}"
                queryset = queryset.annotate(**{annotation: Exists(term_relation)})
                queryset = queryset.filter(
                    Q(search_vector=SearchQuery(term, config="simple"))
                    | Q(title__trigram_word_similar=term)
                    | Q(case_id__trigram_word_similar=term)
                    | Q(tags_text__icontains=term)
                    | Q(allegations_text__icontains=term)
                    | Q(court_cases_text__icontains=term)
                    | Q(**{annotation: True})
                )
            rank = SearchRank(
                vector,
                SearchQuery(query, config="simple"),
                cover_density=True,
            )
            queryset = queryset.annotate(
                archive_score=(
                    Coalesce(rank, Value(0.0)) * Value(4.0)
                    + Greatest(
                        F("title_similarity"),
                        F("identifier_similarity"),
                        output_field=FloatField(),
                    )
                    * Value(2.0)
                    + DatabaseCase(
                        When(relation_match=True, then=Value(0.5)),
                        default=Value(0.0),
                        output_field=FloatField(),
                    )
                )
            )
        else:
            queryset = queryset.annotate(archive_score=Value(0.0))
        return self._candidate_values(queryset, "case", "title")

    def _case_relation_match(self, query):
        relation_queryset = CaseEntityRelationship.objects.filter(
            case_id=OuterRef("pk")
        )
        for term in query.split():
            relation_queryset = relation_queryset.filter(
                Q(entity__display_name__icontains=term)
                | Q(entity__nes_id__icontains=term)
                | Q(notes__icontains=term)
                | Q(relationship_type__icontains=term)
            )
        return relation_queryset

    def _entity_candidates(self, query, entity_types, roles, case_types, tags):
        visible_relationships = CaseEntityRelationship.objects.filter(
            case__state=CaseState.PUBLISHED
        )
        if roles:
            visible_relationships = visible_relationships.filter(
                relationship_type__in=roles
            )
        if case_types:
            visible_relationships = visible_relationships.filter(
                case__case_type__in=case_types
            )
        if tags:
            tag_filter = Q()
            for tag in tags:
                tag_filter |= Q(case__tags__contains=[tag])
            visible_relationships = visible_relationships.filter(tag_filter)

        queryset = JawafEntity.objects.annotate(
            visible=Exists(visible_relationships.filter(entity_id=OuterRef("pk")))
        ).filter(visible=True)
        if entity_types:
            type_filter = Q()
            for name in entity_types:
                if name == "unknown":
                    known = Q()
                    for known_type in ("person", "organization", "location"):
                        known |= Q(nes_id__startswith=f"entity:{known_type}/")
                    type_filter |= ~known
                else:
                    type_filter |= Q(nes_id__startswith=f"entity:{name}/")
            queryset = queryset.filter(type_filter)

        vector = SearchVector("display_name", "nes_id", config="simple")
        queryset = queryset.annotate(search_vector=vector)
        if query:
            queryset = queryset.annotate(
                title_similarity=Coalesce(
                    TrigramWordSimilarity(Value(query), "display_name"),
                    Value(0.0),
                ),
                identifier_similarity=Coalesce(
                    TrigramWordSimilarity(Value(query), "nes_id"),
                    Value(0.0),
                ),
            )
            related_match = visible_relationships.filter(entity_id=OuterRef("pk"))
            for term in query.split():
                related_match = related_match.filter(
                    Q(case__title__icontains=term)
                    | Q(relationship_type__icontains=term)
                    | Q(notes__icontains=term)
                )
            queryset = queryset.annotate(related_match=Exists(related_match))
            for term in query.split():
                queryset = queryset.filter(
                    Q(search_vector=SearchQuery(term, config="simple"))
                    | Q(display_name__trigram_word_similar=term)
                    | Q(nes_id__trigram_word_similar=term)
                    | Q(related_match=True)
                )
            rank = SearchRank(
                vector,
                SearchQuery(query, config="simple"),
                cover_density=True,
            )
            queryset = queryset.annotate(
                archive_score=(
                    Coalesce(rank, Value(0.0)) * Value(4.0)
                    + Greatest(
                        F("title_similarity"),
                        F("identifier_similarity"),
                        output_field=FloatField(),
                    )
                    * Value(2.0)
                    + DatabaseCase(
                        When(related_match=True, then=Value(0.5)),
                        default=Value(0.0),
                        output_field=FloatField(),
                    )
                )
            )
        else:
            queryset = queryset.annotate(archive_score=Value(0.0))
        return self._candidate_values(queryset, "entity", "display_name")

    def _document_candidates(self, query, entity_types, roles, case_types, tags):
        visible_links = self._visible_evidence_links(
            entity_types,
            roles,
            case_types,
            tags,
            document_source_outer_ref=True,
        )
        queryset = (
            DocumentSource.objects.filter(is_deleted=False)
            .annotate(visible=Exists(visible_links))
            .filter(visible=True)
        )
        vector = SearchVector("title", "description", "source_id", config="simple")
        queryset = queryset.annotate(search_vector=vector)
        if query:
            queryset = queryset.annotate(
                title_similarity=Coalesce(
                    TrigramWordSimilarity(Value(query), "title"),
                    Value(0.0),
                ),
                identifier_similarity=Coalesce(
                    TrigramWordSimilarity(Value(query), "source_id"),
                    Value(0.0),
                ),
            )
            for index, term in enumerate(query.split()):
                related = DocumentSource.related_entities.through.objects.filter(
                    documentsource_id=OuterRef("pk")
                ).filter(
                    Q(jawafentity__display_name__icontains=term)
                    | Q(jawafentity__nes_id__icontains=term)
                )
                annotation = f"related_term_{index}"
                queryset = queryset.annotate(**{annotation: Exists(related)})
                queryset = queryset.filter(
                    Q(search_vector=SearchQuery(term, config="simple"))
                    | Q(title__trigram_word_similar=term)
                    | Q(source_id__trigram_word_similar=term)
                    | Q(source_type__icontains=term)
                    | Q(**{annotation: True})
                )
            rank = SearchRank(
                vector,
                SearchQuery(query, config="simple"),
                cover_density=True,
            )
            queryset = queryset.annotate(
                archive_score=(
                    Coalesce(rank, Value(0.0)) * Value(4.0)
                    + Greatest(
                        F("title_similarity"),
                        F("identifier_similarity"),
                        output_field=FloatField(),
                    )
                    * Value(2.0)
                )
            )
        else:
            queryset = queryset.annotate(archive_score=Value(0.0))
        return self._candidate_values(queryset, "document", "title")

    def _visible_evidence_links(
        self,
        entity_types,
        roles,
        case_types,
        tags,
        *,
        document_source_outer_ref=False,
    ):
        queryset = CaseEvidenceSource.objects.filter(case__state=CaseState.PUBLISHED)
        if document_source_outer_ref:
            queryset = queryset.filter(document_source_id=OuterRef("pk"))
        if case_types:
            queryset = queryset.filter(case__case_type__in=case_types)
        if tags:
            tag_filter = Q()
            for tag in tags:
                tag_filter |= Q(case__tags__contains=[tag])
            queryset = queryset.filter(tag_filter)
        if roles or entity_types:
            relationship_filter = Q()
            if roles:
                relationship_filter &= Q(
                    case__entity_relationships__relationship_type__in=roles
                )
            if entity_types:
                relationship_filter &= self._entity_type_filter(
                    "case__entity_relationships__entity__nes_id",
                    entity_types,
                )
            if document_source_outer_ref:
                relationship_filter &= Q(
                    case__entity_relationships__entity__document_sources=OuterRef("pk")
                )
            queryset = queryset.filter(relationship_filter)
        return queryset

    def _entity_type_filter(self, field_name, entity_types):
        entity_filter = Q()
        for name in entity_types:
            if name == "unknown":
                known = Q()
                for known_type in ("person", "organization", "location"):
                    known |= Q(**{f"{field_name}__startswith": f"entity:{known_type}/"})
                entity_filter |= ~known
            else:
                entity_filter |= Q(**{f"{field_name}__startswith": f"entity:{name}/"})
        return entity_filter

    def _candidate_values(self, queryset, result_type, title_field):
        return queryset.annotate(
            result_type=Value(result_type),
            title_sort=Lower(Coalesce(title_field, Value(""))),
        ).values(
            "id",
            "result_type",
            "archive_score",
            "created_at",
            "updated_at",
            "title_sort",
        )

    def _combine_candidates(self, querysets, sort):
        combined = querysets[0]
        for queryset in querysets[1:]:
            combined = combined.union(queryset, all=True)
        if sort == "newest":
            return combined.order_by("-created_at", "result_type", "id")
        if sort == "oldest":
            return combined.order_by("created_at", "result_type", "id")
        if sort == "title":
            return combined.order_by("title_sort", "result_type", "id")
        return combined.order_by("-archive_score", "-updated_at", "result_type", "id")

    def _database_facets(
        self,
        cases,
        entities,
        documents,
        *,
        counts,
        entity_types,
        roles,
        case_types,
        tags,
    ):
        base_cases = self._base_cases(entity_types, roles, case_types, tags)
        related_cases = base_cases.filter(
            Q(id__in=cases.values("id"))
            | Q(entity_relationships__entity_id__in=entities.values("id"))
            | Q(evidence_links__document_source_id__in=documents.values("id"))
        ).distinct()

        case_type_counts = {
            row["case_type"]: row["count"]
            for row in related_cases.values("case_type").annotate(
                count=Count("id", distinct=True)
            )
        }
        tag_counts = self._tag_counts(related_cases)
        role_counts = {
            name: count
            for name, count in (
                CaseEntityRelationship.objects.filter(
                    case_id__in=related_cases.values("id")
                )
                .values("relationship_type")
                .annotate(count=Count("id"))
                .values_list("relationship_type", "count")
            )
        }
        entity_type_counts = {
            row["archive_entity_type"]: row["count"]
            for row in (
                JawafEntity.objects.filter(id__in=entities.values("id"))
                .annotate(
                    archive_entity_type=DatabaseCase(
                        When(
                            nes_id__startswith="entity:person/",
                            then=Value("person"),
                        ),
                        When(
                            nes_id__startswith="entity:organization/",
                            then=Value("organization"),
                        ),
                        When(
                            nes_id__startswith="entity:location/",
                            then=Value("location"),
                        ),
                        default=Value("unknown"),
                        output_field=CharField(),
                    )
                )
                .values("archive_entity_type")
                .annotate(count=Count("id"))
            )
        }
        type_counts = {
            "case": counts["cases"],
            "entity": counts["entities"],
            "document": counts["documents"],
        }
        return {
            "type": [
                self._facet_item(name, TYPE_DISPLAY_NAMES[name], type_counts[name])
                for name in ("case", "entity", "document")
            ],
            "entity_type": [
                self._facet_item(
                    name,
                    ENTITY_TYPE_DISPLAY_NAMES[name],
                    entity_type_counts[name],
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

    def _tag_counts(self, related_cases):
        return dict(
            related_cases.annotate(
                tag_value=Func(
                    F("tags"),
                    function="jsonb_array_elements_text",
                    output_field=TextField(),
                )
            )
            .values("tag_value")
            .annotate(tag_count=Count("id"))
            .order_by("tag_value")
            .values_list("tag_value", "tag_count")
        )

    def _case_ids_for_sources(self, source_ids):
        if not source_ids:
            return set()
        return set(
            CaseEvidenceSource.objects.filter(
                case__state=CaseState.PUBLISHED,
                document_source__source_id__in=source_ids,
            )
            .values_list("case_id", flat=True)
            .distinct()
        )

    def _hydrate_page(
        self,
        rows,
        *,
        query,
        entity_types,
        roles,
        case_types,
        tags,
    ):
        ids_by_type = {
            "case": [],
            "entity": [],
            "document": [],
        }
        for row in rows:
            ids_by_type[row["result_type"]].append(row["id"])

        base_cases = self._base_cases(entity_types, roles, case_types, tags)
        related_case_ids = set(ids_by_type["case"])
        if ids_by_type["entity"]:
            related_case_ids.update(
                base_cases.filter(
                    entity_relationships__entity_id__in=ids_by_type["entity"]
                ).values_list("id", flat=True)
            )
        source_ids = list(
            DocumentSource.objects.filter(id__in=ids_by_type["document"]).values_list(
                "source_id", flat=True
            )
        )
        related_case_ids.update(self._case_ids_for_sources(source_ids))

        hydrated_cases = list(
            base_cases.filter(id__in=related_case_ids)
            .prefetch_related("entity_relationships__entity")
            .distinct()
        )
        cases_by_id = {case.id: case for case in hydrated_cases}
        visible_relationships = CaseEntityRelationship.objects.filter(
            case_id__in=cases_by_id
        ).select_related("case", "entity")
        entities = {
            entity.id: entity
            for entity in JawafEntity.objects.filter(
                id__in=ids_by_type["entity"]
            ).prefetch_related(
                Prefetch(
                    "case_relationships",
                    queryset=visible_relationships,
                )
            )
        }
        visible_entity_ids = set(
            visible_relationships.values_list("entity_id", flat=True)
        )
        documents = {
            source.id: source
            for source in DocumentSource.objects.filter(
                id__in=ids_by_type["document"]
            ).prefetch_related("related_entities")
        }
        source_case_ids = self._source_case_ids(hydrated_cases, documents.values())

        records = {}
        for case_id in ids_by_type["case"]:
            case = cases_by_id.get(case_id)
            if case:
                records[("case", case_id)] = self._record_with_rank(
                    self._case_record, case, query
                )
        for entity_id in ids_by_type["entity"]:
            entity = entities.get(entity_id)
            if entity:
                records[("entity", entity_id)] = self._record_with_rank(
                    self._entity_record, entity, query, cases_by_id
                )
        for document_id in ids_by_type["document"]:
            source = documents.get(document_id)
            if source:
                records[("document", document_id)] = self._record_with_rank(
                    self._document_record,
                    source,
                    query,
                    cases_by_id,
                    source_case_ids,
                    visible_entity_ids,
                )

        results = []
        for row in rows:
            record = records.get((row["result_type"], row["id"]))
            if not record:
                continue
            result = self._public_result(record)
            result["score"] = max(
                result["score"],
                int(round(float(row["archive_score"] or 0) * 1000)),
            )
            results.append(result)
        return results

    def _record_with_rank(self, builder, instance, query, *args):
        record = builder(instance, query, *args)
        if record is None:
            record = builder(instance, "", *args)
        return record
