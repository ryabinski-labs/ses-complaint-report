// SPDX-License-Identifier: MIT | Copyright (c) 2026 Yoni Ryabinski
import { defineConfig } from '@playwright/test';

// Boots the app itself in demo mode (synthetic data, no AWS calls) and reuses
// a running instance on the same port during local development.
export default defineConfig({
  testDir: '.',
  reporter: 'list',
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:8799',
  },
  webServer: {
    command: 'uv run sesreport',
    cwd: '../..',
    env: {
      SESREPORT_DEMO: '1',
      SESREPORT_HOST: '127.0.0.1',
      SESREPORT_PORT: '8799',
      SESREPORT_REFRESH_SECONDS: '3600',
    },
    url: 'http://127.0.0.1:8799/api/status',
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
