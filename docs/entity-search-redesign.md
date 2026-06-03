# Entity Search Redesign

**Status**: Draft
**Created**: 2024-12-04
**Author**: System
**Related**: API_DOCUMENTATION.md, ENTITY_MIGRATION_SUMMARY.md

## Overview

This document outlines the redesign of entity search functionality in Jawafdehi to simplify the user experience and improve performance by moving from server-side NES API queries to client-side filtering of Jawafdehi entities.

## Current State (Before Changes)

### Frontend (Jawafdehi)
- **Entity Source**: Fetches entities from Nepal Entity Service (NES) API
- **Filtering**:
  - Entity type filter (person, political_party)
  - Server-side search via NES API query parameter
  - Client-side filtering by entity IDs that have cases
- **Search Flow**:
  1. Fetch entity IDs with cases from JDS API
  2. Query NES API with entity_type, sub_type, query, and entity_ids filters
  3. Apply additional client-side filtering for names
  4. Filter to only show entities with cases
- **Display**: Shows random allegation and case counts

### Backend (JawafdehiAPI)
- **Endpoint**: `GET /api/entities/`
- **Model**: `JawafEntity` with fields:
  - `id` (primary key)
  - `nes_id` (nullable, reference to NES)
  - `display_name` (nullable, custom name)
- **Serializer**: `JawafEntitySerializer` returns `id`, `nes_id`, `display_name`
- **Queryset**: Returns **all entities**, ordered by `-created_at` (not filtered by case association)
- **Search**: Full-text search on `nes_id` and `display_name` fields
- **Pagination**: 50 items per page

### Issues
1. **Complexity**: Frontend queries NES API first, then filters by JDS entity IDs
2. **Performance**: Multiple API calls and complex filtering logic
3. **Inconsistency**: Entity type filtering happens on NES side, not JDS side
4. **Missing Data**: No case count information in entity responses (shows random numbers)
5. **Unnecessary Filter**: Entity type filter not useful with small dataset
6. **Wrong Source**: Should fetch from JDS (source of truth for entities in cases), not NES
7. **No Backend Filtering**: Backend returns all entities, frontend has to filter by case association

## Proposed Changes

### 1. Remove Entity Type Filter

**Rationale**: With only a few entities in the system currently, filtering by type adds unnecessary complexity without significant benefit.

**Frontend Changes**:
- Remove entity type radio buttons (person/political_party)
- Remove `entityTypeFilter` state and related logic
- Remove entity type from URL search params
- Simplify UI to focus on search

### 2. Switch to Client-Side Search

**Rationale**: With a small number of entities, client-side filtering is faster and simpler than server-side queries.

**Frontend Changes**:
- Fetch JawafEntities from `/api/entities/` with pagination (default 50 per page)
- Load More button to fetch additional pages
- For entities with `nes_id`, fetch NES entity data to display rich profiles
- Implement client-side search filtering on:
  - `display_name` field (from JawafEntity)
  - `nes_id` field (from JawafEntity)
  - Entity names from NES data (if available)
- Keep debounced search for smooth UX
- Search filters across all loaded entities (not just current page)

**Backend Changes**:
- **Filter entities by case association**: Update `JawafEntityViewSet` queryset to only return entities that appear in published cases
- Uses existing Django REST Framework pagination (50 items per page)
- Entities must be in `alleged_entities` or `related_entities` of at least one published case
- **Note**: Location entities are excluded from the entity list

**Note**: NES API calls are made one at a time (N+1 pattern). This is acceptable for small datasets but should be optimized with a batch endpoint in the future.

### 3. Enrich Entities with NES Data

**Rationale**: JawafEntities may have `nes_id` references. We need to fetch NES data to display rich entity profiles (names, pictures, attributes).

**Frontend Changes**:

