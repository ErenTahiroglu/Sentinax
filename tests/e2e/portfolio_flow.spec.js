import { test, expect } from '@playwright/test';

test.describe('End-to-End Portfolio Analysis Flow', () => {
    test.beforeEach(async ({ page }) => {
        // App.init() is async; wait for it so handlers are bound before interacting
        const ready = page.waitForEvent('console', {
            predicate: (m) => m.text().includes('App Initialized Successfully'),
            timeout: 15000
        });
        await Promise.all([ready, page.goto('/')]);
    });

    test('should allow guest login and run analysis', async ({ page }) => {
        // Mock the API with the current SSE contract
        await page.route('**/api/analyze', async (route) => {
            const items = [
                { ticker: 'AAPL', analysis: 'Strong buy signal.', score: 15 },
                { ticker: 'TSLA', analysis: 'Volatile but growth potential.', score: 5 }
            ];
            await route.fulfill({
                status: 200,
                contentType: 'text/event-stream',
                body: items.map(i => `data: ${JSON.stringify(i)}\n\n`).join('')
            });
        });

        // 1. Bypass landing page via guest entry
        await page.getByRole('button', { name: 'Misafir Olarak Dene', exact: true }).click();

        // 2. Ensure main app is usable
        await expect(page.locator('#ticker-input')).toBeVisible();
        await expect(page.locator('#analyze-btn')).toBeVisible();

        // 3. Enter tickers
        await page.locator('#ticker-input').fill('AAPL, TSLA');

        // 4. Start analysis
        const analyzeBtn = page.locator('#analyze-btn');
        await analyzeBtn.click();

        // 5. Wait for results section to appear
        await expect(page.locator('#results')).toBeVisible({ timeout: 10000 });
        
        // 6. Verify each streamed ticker rendered as a real result card
        const cards = page.locator('#results-grid .result-card:not(.skeleton-card)');
        await expect(cards).toHaveCount(2);
        await expect(cards.filter({ hasText: 'AAPL' })).toHaveCount(1);
        await expect(cards.filter({ hasText: 'TSLA' })).toHaveCount(1);
    });

    test('should show auth modal when clicking primary action without login', async ({ page }) => {
        // This test assumes we are on landing page
        await page.locator('#landing-nav-login-btn').click();
        await expect(page.locator('#login-modal')).toBeVisible();
        
        // Try password visibility toggle
        const pwInput = page.locator('#auth-password');
        await expect(pwInput).toBeVisible();
        await expect(pwInput).toHaveAttribute('type', 'password');
        const toggle = page.getByTitle('Şifreyi göster/gizle');
        await toggle.click();
        await expect(pwInput).toHaveAttribute('type', 'text');
        await toggle.click();
        await expect(pwInput).toHaveAttribute('type', 'password');
    });
});
