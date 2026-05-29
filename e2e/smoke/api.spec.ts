import { test, expect } from '@playwright/test';

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
      const response = await request.get(path);
      expect(response.status()).toBe(200);
    });
  }
});

test.describe('Case endpoints', () => {
  test('case list returns paginated response', async ({ request }) => {
    const response = await request.get('/api/cases/');
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
    const response = await request.get('/api/cases/?case_type=CORRUPTION');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(Array.isArray(body.results)).toBe(true);
    if (!Array.isArray(body.results) || body.results.length === 0) {
      test.skip(true, 'No cases available for filter test');
      return;
    }
    for (const case_ of body.results) {
      expect(case_.case_type).toBe('CORRUPTION');
    }
  });

  test('case list supports search', async ({ request }) => {
    const response = await request.get('/api/cases/?search=corruption');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('results');
  });

  test('case detail returns full case data', async ({ request }) => {
    const listResp = await request.get('/api/cases/');
    const listBody = await listResp.json();

    if (!listBody || !Array.isArray(listBody.results) || listBody.results.length === 0) {
      test.skip(true, 'No published cases available for detail test');
      return;
    }

    const slug = listBody.results[0].slug;
    const response = await request.get(`/api/cases/${slug}/`);
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
    const response = await request.get('/api/sources/');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('count');
    expect(body).toHaveProperty('results');
  });
});

test.describe('Entity endpoints', () => {
  test('entities list returns paginated response', async ({ request }) => {
    const response = await request.get('/api/entities/');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('count');
    expect(body).toHaveProperty('results');
  });
});

test.describe('Schema and docs', () => {
  test('OpenAPI schema is valid JSON', async ({ request }) => {
    const response = await request.get('/api/schema/?format=json');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('openapi');
    expect(body).toHaveProperty('info');
    expect(body.info).toHaveProperty('title');
  });

  test('Swagger UI loads', async ({ page, browserName }) => {
    test.skip(browserName !== 'chromium', 'Swagger UI load test Chrome only');
    const response = await page.goto('/api/swagger/');
    expect(response?.status()).toBe(200);
    await expect(page.locator('.swagger-ui')).toBeVisible({ timeout: 10000 });
  });
});

test.describe('Content-Type checks', () => {
  test('API endpoints return JSON', async ({ request }) => {
    const response = await request.get('/api/cases/');
    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toContain('application/json');
  });

  test('schema returns JSON', async ({ request }) => {
    const response = await request.get('/api/schema/?format=json');
    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toContain('application/json');
  });
});

test.describe('404 handling', () => {
  test('non-existent case returns 404', async ({ request }) => {
    const response = await request.get('/api/cases/non-existent-slug-99999/');
    expect(response.status()).toBe(404);
  });

  test('non-existent route returns 404', async ({ request }) => {
    const response = await request.get('/api/non-existent-endpoint/');
    expect(response.status()).toBe(404);
  });
});

test.describe('Statistics endpoint', () => {
  test('statistics returns expected keys', async ({ request }) => {
    const response = await request.get('/api/statistics/');
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty('published_cases');
    expect(body).toHaveProperty('cases_under_investigation');
    expect(body).toHaveProperty('cases_closed');
    expect(body).toHaveProperty('entities_tracked');
    expect(typeof body.published_cases).toBe('number');
  });
});