```typescript
// Fetch and enrich entities with pagination
useEffect(() => {
  const fetchEntities = async (pageNum: number) => {
    if (pageNum === 1) {
      setLoading(true);
    } else {
      setLoadingMore(true);
    }

    try {
      // 1. Fetch JawafEntities from JDS API with pagination
      const response = await fetch(`/api/entities/?page=${pageNum}`);
      const data = await response.json();
      const jawafEntities = data.results || [];
      const hasNext = data.next !== null;

      // 2. Enrich with NES data for entities that have nes_id
      // TODO: Replace with batch NES API call when available
      const enrichedEntities = await Promise.all(
        jawafEntities.map(async (jawafEntity) => {
          if (jawafEntity.nes_id) {
            try {
              const nesEntity = await getEntityById(jawafEntity.nes_id);
              return { jawafEntity, nesEntity };
            } catch (error) {
              console.warn(`Failed to fetch NES data for ${jawafEntity.nes_id}`);
              return { jawafEntity };
            }
          }
          return { jawafEntity };
        })
      );

      // 3. Client-side search filtering
      let filtered = enrichedEntities;
      if (debouncedSearchQuery) {
        const query = debouncedSearchQuery.toLowerCase();
        filtered = enrichedEntities.filter(({ jawafEntity, nesEntity }) => {
          // Search in JawafEntity fields
          if (jawafEntity.display_name?.toLowerCase().includes(query)) return true;
          if (jawafEntity.nes_id?.toLowerCase().includes(query)) return true;
          // Search in NES entity names if available
          if (nesEntity?.names) {
            const nameEn = nesEntity.names.find(n => n.lang === 'en')?.name?.toLowerCase() || '';
            const nameNe = nesEntity.names.find(n => n.lang === 'ne')?.name?.toLowerCase() || '';
            if (nameEn.includes(query) || nameNe.includes(query)) return true;
          }
          return false;
        });
      }

      // Append to existing entities
      setAllEntities(prev => pageNum === 1 ? enrichedEntities : [...prev, ...enrichedEntities]);
      setHasMore(hasNext);
    } catch (error) {
      console.error("Failed to fetch entities:", error);
      toast.error(t("entities.fetchError"));
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  };

  fetchEntities(page);
}, [page, t]);

// 3. Apply search filter when entities change
useEffect(() => {
  let filtered = allEntities;
  if (debouncedSearchQuery) {
    const query = debouncedSearchQuery.toLowerCase();
    filtered = allEntities.filter(({ jawafEntity, nesEntity }) => {
      // Search in JawafEntity and NES fields
      // ... (same as before)
    });
  }

  // 4. Sort alphabetically
  const sorted = [...filtered].sort((a, b) => {
    const nameA = a.jawafEntity.display_name || a.jawafEntity.nes_id || '';
    const nameB = b.jawafEntity.display_name || b.jawafEntity.nes_id || '';
    return nameA.localeCompare(nameB);
  });

  setEntities(sorted);
}, [allEntities, debouncedSearchQuery]);
```

**Pagination Behavior**:
- Initial load: Fetch first page of entities
- Load More button: Fetch next page and append to list
- Search: Filter across all loaded entities (client-side)
- Default page size: 50 entities per page (Django REST Framework default)

## Implementation Plan

### Phase 1: Backend Changes
1. Update `JawafEntityViewSet.get_queryset()` to filter entities by case association
2. Only return entities that appear in published cases (or IN_REVIEW if feature flag enabled)
3. Implement simple caching layer with LocMemCache
4. Test that queryset correctly filters entities
5. Test cache hit/miss behavior

### Phase 2: Frontend UI Changes
1. Remove entity type filter UI components
2. Remove sort dropdown UI
3. Fetch JawafEntities from JDS `/api/entities/` endpoint (now filtered by backend)
4. Enrich entities with NES data (for entities with `nes_id`)
5. Implement client-side search filtering on JawafEntity and NES fields
6. Remove `getEntityIdsWithCases` API call (no longer needed)
7. Update EntityCard to accept both JawafEntity and optional NES Entity
8. Display "?? Cases" placeholder instead of actual counts

