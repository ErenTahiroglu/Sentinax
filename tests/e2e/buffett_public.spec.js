// Phase 25D1E5: the PUBLIC Buffett interface in real Chromium and Mobile Chrome.
//  * the BUILT artifact (deploy/buffett-cloudflare/dist) served by the local Cloudflare-model server on port 3007 with its _headers (strict CSP) and 404.html;
//  * the SOURCE page on port 3000 (no CSP) so DOM-level injection safety is proven independently of the CSP.
// Synthetic AVAILABLE payloads are MOCK fixtures that only exercise the renderer; live evidence stays UNAVAILABLE and nothing here qualifies any source.
// Local models of Cloudflare routing/headers are local evidence only; the real platform is unverified until an actual deployment.
import { test, expect, request as pwRequest } from '@playwright/test';

const ART = 'http://127.0.0.1:3007';
const OWNER_TEST = 'http://127.0.0.1:3005';
const TARGETS = [['artifact (strict CSP)', `${ART}/`], ['source page (no CSP)', 'http://127.0.0.1:3000/buffett/index.html']];
const PAYLOADS = ['<img src=x onerror="window.__xss=1">', '<script>window.__xss=1</script>', '"><svg onload=window.__xss=1>', "' onmouseover='window.__xss=1' x='", '<a href="javascript:window.__xss=1">x</a>'];

const mockStock = (over = {}) => ({
    ticker: 'MOCKA', name: 'MOCK Holding A.S.', total_score: 72.5, current_price: 10.25, target_entry: [8, 9.5], margin_of_safety_pct: 12.345,
    details: { moat_score: 15, profitability_score: 20, balance_sheet_score: 12, valuation_score: 25, dcf: { base_case: 11, bear_case: 8, bull_case: 14, intrinsic_value: 11 }, llm_analysis: 'MOCK analysis', passed_veto: true },
    ...over,
});
const availableBody = (extra) => JSON.stringify({ data_status: 'AVAILABLE', reason_code: 'MOCK', reason: 'mock', warnings: [], as_of: '2026-01-01T00:00:00+00:00', generated_at: '2026-01-01T00:00:00+00:00', portfolio: [], changes: [], ...extra });
const json = (body) => ({ status: 200, contentType: 'application/json', body });

async function collect(page) {
    const state = { csp: [], errors: [], origins: new Set() };
    page.on('request', (r) => state.origins.add(new URL(r.url()).origin));
    page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource|Failed to fetch/.test(m.text())) state.errors.push(m.text()); });
    page.on('pageerror', (e) => state.errors.push(`pageerror: ${e.message}`));
    await page.addInitScript(() => {
        window.__csp = [];
        document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
    });
    return state;
}

