const { test, expect } = require('@playwright/test');

test.describe('Network Chaos & Resilience Tests', () => {
    test('should retry on 503 Service Unavailable and eventually succeed', async ({ page }) => {
        let attempt = 0;

        // Intercept /api/analyze
        await page.route('**/api/analyze', async (route) => {
            attempt++;
            if (attempt === 1) {
                // Fail the first attempt
                await route.fulfill({
                    status: 503,
                    contentType: 'application/json',
                    body: JSON.stringify({ detail: 'Service Unavailable' })
                });
            } else {
                // Succeed on subsequent attempts (mocked SSE)
                await route.fulfill({
                    status: 200,
                    contentType: 'text/event-stream',
                    body: 'data: {"ticker": "AAPL", "price": 150}\n\n'
                });
            }
        });

        await page.goto('/');
        await page.click('#guest-btn');
        
        // Trigger analysis
        await page.fill('#ticker-input', 'AAPL');
        await page.click('#analyze-btn');

        // Verify it eventually succeeds
        await expect(page.locator('.result-card')).toBeVisible({ timeout: 15000 });
        await expect(page.locator('body')).toContainText('AAPL');
    });

    test('should retry on network disconnect (abort)', async ({ page }) => {
        let attempt = 0;

        await page.route('**/api/analyze', async (route) => {
            attempt++;
            if (attempt === 1) {
                await route.abort('failed');
            } else {
                await route.fulfill({
                    status: 200,
                    contentType: 'application/json',
                    body: JSON.stringify([{ ticker: "MSFT", analysis: "MSFT is strong.", score: 10 }])
                });
            }
        });

        await page.goto('/');
        await page.click('#guest-btn');
        await page.fill('#ticker-input', 'MSFT');
        await page.click('#analyze-btn');

        await expect(page.locator('.result-card')).toBeVisible({ timeout: 15000 });
        await expect(page.locator('body')).toContainText('MSFT');
    });
});