### Phase 3: Testing
1. Test backend filtering: Only entities in published cases are returned
2. Test caching: Verify cache hit/miss behavior
3. Test search functionality with Nepali and English text
4. Test with various entity counts
5. Verify performance with current dataset size (with and without cache)
6. Test pagination with Load More button
7. Test stale cache behavior: Publish a case and verify list updates after cache expires

### Phase 4: Documentation
1. Update API documentation with queryset filtering logic
2. Update frontend component documentation
3. Document the simplified search approach

## Performance Considerations

### Current Dataset Size
- Small number of entities (< 100)
- Client-side filtering is optimal for this scale

### Future Scaling
When entity count grows significantly (> 1000):
1. **Reintroduce Pagination**: Keep pagination but fetch larger pages
2. **Server-Side Search**: Move search back to server with database indexes
3. **Caching**: Cache entity list with short TTL
4. **Denormalize**: Store case_count on JawafEntity model, update via signals
5. **Virtual Scrolling**: Implement infinite scroll or virtual list rendering

### Query Optimization
- Use `select_related` and `prefetch_related` for entity relationships
- Add database indexes on `nes_id` and `display_name` for search
- Consider materialized view for entity case counts

### Caching Strategy
- **Cache Layer**: Django LocMemCache (in-memory cache)
- **Cache Key**: `public_entities_list`
- **TTL**: 5-10 minutes (300-600 seconds)
- **Stale Cache**: Acceptable - reduces server load
- **Benefits**: Reduces expensive queryset evaluation from O(n) cases to O(1) cache lookup
- **Philosophy**: Keep it simple - no cache invalidation, just let it expire naturally

## Migration Notes

### Database
No schema changes required.

### API Versioning
No API changes in initial implementation.

### Backend Breaking Changes
- **API Behavior Change**: `/api/entities/` now returns only entities associated with published cases
- Entities must appear in `alleged_entities` or `related_entities` of at least one published case
- **Location entities are excluded** from the entity list
- **Caching**: Entity list is cached for 10 minutes using LocMemCache
- **Stale Data**: Newly published cases may not appear in entity list for up to 10 minutes (acceptable tradeoff)

### Frontend Breaking Changes
- Remove entity type filter UI
- Remove sort dropdown UI
- Change from NES-first to JDS-first entity fetching (with NES enrichment)
- EntityCard component now requires `jawafEntity` prop and accepts optional `entity` prop
- Display "?? Cases" placeholder instead of actual case counts
- Remove client-side filtering by case association (now handled by backend)

## Testing Strategy

### Frontend Tests
```typescript
describe('Entity Search', () => {
  it('fetches entities from JDS API', () => {
    // Test API call to /api/entities/
  });

  it('filters entities by search query', () => {
    // Test client-side search filtering on display_name and nes_id
  });

  it('sorts entities alphabetically', () => {
    // Test alphabetical sorting by display_name or nes_id
  });

  it('displays all entities without type filter', () => {
    // Test that all entities are shown regardless of type
  });
});
```

## Open Questions

1. **Pagination**: ~~Should we remove pagination entirely for small datasets or keep it for future scaling?~~
   - **Decision**: Keep pagination with default page size (50), use Load More button

2. **Caching**: Should we cache the entity list on frontend?
   - **Recommendation**: Not needed initially, add if performance issues arise

3. **Entity Type**: Should we completely remove entity type data or just hide the filter?
   - **Recommendation**: Keep the data (nes_id contains type info), just remove UI filter

4. **Entity Cards**: What information should entity cards display without case counts?
   - **Recommendation**: Show entity name, nes_id, and link to entity detail page (which will show cases in future)

## Success Metrics

- **Performance**:
  - Entity list page load time < 1 second
- **UX**:
  - Search results appear within 500ms of typing (debounced)
  - Simple, clean interface without unnecessary filters
- **Simplicity**:
  - Reduced code complexity in entity search component
  - JDS-first approach with NES enrichment
  - Backend handles case association filtering

## Backend Implementation

### Filter Entities by Case Association

Update the `JawafEntityViewSet` to only return entities that appear in published cases:

