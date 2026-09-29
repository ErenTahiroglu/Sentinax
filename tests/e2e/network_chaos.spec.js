import { test, expect } from '@playwright/test';

const sse = (item) => `data: ${JSON.stringify(item)}\n\n`;

async function gotoAppReady(page) {
    const ready = page.waitForEvent('console', {
        predicate: (m) => m.text().includes('App Initialized Successfully'),
        timeout: 15000
    });
    await Promise.all([ready, page.goto('/')]);
}

async function enterGuestAndAnalyze(page, ticker) {
    await gotoAppReady(page);
    await page.getByRole('button', { name: 'Misafir Olarak Dene', exact: true }).click();
    await expect(page.locator('#ticker-input')).toBeVisible();
    await page.locator('#ticker-input').fill(ticker);
    await page.locator('#analyze-btn').click();
}

test.describe('Network Chaos & Resilience Tests', () => {
    test('should retry on 503 Service Unavailable and eventually succeed', async ({ page }) => {
        let attempts = 0;

        await page.route('**/api/analyze', async (route) => {
            attempts++;
            if (attempts === 1) {
                await route.fulfill({
                    status: 503,
                    contentType: 'application/json',
                    body: JSON.stringify({ detail: 'Service Unavailable' })
                });
            } else {
                await route.fulfill({
                    status: 200,
                    contentType: 'text/event-stream',
                    body: sse({ ticker: 'AAPL', price: 150 })
                });
            }
        });

        await enterGuestAndAnalyze(page, 'AAPL');

        await expect(page.locator('#results-grid .result-card:not(.skeleton-card)').first()).toBeVisible({ timeout: 15000 });
        await expect(page.locator('#results-grid .result-card:not(.skeleton-card)').first()).toContainText('AAPL');
        expect(attempts).toBeGreaterThanOrEqual(2);
    });

    test('should retry on network disconnect (abort) and eventually succeed', async ({ page }) => {
        let attempts = 0;

        await page.route('**/api/analyze', async (route) => {
            attempts++;
            if (attempts === 1) {
                await route.abort('failed');
            } else {
                await route.fulfill({
                    status: 200,
                    contentType: 'text/event-stream',
                    body: sse({ ticker: 'MSFT', price: 300 })
                });
            }
        });

        await enterGuestAndAnalyze(page, 'MSFT');

        await expect(page.locator('#results-grid .result-card:not(.skeleton-card)').first()).toBeVisible({ timeout: 15000 });
        await expect(page.locator('#results-grid .result-card:not(.skeleton-card)').first()).toContainText('MSFT');
        expect(attempts).toBeGreaterThanOrEqual(2);
    });
});
