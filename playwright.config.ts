import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 4 : undefined,
  reporter: [
    ['html', { open: 'never' }],
    ['list'],
    ...(process.env.CI ? [['github']] : []),
  ],
  timeout: 30_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:8000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
      testIgnore: /swagger/, // Swagger UI load test Chrome only
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
      testIgnore: /swagger/,
    },
  ],
  webServer: process.env.CI
    ? []
    : [
        {
          command: 'poetry run python manage.py runserver 0.0.0.0:8000',
          port: 8000,
          timeout: 30_000,
          reuseExistingServer: true,
          env: {
            DJANGO_SETTINGS_MODULE: 'config.settings',
            DATABASE_URL: 'sqlite:///db.sqlite3',
            SECRET_KEY: 'playwright-test-key-not-for-production',
            DEBUG: 'True',
            ALLOWED_HOSTS: 'localhost,127.0.0.1',
          },
        },
      ],
});