```python
class JawafEntityViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public read-only API for JawafEntities.

    Only returns entities that appear in published cases.
    """

    serializer_class = JawafEntitySerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['nes_id', 'display_name']

    def get_queryset(self):
        """
        Return only entities that appear in published cases.

        An entity is included if it appears in alleged_entities or
        related_entities of at least one published case.

        Note: Location entities are excluded from this list.
        """
        from .models import CaseState

        published_cases = Case.objects.filter(state=CaseState.PUBLISHED)

        # Get entity IDs from case relationships (alleged and related only)
        entity_ids = set()
        for case in published_cases:
            # Add alleged entities
            entity_ids.update(case.alleged_entities.values_list('id', flat=True))
            # Add related entities
            entity_ids.update(case.related_entities.values_list('id', flat=True))

        # Return entities that appear in cases
        return JawafEntity.objects.filter(id__in=entity_ids).order_by('-created_at')
```

### Add Caching Layer

Implement simple caching to improve performance:

```python
from django.core.cache import cache

class JawafEntityViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public read-only API for JawafEntities with caching.
    """

    serializer_class = JawafEntitySerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['nes_id', 'display_name']

    def get_queryset(self):
        """
        Return only entities that appear in published cases.
        Uses simple caching to avoid expensive queryset evaluation.
        """
        from .models import CaseState

        # Try to get entity IDs from cache
        entity_ids = cache.get('public_entities_list')

        if entity_ids is None:
            # Cache miss - compute entity IDs
            published_cases = Case.objects.filter(state=CaseState.PUBLISHED)

            entity_ids = set()
            for case in published_cases:
                entity_ids.update(case.alleged_entities.values_list('id', flat=True))
                entity_ids.update(case.related_entities.values_list('id', flat=True))

            # Cache for 10 minutes - stale cache is acceptable
            cache.set('public_entities_list', entity_ids, timeout=600)

        return JawafEntity.objects.filter(id__in=entity_ids).order_by('-created_at')
```

**Cache Configuration**:

In `settings.py`:
```python
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'jawafdehi-cache',
    }
}
```

**Notes**:
- Uses LocMemCache (in-memory cache) - simple and sufficient
- Single cache key: `public_entities_list`
- TTL: 10 minutes (600 seconds)
- No cache invalidation - let it expire naturally (KISS principle)
- Stale cache for 5-10 minutes is acceptable to reduce server load
```

**Performance Considerations**:
- This approach iterates through all published cases to collect entity IDs
- For large datasets, consider using `prefetch_related` or database annotations
- Alternative: Use Q objects with `distinct()`:
  ```python
  return JawafEntity.objects.filter(
      Q(alleged_in_cases__in=published_cases) |
      Q(related_in_cases__in=published_cases)
  ).distinct().order_by('-created_at')
  ```
  (Note: This requires reverse relationships to be properly defined on the JawafEntity model)

**Caching Strategy**:
- The entity list changes infrequently (only when cases are published/updated)
- Implement simple caching to avoid expensive queryset evaluation on every request
- Use LocMemCache with single cache key: `public_entities_list`
- TTL: 10 minutes (600 seconds)
- No cache invalidation - let it expire naturally (KISS principle)
- Stale cache is acceptable to reduce server load

## Implementation Notes

### NES Data Fetching

The current implementation fetches JawafEntity records from JDS API with pagination, then enriches them with NES data:

1. Fetch entities from `/api/entities/?page=1` (JDS) - default 50 per page
2. For each entity with `nes_id`, fetch NES data via `getEntityById(nes_id)`
3. Combine JawafEntity and NES Entity data for display
4. User clicks "Load More" to fetch next page
5. New entities are appended to existing list

**Known Limitation**: NES API calls are made one at a time (N+1 query pattern).

**TODO**: Implement batch NES API endpoint to fetch multiple entities at once:
```typescript
// Proposed batch endpoint
GET /entities?entity-id=id1,id2,id3
```

This would reduce N API calls to 1 API call, significantly improving performance.

### Entity Display Priority

Entity cards display information in this priority:
1. **NES Entity Data** (if available): Full entity profile with names, pictures, attributes
2. **JawafEntity display_name** (if NES data unavailable): Custom display name
3. **JawafEntity nes_id** (fallback): Show the NES ID as identifier

### Case Count Display

Entity cards show "?? Cases" as a placeholder. This will be implemented in future work when case count API is added.

## Future Improvements

### 1. Batch NES API Endpoint

**Priority**: High
**Rationale**: Current implementation makes N API calls to fetch NES data for N entities, causing performance issues.

**Backend Changes (NES)**:
- Add support for comma-separated entity IDs in `/entities` endpoint
- Return multiple entities in single response

**Frontend Changes**:
- Collect all nes_ids from JawafEntity list
- Make single batch API call to NES
- Map NES entities back to JawafEntities

**Expected Performance Improvement**:
- Reduce API calls from N to 1
- Reduce page load time from ~N*100ms to ~100ms

### 2. Add Entity Detail Endpoint with Case Lists

**Rationale**: Single entity page needs to show all cases the entity is involved in, separated by relationship type.

**Backend Changes**:

#### Create Detail Serializer
```python
class JawafEntityDetailSerializer(serializers.ModelSerializer):
    """
    Detailed serializer for single entity view with full case information.
    """
    alleged_cases = serializers.SerializerMethodField()
    related_cases = serializers.SerializerMethodField()

    class Meta:
        model = JawafEntity
        fields = ['id', 'nes_id', 'display_name', 'alleged_cases', 'related_cases']

    def get_alleged_cases(self, obj):
        """Get all cases where this entity is alleged."""
        # Return full case data using CaseSerializer
        pass

    def get_related_cases(self, obj):
        """Get all cases where this entity is related or a location."""
        # Return full case data using CaseSerializer
        pass