test.describe('public Buffett artifact (local Cloudflare model)', () => {
    test('the root loads anonymously, the owner entry is disabled without an approved destination, and nothing external is requested', async ({ page }) => {
        const st = await collect(page);
        await page.goto(`${ART}/`);
        await expect(page.locator('.logo-text')).toContainText('Buffett');
        const entry = page.locator('#owner-entry');
        await expect(entry).toHaveText('Sahip Girişi');
        await expect(entry).toHaveAttribute('aria-disabled', 'true');
        await expect(entry).not.toHaveAttribute('href', /.*/);
        await expect(page.locator('#portfolio-grid .unavailable-state')).toBeVisible();     // the API is absent on the static origin: fail closed, never fake data
        await expect(page.locator('.stock-card')).toHaveCount(0);
        expect(await page.evaluate(() => window.__csp)).toEqual([]);
        expect([...st.origins]).toEqual([ART]);
        expect(st.errors).toEqual([]);
        expect(await page.evaluate(() => ({ l: localStorage.length, s: sessionStorage.length, c: document.cookie, w: 'serviceWorker' in navigator ? 0 : 0 }))).toMatchObject({ l: 0, s: 0, c: '' });
        expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length)).toBe(0);
        await expect(page.locator('form, input, [type=password]')).toHaveCount(0);
    });

    test('with no API at all the history tab is unavailable too and no fabricated AVAILABLE result ever appears', async ({ page }) => {
        await page.goto(`${ART}/`);
        await page.click('#btn-history');
        await expect(page.locator('#history-list .unavailable-state')).toBeVisible();
        await expect(page.locator('.history-item, .stock-card')).toHaveCount(0);
    });

    test('an approved test-only destination enables a plain link to the separate owner origin; an unapproved one leaves it disabled; no token or cookie is attached', async ({ page }) => {
        await page.route('**/js/owner_entry_config.js', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: `window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: '${OWNER_TEST}', path: '/owner/' });` }));
        await page.goto(`${ART}/`);
        const entry = page.locator('#owner-entry');
        await expect(entry).toHaveAttribute('href', `${OWNER_TEST}/owner/`);
        const href = await entry.getAttribute('href');
        expect(new URL(href).search + new URL(href).hash).toBe('');
        expect(href).not.toMatch(/token|eyJ|@/);
        await expect(entry).toHaveAttribute('rel', 'noopener noreferrer');
        expect(await page.context().cookies()).toEqual([]);
        await page.unroute('**/js/owner_entry_config.js');
        await page.route('**/js/owner_entry_config.js', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: "window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: 'http://evil.example:3005', path: '/owner/' });" }));
        await page.reload();
        await expect(page.locator('#owner-entry')).not.toHaveAttribute('href', /.*/);
        await page.unroute('**/js/owner_entry_config.js');
        await page.route('**/js/owner_entry_config.js', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: `window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: '${ART}', path: '/owner/' });` }));
        await page.reload();
        await expect(page.locator('#owner-entry')).not.toHaveAttribute('href', /.*/);          // the public origin is never used as the owner destination
    });

    test('headers: strict CSP and security headers on every response, unknown routes are 404 dead ends (no SPA fallback), /owner/* and /private/* are not served', async () => {
        const http = await pwRequest.newContext({ baseURL: ART });
        for (const u of ['/', '/css/styles.css', '/js/ui.js', '/nope']) {
            const r = await http.get(u, { maxRedirects: 0 });
            const h = r.headers();
            expect(h['content-security-policy'], u).toContain("script-src 'self'");
            expect(h['content-security-policy'], u).not.toMatch(/unsafe-inline|unsafe-eval|\*|https?:/);
            expect(h['content-security-policy'], u).toContain("frame-ancestors 'none'");
            expect(h['x-content-type-options'], u).toBe('nosniff');
            expect(h['x-frame-options'], u).toBe('DENY');
            expect(h['referrer-policy'], u).toBe('no-referrer');
            expect(h['access-control-allow-origin'], u).toBeUndefined();
        }
        expect((await http.get('/', { maxRedirects: 0 })).status()).toBe(200);
        expect((await http.get('/index.html', { maxRedirects: 0 })).headers().location).toBe('/');
        for (const u of ['/owner', '/owner/', '/owner/index.html', '/owner/js/owner_app.js', '/private/portfolio/summary', '/private/x', '/frontend/index.html', '/sw.js', '/manifest.json', '/api/health', '/buffett/portfolio', '/_headers', '/js/missing.js']) {
            const r = await http.get(u);
            expect(r.status(), u).toBe(404);
            const body = await r.text();
            expect(body, u).not.toMatch(/Buffett|Sahip|owner_app|Sentinax/);
            expect(body, u).toContain('404');
        }
        expect((await http.post('/private/portfolio/summary', { data: {} })).status()).toBeGreaterThanOrEqual(400);
        await http.dispose();
    });

    test('desktop and mobile layouts stay functional (no horizontal overflow, header link inside the viewport)', async ({ page }) => {
        await page.goto(`${ART}/`);
        for (const vp of [{ width: 1440, height: 900 }, { width: 820, height: 1000 }, { width: 390, height: 844 }, { width: 320, height: 640 }]) {
            await page.setViewportSize(vp);
            expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), `${vp.width}px`).toBeLessThanOrEqual(0);
            const box = await page.locator('#owner-entry').boundingBox();
            expect(box.x).toBeGreaterThanOrEqual(0);
            expect(box.x + box.width).toBeLessThanOrEqual(vp.width);
        }
    });
});

