# Case Statistics API Contract

## Overview

This document defines the API contract for the case statistics endpoint that provides aggregate data for display on the Jawafdehi frontend homepage.

## Endpoint

```
GET /api/statistics/
```

## Authentication

- **Required**: No (public endpoint)
- **Rate Limiting**: Not currently configured

## Request

### Query Parameters

None required. This endpoint returns current statistics for all cases.

### Example Request

```bash
curl -X GET "https://portal.jawafdehi.org/api/statistics/" \
  -H "Accept: application/json"
```

## Response

### Success Response (200 OK)

```json
{
  "published_cases": 127,
  "entities_tracked": 89,
  "cases_under_investigation": 43,
  "cases_closed": 31,
  "last_updated": "2024-12-04T10:30:00Z"
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `published_cases` | integer | Number of cases with state "PUBLISHED" |
| `entities_tracked` | integer | Total number of entities in the system |
| `cases_under_investigation` | integer | Number of cases with state "DRAFT" or "IN_REVIEW" |
| `cases_closed` | integer | Number of cases with state "CLOSED" |
| `last_updated` | string (ISO 8601) | Timestamp when statistics were last calculated |

### Error Responses

#### 500 Internal Server Error

```json
{
  "error": "Unable to calculate statistics",
  "detail": "Database connection error"
}
```

#### 503 Service Unavailable

```json
{
  "error": "Statistics service temporarily unavailable",
  "detail": "Cache refresh in progress"
}
```

## Implementation Notes

### Calculation Logic

#### Published Cases
- Count all cases with `state = 'PUBLISHED'`
- Query: `Case.objects.filter(state=CaseState.PUBLISHED).count()`

#### Cases Under Investigation
- Count cases with `state = 'DRAFT'` OR `state = 'IN_REVIEW'`
- Query: `Case.objects.filter(state__in=[CaseState.DRAFT, CaseState.IN_REVIEW]).count()`

#### Cases Closed
- Count cases with `state = 'CLOSED'`
- Query: `Case.objects.filter(state=CaseState.CLOSED).count()`

#### Entities Tracked
- Count total number of JawafEntity records in the system
- Simple count query:

```python
entities_tracked = JawafEntity.objects.count()
```

### Caching Implementation

Statistics are cached for 5 minutes using Django's LocMemCache.

```python
# settings.py
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'jawafdehi-cache',
        'TIMEOUT': 300,
        'OPTIONS': {'MAX_ENTRIES': 1000}
    }
}

# api_views.py
class StatisticsView(APIView):
    def get(self, request):
        cache_key = 'stats-cache'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        stats = {
            'published_cases': Case.objects.filter(state=CaseState.PUBLISHED).count(),
            'cases_under_investigation': Case.objects.filter(
                state__in=[CaseState.DRAFT, CaseState.IN_REVIEW]
            ).count(),
            'cases_closed': Case.objects.filter(state=CaseState.CLOSED).count(),
            'entities_tracked': JawafEntity.objects.count(),
            'last_updated': timezone.now().isoformat(),
        }

        cache.set(cache_key, stats, timeout=300)
        return Response(stats)
```

**Alternative Backends for Production:**
- **Redis**: For multi-server deployments with shared cache
- **Memcached**: For distributed caching
- **Database**: No additional infrastructure, uses PostgreSQL

### Performance Considerations

1. **Database Indexing**: Ensure indexes exist on `state` field (already defined in model)
2. **Cache Hit Rate**: Monitor cache effectiveness with 5-minute TTL
3. **Cache Warming**: Consider warming cache on application startup
4. **Query Performance**: Simple count queries are fast; caching prevents repeated execution

### Security Considerations

1. **No Sensitive Data**: Only aggregate counts are exposed; no case details
2. **Rate Limiting**: Not currently configured but recommended for production
3. **CORS**: Already enabled for all origins (configured in settings)
4. **Cache Poisoning**: Uses secure cache keys and validates data before caching

## Frontend Integration

### TypeScript Interface

```typescript
interface CaseStatistics {
  published_cases: number;
  entities_tracked: number;
  cases_under_investigation: number;
  cases_closed: number;
  last_updated: string;
}
```

### Usage Example

```typescript
import { useQuery } from '@tanstack/react-query';

