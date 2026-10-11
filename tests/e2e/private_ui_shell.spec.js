// Phase 25D1E1: the OFFLINE private Deep Ocean shell in real Chromium (desktop + Mobile Chrome), served locally from private_ui/ on port 3003.
// No backend, no Supabase, no login: it proves layout, honest state labelling, and the absence of storage/network/CSP problems. Not a deployment test.
import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:3003';
test.use({ baseURL: BASE });
const SECTIONS = [['overview', 'Genel Bakış'], ['opportunities', 'Yatırım Fırsatları'], ['portfolio', 'Portföy'], ['allocation', 'Dağılım ve Senaryolar'], ['research', 'Piyasa Araştırması'], ['risk', 'Risk ve Kanıt']];

test.describe('private shell (offline prototype)', () => {
    test('renders the six areas and every area shows an accurately labelled UNAVAILABLE state', async ({ page }) => {
        await page.goto('/');
        await expect(page.locator('.proto-banner')).toContainText('Çevrimdışı arayüz prototipi');
        await expect(page.locator('.nav-btn')).toHaveCount(6);
        for (const [id, label] of SECTIONS) {
            await page.locator(`.nav-btn[data-target="${id}"]`).click();
            await expect(page.locator(`[data-panel="${id}"]`)).toBeVisible();
            await expect(page.locator('[data-panel]:visible')).toHaveCount(1);
            await expect(page.locator(`.nav-btn[data-target="${id}"]`)).toHaveAttribute('aria-current', 'page');
            await expect(page.locator(`[data-panel="${id}"] h1`)).toHaveText(label);
            await expect(page.locator(`[data-panel="${id}"] .state-card .chip-unavailable`)).toHaveText('UNAVAILABLE');
        }
    });

    test('simple mode hides provenance, advanced mode reveals it, with no storage or cookies', async ({ page }) => {
        await page.goto('/');
        await expect(page.locator('.prov')).toBeHidden();
        await page.locator('[data-mode-set="advanced"]').click();
        await expect(page.locator('.prov')).toBeVisible();
        await expect(page.locator('.prov')).toContainText('NO_QUALIFIED_SOURCE_CONNECTED');
        await page.locator('[data-mode-set="simple"]').click();
        await expect(page.locator('.prov')).toBeHidden();
        const state = await page.evaluate(async () => ({ local: localStorage.length, session: sessionStorage.length, cookie: document.cookie, idb: indexedDB.databases ? (await indexedDB.databases()).length : 0, workers: (await navigator.serviceWorker.getRegistrations()).length }));
        expect(state).toEqual({ local: 0, session: 0, cookie: '', idb: 0, workers: 0 });
    });

    test('is responsive: no horizontal overflow, navigation reachable on every viewport', async ({ page }) => {
        for (const vp of [{ width: 1440, height: 900 }, { width: 820, height: 1000 }, { width: 390, height: 844 }]) {
            await page.setViewportSize(vp);
            await page.goto('/');
            await page.locator('[data-mode-set="advanced"]').click();
            for (const [id] of SECTIONS) {
                await page.locator(`.nav-btn[data-target="${id}"]`).click();
                const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
                expect(overflow, `${vp.width}px ${id}`).toBeLessThanOrEqual(0);
            }
            const nav = await page.locator('.sidenav').boundingBox();
            expect(nav.width).toBeLessThanOrEqual(vp.width);
        }
    });

    test('shows no number, percentage or currency amount in any panel (only the contract version string)', async ({ page }) => {
        await page.goto('/');
        await page.locator('[data-mode-set="advanced"]').click();
        for (const [id] of SECTIONS) {
            await page.locator(`.nav-btn[data-target="${id}"]`).click();
            const t = await page.locator(`[data-panel="${id}"]`).innerText();
            expect(t.replace(/private_portfolio_summary\.v1/, '')).not.toMatch(/[0-9%₺$€£]/);
        }
    });

    test('makes only same-origin requests, no CSP violations, no console errors and sends no token', async ({ page }) => {
        const origins = new Set();
        const headers = [];
        const logs = [];
        page.on('request', (r) => { origins.add(new URL(r.url()).origin); headers.push(r.headers()); });
        page.on('console', (m) => logs.push(`${m.type()}: ${m.text()}`));
        page.on('pageerror', (e) => logs.push(`pageerror: ${e.message}`));
        await page.addInitScript(() => {
            window.__csp = [];
            document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
        });
        await page.goto('/');
        for (const [id] of SECTIONS) await page.locator(`.nav-btn[data-target="${id}"]`).click();
        expect(await page.evaluate(() => window.__csp)).toEqual([]);
        expect([...origins]).toEqual([BASE]);
        expect(headers.some((h) => h.authorization || h.cookie)).toBe(false);
        expect(logs.filter((l) => /error|pageerror|refused|violat/i.test(l))).toEqual([]);
    });
});
