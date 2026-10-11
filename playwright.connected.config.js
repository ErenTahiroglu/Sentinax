// Phase 25D1E4: CONNECTED offline tests. Chromium + Mobile Chrome drive the real public Buffett page and the built owner artifact; the owner page's same-origin
// `/private/*` calls and Buffett's `/buffett/*` calls reach the REAL FastAPI application (real middleware, real JWT verifier, real require_private_owner) started by the
// test-only harness backend/tests/e2e_harness/real_api_server.py. Supabase Auth stays a browser-level mock whose access tokens are SIGNED by that harness.
// Needs Python with backend/requirements.txt (run it in the backend job or with PATH=.venv/bin:$PATH). Never contacts Supabase, Oracle or Cloudflare.
import crypto from 'node:crypto';
import { defineConfig, devices } from '@playwright/test';

process.env.SENTINAX_HARNESS_SECRET ||= crypto.randomBytes(16).toString('hex'); // per run; workers inherit it
const PROJECT_URL = 'https://zlggrmsolklhfgijcjnz.supabase.co'; // the public project origin the owner page is pinned to (used only to build the expected issuer)
const API = 'http://127.0.0.1:8765';
const PY = process.env.SENTINAX_HARNESS_PYTHON || 'python3';

export default defineConfig({
  testDir: './tests/connected',
  timeout: 60 * 1000,
  fullyParallel: false,
  workers: 1, // the harness log and owner configuration are shared state
  forbidOnly: !!process.env.CI,
  retries: 0, // a flaky security test must fail loudly, not be retried into a pass
  reporter: process.env.CI ? [['list'], ['json', { outputFile: 'test-results/connected.json' }]] : 'list',
  use: { trace: 'on-first-retry', screenshot: 'only-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'Mobile Chrome', use: { ...devices['Pixel 5'] } },
  ],
  webServer: [
    {
      command: `${PY} -m backend.tests.e2e_harness.real_api_server 8765`,
      url: `${API}/__harness/ready`,
      env: { SENTINAX_HARNESS_SECRET: process.env.SENTINAX_HARNESS_SECRET, SENTINAX_HARNESS_PROJECT_URL: PROJECT_URL },
      reuseExistingServer: false,
      timeout: 120 * 1000,
    },
    {
      command: 'node scripts/owner_deploy_serve.mjs cloudflare',
      url: 'http://127.0.0.1:3005/owner/',
      env: { OWNER_DEPLOY_PORT: '3005', SENTINAX_TEST_API_ORIGIN: API },
      reuseExistingServer: false,
      timeout: 60 * 1000,
    },
    {
      command: 'node scripts/buffett_harness_serve.mjs',
      url: 'http://127.0.0.1:3006/',
      env: { BUFFETT_HARNESS_PORT: '3006', SENTINAX_TEST_API_ORIGIN: API },
      reuseExistingServer: false,
      timeout: 60 * 1000,
    },
  ],
});
