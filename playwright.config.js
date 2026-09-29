import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60 * 1000,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: 'http://127.0.0.1:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure'
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    // Adding mobile for responsive regression
    {
      name: 'Mobile Chrome',
      use: { ...devices['Pixel 5'] },
    },
  ],
  // Static frontend server (frontend/ has no npm manifest)
  webServer: {
    // Stock `http.server` has a listen backlog of 5 and resets connections when
    // parallel workers load the ES-module graph; raise it.
    command: `python3 -c "import functools, http.server as h; h.ThreadingHTTPServer.request_queue_size = 128; h.test(HandlerClass=functools.partial(h.SimpleHTTPRequestHandler, directory='frontend'), ServerClass=h.ThreadingHTTPServer, port=3000, bind='127.0.0.1')"`,
    url: 'http://127.0.0.1:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 30 * 1000,
  },
});