const useStatistics = () => {
  return useQuery<CaseStatistics>({
    queryKey: ['statistics'],
    queryFn: async () => {
      const response = await fetch('https://portal.jawafdehi.org/api/statistics/');
      if (!response.ok) {
        throw new Error('Failed to fetch statistics');
      }
      return response.json();
    },
    staleTime: 5 * 60 * 1000, // 5 minutes
    refetchInterval: 5 * 60 * 1000, // Refetch every 5 minutes to match cache
  });
};
```

### Display Mapping

Map API response to frontend display:

```typescript
const { data: stats } = useStatistics();

// Map to StatCard components
<StatCard
  title={t("home.stats.totalCases")}
  value={stats?.published_cases.toString() || "0"}
  icon={FileText}
  description={t("home.stats.totalCasesDesc")}
/>

<StatCard
  title={t("home.stats.entitiesTracked")}
  value={stats?.entities_tracked.toString() || "0"}
  icon={Users}
  description={t("home.stats.entitiesTrackedDesc")}
/>

<StatCard
  title={t("home.stats.underInvestigation")}
  value={stats?.cases_under_investigation.toString() || "0"}
  icon={Eye}
  description={t("home.stats.underInvestigationDesc")}
/>

<StatCard
  title={t("home.stats.resolved")}
  value={stats?.cases_closed.toString() || "0"}
  icon={TrendingUp}
  description={t("home.stats.resolvedDesc")}
/>
```

## Testing

### Test Cases

1. **Successful retrieval**: Verify all fields are present and have correct types
2. **Empty database**: Verify all counts return 0 when no cases exist
3. **Cache behavior**: Verify statistics are cached and not recalculated on every request
4. **State filtering**: Verify correct counts for each state (PUBLISHED, DRAFT, IN_REVIEW, CLOSED)
5. **Entity counting**: Verify entities are counted correctly across all relationships
6. **Performance**: Verify response time is under 200ms (with caching)

### Sample Test Data

```python
# pytest example
import pytest
from django.core.cache import cache
from cases.models import Case, CaseState, JawafEntity

@pytest.mark.django_db
def test_statistics_endpoint(api_client):
    response = api_client.get('/api/statistics/')
    assert response.status_code == 200

    data = response.json()
    assert 'published_cases' in data
    assert 'entities_tracked' in data
    assert 'cases_under_investigation' in data
    assert 'cases_closed' in data
    assert 'last_updated' in data

    # Verify types
    assert isinstance(data['published_cases'], int)
    assert isinstance(data['entities_tracked'], int)
    assert isinstance(data['cases_under_investigation'], int)
    assert isinstance(data['cases_closed'], int)

@pytest.mark.django_db
def test_statistics_counts_by_state(api_client, case_factory, entity_factory):
    # Create test data
    entity = entity_factory()

    # Create cases in different states
    published_case = case_factory(state=CaseState.PUBLISHED)
    published_case.alleged_entities.add(entity)

    draft_case = case_factory(state=CaseState.DRAFT)
    review_case = case_factory(state=CaseState.IN_REVIEW)
    closed_case = case_factory(state=CaseState.CLOSED)

    response = api_client.get('/api/v1/statistics/')
    data = response.json()

    assert data['published_cases'] == 1
    assert data['cases_under_investigation'] == 2  # DRAFT + IN_REVIEW
    assert data['cases_closed'] == 1
    assert data['entities_tracked'] == 1

@pytest.mark.django_db
def test_statistics_cache_behavior(api_client, case_factory):
    # Clear cache
    cache.clear()

    # First request - should calculate
    response1 = api_client.get('/api/statistics/')
    data1 = response1.json()

    # Create new case
    case_factory(state=CaseState.PUBLISHED)

    # Second request within 5 minutes - should return cached data
    response2 = api_client.get('/api/statistics/')
    data2 = response2.json()

    # Should be same as cached
    assert data1['published_cases'] == data2['published_cases']

    # Clear cache and request again - should reflect new case
    cache.clear()
    response3 = api_client.get('/api/statistics/')
    data3 = response3.json()

    assert data3['published_cases'] == data1['published_cases'] + 1

@pytest.mark.django_db
def test_statistics_empty_database(api_client):
    response = api_client.get('/api/statistics/')
    data = response.json()

    assert data['published_cases'] == 0
    assert data['entities_tracked'] == 0
    assert data['cases_under_investigation'] == 0
    assert data['cases_closed'] == 0
```

## Changelog

### 1.0.0 (Initial Release)
- Initial statistics endpoint with case counts by state
- Published cases count (state=PUBLISHED)
- Cases under investigation count (state=DRAFT or IN_REVIEW)
- Cases closed count (state=CLOSED)
- Total entity count in system
- 5-minute cache implementation using LocMemCache