for (const [label, url] of TARGETS) {
    test.describe(`SAFE rendering of synthetic MOCK evidence, ${label}`, () => {
        test('adversarial company, ticker, analysis, reason and date appear as literal text and create no element or handler', async ({ page }) => {
            const st = await collect(page);
            const stocks = PAYLOADS.map((p, i) => mockStock({ ticker: i === 1 ? p : `MOCK${i}`, name: p, details: { ...mockStock().details, llm_analysis: p } }));
            const changes = PAYLOADS.map((p, i) => ({ action: i % 2 ? 'REMOVED' : 'ADDED', ticker: p, reason: p, date: p }));
            await page.route('**/buffett/portfolio', (route) => route.fulfill(json(availableBody({ portfolio: stocks }))));
            await page.route('**/buffett/history', (route) => route.fulfill(json(availableBody({ changes }))));
            await page.goto(url);
            await expect(page.locator('#portfolio-grid .stock-card')).toHaveCount(PAYLOADS.length);
            const grid = await page.locator('#portfolio-grid').evaluate((el) => ({ text: el.textContent, bad: el.querySelectorAll('img, script, svg, iframe, object, embed, link, style, form, input, a, [onerror], [onload], [onclick], [onmouseover]').length }));
            expect(grid.bad).toBe(0);
            for (const p of PAYLOADS) expect(grid.text).toContain(p);
            await page.locator('#portfolio-grid .stock-card').nth(2).click();
            await expect(page.locator('#detail-drawer')).not.toHaveClass(/hidden/);
            const drawer = await page.locator('#drawer-content').evaluate((el) => ({ text: el.textContent, bad: el.querySelectorAll('img, script, svg, iframe, object, embed, link, style, form, input, a, [onerror], [onload], [onmouseover]').length }));
            expect(drawer.bad).toBe(0);
            expect(drawer.text).toContain(PAYLOADS[2]);
            await page.click('#close-drawer');
            await expect(page.locator('#detail-drawer')).toHaveClass(/hidden/);
            await page.click('#btn-history');
            await expect(page.locator('#history-list .history-item')).toHaveCount(PAYLOADS.length);
            const hist = await page.locator('#history-list').evaluate((el) => ({ text: el.textContent, bad: el.querySelectorAll('img, script, svg, iframe, object, embed, link, style, form, input, a, [onerror], [onload], [onmouseover]').length }));
            expect(hist.bad).toBe(0);
            for (const p of PAYLOADS) expect(hist.text).toContain(p);
            expect(await page.evaluate(() => window.__xss)).toBeUndefined();
            expect(await page.evaluate(() => window.__csp)).toEqual([]);                    // nothing even attempted to run (no CSP violation either)
            expect(st.errors).toEqual([]);
        });

        test('malformed AVAILABLE payloads never crash the page or show NaN, Infinity, zero or invented values', async ({ page }) => {
            const st = await collect(page);
            const stocks = [mockStock({ ticker: 'BADPRICE', current_price: 'x' }), mockStock({ ticker: 'BADSCORE', total_score: null }), mockStock({ ticker: 'BADENTRY', target_entry: [1] }), mockStock({ ticker: 'NODETAILS', details: undefined }), null, 5, mockStock({ ticker: 'MOCKOK' })];
            await page.route('**/buffett/portfolio', (route) => route.fulfill(json(availableBody({ portfolio: stocks }))));
            await page.goto(url);
            await expect(page.locator('#portfolio-grid .stock-card')).toHaveCount(stocks.length);
            await expect(page.locator('#portfolio-grid .invalid-evidence')).toHaveCount(5);
            const bad = await page.locator('#portfolio-grid .invalid-evidence').allTextContents();
            for (const t of bad) expect(t).not.toMatch(/NaN|Infinity|undefined|null|₺|%|\d\.\d/);
            await page.locator('#portfolio-grid .stock-card', { hasText: 'NODETAILS' }).click().catch(() => {});
            await expect(page.locator('#portfolio-grid .stock-card', { hasText: 'MOCKOK' })).not.toHaveClass(/invalid-evidence/);
            expect(st.errors).toEqual([]);
        });

        test('a payload whose list is not an array is rendered as unavailable (invalid evidence), never as an empty success', async ({ page }) => {
            await page.route('**/buffett/portfolio', (route) => route.fulfill(json(availableBody({ portfolio: { 0: 'x' } }))));
            await page.goto(url);
            await expect(page.locator('#portfolio-grid .unavailable-state')).toContainText('Geçersiz kanıt');
            await expect(page.locator('.stock-card')).toHaveCount(0);
        });

        test('very long adversarial text wraps inside the layout on desktop and mobile', async ({ page }) => {
            await page.route('**/buffett/portfolio', (route) => route.fulfill(json(availableBody({ portfolio: [mockStock({ name: 'W'.repeat(190), ticker: 'T'.repeat(60) })] }))));
            await page.goto(url);
            await expect(page.locator('#portfolio-grid .stock-card')).toHaveCount(1);
            for (const vp of [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 320, height: 640 }]) {
                await page.setViewportSize(vp);
                expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), `${vp.width}px`).toBeLessThanOrEqual(0);
            }
        });
    });
}