```

#### Update ViewSet
```python
def get_serializer_class(self):
    """Use JawafEntityDetailSerializer for retrieve action."""
    if self.action == 'retrieve':
        return JawafEntityDetailSerializer
    return JawafEntitySerializer
```

**Frontend Changes**:
- Create entity detail page at `/entities/:id`
- Display alleged cases and related cases in separate sections
- No pagination needed (all cases shown)

**When to Implement**:
- After entity search simplification is complete
- When entity detail page design is ready

### 2. Add Case Counts to Entity List

**Rationale**: Users need to see how many cases each entity is involved in for sorting and context.

**Backend Changes**:

#### Update List Serializer
```python
class JawafEntitySerializer(serializers.ModelSerializer):
    """
    Serializer for JawafEntity model with case counts.
    """
    alleged_case_count = serializers.SerializerMethodField()
    related_case_count = serializers.SerializerMethodField()

    class Meta:
        model = JawafEntity
        fields = ['id', 'nes_id', 'display_name', 'alleged_case_count', 'related_case_count']

    def get_alleged_case_count(self, obj):
        """Count cases where entity is alleged."""
        # Implementation similar to detail serializer
        pass

    def get_related_case_count(self, obj):
        """Count cases where entity is related or a location."""
        # Implementation similar to detail serializer
        pass
```

**Performance Considerations**:
- **N+1 Query Problem**: Serializer methods will cause N queries for N entities
- **Solutions**:
  - Use `prefetch_related` with custom Prefetch objects
  - Denormalize counts to JawafEntity model (update via signals)
  - Use database annotations in viewset queryset
  - Cache counts with short TTL

**Frontend Changes**:
- Update `JawafEntity` type to include `alleged_case_count` and `related_case_count`
- Display counts in EntityCard component
- Enable sorting by alleged case count
- Remove mock random case counts

**When to Implement**:
- After initial entity detail endpoint is working
- When sorting by case count becomes a user requirement
- When performance testing shows acceptable query times

## References

- [API Documentation](./API_DOCUMENTATION.md)
- [Entity Migration Summary](./ENTITY_MIGRATION_SUMMARY.md)
