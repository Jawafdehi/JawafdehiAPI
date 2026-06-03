# Design: Entity Cases Relationship in Entity Endpoints

## Status: ✅ IMPLEMENTED

## Overview

Enhance the `/entities/{id}` GET endpoint and `/entities/` LIST endpoint to include lists of case IDs that reference the entity, categorized by relationship type.

## Problem Statement

Currently, the entity detail endpoint returns basic entity information from NES but doesn't expose which cases reference this entity. Users need to see:
- Cases where this entity is alleged (accused/subject of the case)
- Cases where this entity is related (mentioned as related entity or location)

## Proposed Solution

Add two new fields to the entity detail response:
- `alleged_cases`: List of case IDs where this entity appears in the `alleged_entities` field
- `related_cases`: List of case IDs where this entity appears in `related_entities` or `location_entities` fields

### API Response Schema

```json
{
  "id": "string",
  "name": "string",
  "type": "string",
  "metadata": {},
  "alleged_cases": [1, 5, 12],
  "related_cases": [3, 8, 15, 22]
}
```

## Implementation Details

### Database Queries

The implementation will require querying the `Case` model with the following logic:

**Alleged Cases:**
```python
alleged_cases = Case.objects.filter(
    alleged_entities__contains=[entity_id],
    status__in=['published', 'in_review']  # Only visible cases
).values_list('id', flat=True)
```

**Related Cases:**
```python
related_cases = Case.objects.filter(
    Q(related_entities__contains=[entity_id]) |
    Q(location_entities__contains=[entity_id]),
    status__in=['published', 'in_review']
).exclude(
    id__in=alleged_cases  # Avoid duplicates
).values_list('id', flat=True)
```

### Endpoint Location

- **File**: `services/jawafdehi-api/cases/api_views.py`
- **View**: `EntityDetailView` or similar entity detail view
- **Method**: `GET /api/entities/{id}/`

### Permissions

- Respect existing case visibility rules
- Only include cases with status `published`
- Apply any user-specific permissions via django-rules

### Performance Considerations

1. **Indexing**: Ensure GIN indexes exist on JSONField arrays:
   ```sql
   CREATE INDEX idx_case_alleged_entities ON cases_case USING GIN (alleged_entities);
   CREATE INDEX idx_case_related_entities ON cases_case USING GIN (related_entities);
   CREATE INDEX idx_case_location_entities ON cases_case USING GIN (location_entities);
   ```

2. **Caching**: Consider caching entity-case relationships if this endpoint becomes high-traffic

3. **Pagination**: For entities with many cases, consider:
   - Limiting results (e.g., most recent 50 cases)
   - Adding pagination parameters
   - Returning counts instead of full lists

## Data Flow

```
Client Request
    ↓
GET /api/entities/{id}/
    ↓
EntityDetailView
    ↓
1. Fetch entity from NES API
    ↓
2. Query alleged_cases from Case model
    ↓
3. Query related_cases from Case model
    ↓
4. Merge data and return response
```

## Testing Strategy

### Unit Tests
- Test alleged_cases query returns correct case IDs
- Test related_cases query returns correct case IDs
- Test no duplicates between alleged and related
- Test visibility filtering (published/in_review)
- Test empty results for entities with no cases

### Integration Tests
- Test full endpoint response structure
- Test with various entity types (person, organization, location)
- Test permission-based filtering

### Test Data
Use authentic Nepali entities and cases:
- Entity: "राम बहादुर थापा" (politician)
- Entity: "काठमाडौं महानगरपालिका" (organization)
- Entity: "काठमाडौं" (location)

## Migration Requirements

No database schema changes required. Ensure indexes exist on:
- `cases_case.alleged_entities`
- `cases_case.related_entities`
- `cases_case.location_entities`

## Frontend Impact

The Jawafdehi frontend will need updates to:
- Update TypeScript types for entity detail response
- Display alleged cases section on entity profile
- Display related cases section on entity profile
- Handle empty states when no cases exist

## Implementation Summary

### What Was Implemented

1. **Modified `JawafEntitySerializer`** (`cases/serializers.py`):
   - Added `alleged_cases` SerializerMethodField
   - Added `related_cases` SerializerMethodField
   - Implemented `get_alleged_cases()` method with proper filtering
   - Implemented `get_related_cases()` method with deduplication logic
   - Added OpenAPI schema annotations

2. **Test Coverage** (`tests/api/test_entity_cases_relationship.py`):
   - 22 comprehensive tests covering all scenarios
   - Tests for both detail and list endpoints
   - Tests for edge cases (entity in multiple roles, draft cases, etc.)

3. **Behavior**:
   - Works automatically for both `/api/entities/` (list) and `/api/entities/{id}/` (detail)
    - Only includes PUBLISHED cases
   - Excludes DRAFT and CLOSED cases
   - Prevents duplicates (if entity is both alleged and related, only shows in alleged_cases)
   - Returns empty lists for entities with no cases

### Files Changed

- `services/jawafdehi-api/cases/serializers.py` - Added case relationship fields
- `services/jawafdehi-api/tests/api/test_entity_cases_relationship.py` - New test file with 22 tests
- `services/jawafdehi-api/docs/design-entity-cases-relationship.md` - This design doc

### No Database Changes Required

The implementation uses existing many-to-many relationships:
- `alleged_entities` (via `cases_as_alleged` reverse relation)
- `related_entities` (via `cases_as_related` reverse relation)
- `locations` (via `cases_as_location` reverse relation)

Existing indexes on these relationships are sufficient for performance.

## Rollout Plan

1. ✅ Add database indexes (already exist)
2. ✅ Implement backend endpoint changes
3. ✅ Write and run tests (22 tests, all passing)
4. ✅ Update API documentation (drf-spectacular schema annotations added)
5. ⏳ Deploy backend changes
6. ⏳ Update frontend to consume new fields
7. ⏳ Deploy frontend changes

## Open Questions

1. Should we return full case objects or just IDs?
   - **Recommendation**: Start with IDs to keep response lightweight, add `/entities/{id}/cases/` endpoint for full details if needed

2. Should we paginate case lists?
   - **Recommendation**: Start without pagination, add if entities commonly have >50 cases

3. Should we include case counts in the list endpoint `/entities/`?
   - **Recommendation**: Yes, add `alleged_cases_count` and `related_cases_count` to list view for overview

4. Should draft cases be included for authenticated users with permissions?
   - **Recommendation**: No, keep it simple. Only published (and optionally in_review) cases

## Success Metrics

- Endpoint response time remains <500ms
- No N+1 query issues
- Frontend successfully displays case relationships
- Users can navigate from entity to related cases

## References

- Django QuerySet API: https://docs.djangoproject.com/en/5.2/ref/models/querysets/
- PostgreSQL GIN indexes: https://www.postgresql.org/docs/current/gin-intro.html
- DRF Serializers: https://www.django-rest-framework.org/api-guide/serializers/
