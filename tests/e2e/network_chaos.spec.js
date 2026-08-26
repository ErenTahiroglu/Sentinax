import { test, expect } from '@playwright/test';

test.describe('Network Chaos & Resilience Tests', () => {
    test('should retry on 503 Service Unavailable and eventually succeed', async ({ page }) => {
        let attempt = 0;

        await page.route('**/api/analyze', async (route) => {
            attempt++;
            if (attempt === 1) {
                await route.fulfill({
                    status: 503,
                    contentType: 'application/json',
                    body: JSON.stringify({ detail: 'Service Unavailable' })
                });
            } else {
                await route.fulfill({
                    status: 200,
                    contentType: 'text/event-stream',
                    body: 'data: {"ticker": "AAPL", "price": 150}\n\n'
                });
            }
        });

        await page.goto('http://localhost:3000');
        await page.click('#guest-btn');
        await page.fill('#ticker-input', 'AAPL');
        await page.click('#analyze-btn');

        // Note: Next.js frontend uses "Analyzing..." text in the button during loading
        await expect(page.locator('#loader')).toBeVisible();
        await expect(page.locator('.result-card')).toBeVisible({ timeout: 15000 });
        await expect(page.locator('body')).toContainText('AAPL');
    });
});
