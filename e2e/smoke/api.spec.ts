import { test, expect } from '@playwright/test';
import casesFixture from '../fixtures/cases.json';
import caseDetailFixture from '../fixtures/case-detail.json';
import statisticsFixture from '../fixtures/statistics.json';
import entitiesFixture from '../fixtures/entities.json';

const API_BASE = process.env.PLAYWRIGHT_API_BASE || 'http://localhost:8000';

const PUBLIC_GET_ENDPOINTS = [
  { path: '/api/cases/', label: 'cases list' },
  { path: '/api/statistics/', label: 'statistics' },
  { path: '/api/entities/', label: 'entities' },
  { path: '/api/sources/', label: 'document sources' },
  { path: '/api/schema/', label: 'OpenAPI schema' },
  { path: '/', label: 'landing page' },
];

test.describe('Public API smoke', () => {
  for (const { path, label } of PUBLIC_GET_ENDPOINTS) {
    test(`${label} returns 200`, async ({ request }) => {
      const response = await request.get(`${API_BASE}${path}`);
      expect(response.status()).toBe(200);
    });
  }
});

test.describe('Case endpoints', () => {
  test('case list returns paginated response', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/cases/`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('count');
    expect(body).toHaveProperty('results');
    expect(Array.isArray(body.results)).toBe(true);

    if (body.results.length > 0) {
      const case_ = body.results[0];
      expect(case_).toHaveProperty('title');
      expect(case_).toHaveProperty('slug');
      expect(case_).toHaveProperty('state', 'PUBLISHED');
    }
  });

  test('case list supports filtering by case_type', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/cases/?case_type=CORRUPTION`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    for (const case_ of body.results) {
      expect(case_.case_type).toBe('CORRUPTION');
    }
  });

  test('case list supports search', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/cases/?search=corruption`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('results');
  });

  test('case detail returns full case data', async ({ request }) => {
    // First get a case slug from the list
    const listResp = await request.get(`${API_BASE}/api/cases/`);
    const listBody = await listResp.json();

    if (listBody.results.length === 0) {
      test.skip(true, 'No published cases available for detail test');
      return;
    }

    const slug = listBody.results[0].slug;
    const response = await request.get(`${API_BASE}/api/cases/${slug}/`);
    expect(response.status()).toBe(200);

    const case_ = await response.json();
    expect(case_).toHaveProperty('title');
    expect(case_).toHaveProperty('description');
    expect(case_).toHaveProperty('key_allegations');
    expect(Array.isArray(case_.key_allegations)).toBe(true);
  });
});

test.describe('Document sources', () => {
  test('sources list returns paginated response', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/sources/`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('count');
    expect(body).toHaveProperty('results');
  });
});

test.describe('Entity endpoints', () => {
  test('entities list returns paginated response', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/entities/`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('count');
    expect(body).toHaveProperty('results');
  });
});

test.describe('Schema and docs', () => {
  test('OpenAPI schema is valid JSON', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/schema/`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('openapi');
    expect(body).toHaveProperty('info');
    expect(body.info).toHaveProperty('title');
  });

  test('Swagger UI loads', async ({ page }) => {
    const response = await page.goto(`${API_BASE}/api/swagger/`);
    expect(response?.status()).toBe(200);
    await expect(page.locator('.swagger-ui')).toBeVisible({ timeout: 10000 });
  });
});

test.describe('Content-Type checks', () => {
  test('API endpoints return JSON', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/cases/`);
    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toContain('application/json');
  });

  test('schema returns JSON', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/schema/`);
    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toContain('application/json');
  });
});

test.describe('404 handling', () => {
  test('non-existent case returns 404', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/cases/non-existent-slug-99999/`);
    expect(response.status()).toBe(404);
  });

  test('non-existent route returns 404', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/non-existent-endpoint/`);
    expect(response.status()).toBe(404);
  });
});

test.describe('Statistics endpoint', () => {
  test('statistics returns expected keys', async ({ request }) => {
    const response = await request.get(`${API_BASE}/api/statistics/`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('total_cases');
    expect(body).toHaveProperty('published_cases');
    expect(typeof body.total_cases).toBe('number');
  });
});
