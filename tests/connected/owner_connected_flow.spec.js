// Phase 25D1E4: CONNECTED journey  PUBLIC BUFFETT -> OWNER LOGIN -> PRIVATE SENTINAX  in real Chromium and Mobile Chrome.
//
// REAL in this file: the public Buffett page, the built owner artifact, the bundled Supabase SDK, the MFA state machine, the private-session controller, and the FastAPI
// application behind /private/* and /buffett/* (real middleware, real JWT verifier with a synthetic ES256 JWKS, real require_private_owner, synthetic owner UUID).
// SYNTHETIC / ASSUMED: Supabase Auth is a browser-level mock (it only issues tokens that the harness signed); the `/private/*` hop is a loopback forwarder standing in for the
// future Cloudflare route; the owner and public pages are on two different loopback ports (= two origins). Nothing contacts Supabase, Oracle or Cloudflare.
// NOT proven here: a real Supabase project, real TOTP, real Cloudflare Pages/headers delivery, real Oracle deployment.
import { test, expect, request as pwRequest } from '@playwright/test';
import { OWNER_UID, OTHER_UID, SUPA, CODE, wire, durableState } from '../e2e/support/owner_mock_auth.js';
import { API, OWNER_ORIGIN, PUBLIC_ORIGIN, log, setOwner, sign, minter } from './support/harness.js';

const OWNER_PAGE = `${OWNER_ORIGIN}/owner/`;
const PRIVATE_URL = `${OWNER_ORIGIN}/private/portfolio/summary`;
const LOCKED = {
    denied: 'Sunucu sahip yetkisini onaylamadı; özel alan kapalı.',
    unavailable: 'Sunucu sahip yapılandırması yok veya hizmet kullanılamıyor; özel alan kapalı.',
};
const SECTIONS = [['overview', 'Genel Bakış'], ['opportunities', 'Yatırım Fırsatları'], ['portfolio', 'Portföy'], ['allocation', 'Dağılım ve Senaryolar'], ['research', 'Piyasa Araştırması'], ['risk', 'Risk ve Kanıt']];
const privateCalls = async () => (await log()).filter((e) => e.path.startsWith('/private'));

test.beforeEach(async () => { await log({ clear: true }); await setOwner(''); });
test.afterEach(async () => { await setOwner(''); });

async function blockExternalFonts(page) {
    await page.route(/fonts\.(googleapis|gstatic)\.com/, (route) => route.abort());
}
async function openOwnerPage(page, wiring = {}) {
    const w = await wire(page, wiring);
    await page.goto(OWNER_PAGE);
    await expect(page.locator('#view-signin')).toBeVisible();
    return w;
}
async function passwordSignIn(page) {
    await page.fill('#email', 'owner@example.invalid');
    await page.fill('#password', 'pw-correct');
    await page.click('#signin-form button[type=submit]');
}
async function enrollToAal2(page) {
    await expect(page.locator('#view-enroll')).toBeVisible();
    await page.click('#btn-enroll');
    await page.fill('#enroll-code', CODE);
    await page.click('#enroll-form button[type=submit]');
}
const allDigitsFree = (t) => !/[0-9%₺$€£]/.test(t);

test.describe('public Buffett -> owner login -> private Sentinax, against the real FastAPI', () => {
    test('the approved journey end to end, with the real backend answering', async ({ page, context }) => {
        await blockExternalFonts(page);
        // --- PUBLIC BUFFETT: anonymous, fail-closed, with a validated link to the separate owner origin -------------------------------------------------------------------------------
        await page.route('**/js/owner_entry_config.js', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: `window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: '${OWNER_ORIGIN}', path: '/owner/' });` }));
        await page.goto(`${PUBLIC_ORIGIN}/`);
        await expect(page.locator('#portfolio-grid .unavailable-state')).toBeVisible();
        const entry = page.locator('#owner-entry');
        await expect(entry).toHaveText('Sahip Girişi');
        await expect(entry).toHaveAttribute('href', OWNER_PAGE);
        await expect(entry).not.toHaveAttribute('aria-disabled', /.*/);
        await expect(entry).toHaveAttribute('rel', 'noopener noreferrer');
        const publicApi = (await log()).filter((e) => e.path.startsWith('/buffett'));
        expect(publicApi.length).toBeGreaterThanOrEqual(1);
        expect(publicApi.every((e) => e.status === 200 && !e.has_authorization && !e.has_cookie && !e.set_cookie)).toBe(true);
        expect(await privateCalls()).toHaveLength(0);
        expect(await page.evaluate(() => ({ l: localStorage.length, s: sessionStorage.length, c: document.cookie }))).toEqual({ l: 0, s: 0, c: '' });
        expect(await page.locator('body').innerText()).not.toMatch(/Authorization|access_token|sb-/);

        // --- navigate: plain link, nothing transferred ---------------------------------------------------------------------------------------------------------------------------------
        const { server, logs, origins } = await wire(page, { mint: minter() });
        const privateResponses = [];
        page.on('response', (r) => { if (r.url().startsWith(PRIVATE_URL)) privateResponses.push(r); });
        await Promise.all([page.waitForURL(OWNER_PAGE), entry.click()]);
        expect(page.url()).toBe(OWNER_PAGE);                                       // exactly the pinned URL: no query, no fragment, no token
        await expect(page.locator('#view-signin')).toBeVisible();                  // the owner page starts SIGNED_OUT
        await expect(page.locator('#app-root')).toBeHidden();
        expect(await page.evaluate(() => document.referrer)).toBe('');
        expect(await page.evaluate(() => ({ l: localStorage.length, s: sessionStorage.length, c: document.cookie }))).toEqual({ l: 0, s: 0, c: '' });
        expect(await privateCalls()).toHaveLength(0);

        // --- password alone / AAL1 reveals nothing and never reaches private data -----------------------------------------------------------------------------------------------------------
        await passwordSignIn(page);
        await expect(page.locator('#view-enroll')).toBeVisible();
        await expect(page.locator('#app-root')).toBeHidden();
        expect(await privateCalls()).toHaveLength(0);

        // --- TOTP -> AAL2 -> the REAL backend authorizes -> Deep Ocean ----------------------------------------------------------------------------------------------------------------------
        await page.click('#btn-enroll');
        await page.fill('#enroll-code', CODE);
        await page.click('#enroll-form button[type=submit]');
        await expect(page.locator('#app-root')).toBeVisible();
        await expect(page.locator('.owner-shell')).toBeHidden();
        const calls = await privateCalls();
        expect(calls).toHaveLength(1);
        expect(calls[0]).toMatchObject({ path: '/private/portfolio/summary', method: 'GET', status: 200, has_authorization: true, has_cookie: false, set_cookie: false, query: '' });
        expect(privateResponses).toHaveLength(1);
        const hdrs = await privateResponses[0].allHeaders();
        expect(hdrs['cache-control']).toBe('private, no-store');
        expect(hdrs['x-correlation-id']).toBeTruthy();                             // emitted by the real application's middleware: proof that FastAPI itself answered
        expect(hdrs['set-cookie']).toBeUndefined();
        for (const [id, label] of SECTIONS) {
            await page.locator(`.nav-btn[data-target="${id}"]`).click();
            await expect(page.locator(`[data-panel="${id}"] h1`)).toHaveText(label);
            await expect(page.locator(`[data-panel="${id}"] .state-card .chip-unavailable`)).toHaveText('UNAVAILABLE');
        }
        await page.locator('.nav-btn[data-target="overview"]').click();
        await page.locator('[data-mode-set="advanced"]').click();
        await expect(page.locator('#prov-version')).toHaveText('private_portfolio_summary.v1');
        await expect(page.locator('#prov-status')).toHaveText('UNAVAILABLE');
        await expect(page.locator('#prov-reason-code')).toHaveText('NO_QUALIFIED_SOURCE_CONNECTED');
        const generated = Date.parse(await page.locator('#prov-generated').textContent());
        expect(Math.abs(Date.now() - generated)).toBeLessThan(120000);             // produced by the live backend just now, not the 2026-01-01 test fixture
        await page.locator('[data-mode-set="simple"]').click();
        await expect(page.locator('.prov')).toBeHidden();

        // --- isolation: nothing durable, nothing shared with the public origin, nothing leaked -----------------------------------------------------------------------------------------
        expect(await durableState(page)).toMatchObject({ local: 0, session: 0, cookie: '', idb: 0, csp: [] });
        expect(await context.cookies()).toEqual([]);
        expect([...origins].filter((o) => !/fonts\./.test(o)).sort()).toEqual([OWNER_ORIGIN, SUPA].sort());
        expect(page.url()).toBe(OWNER_PAGE);
        const publicTab = await context.newPage();
        await blockExternalFonts(publicTab);
        await publicTab.goto(`${PUBLIC_ORIGIN}/`);
        expect(await publicTab.evaluate(() => ({ l: Object.keys(localStorage), s: Object.keys(sessionStorage), c: document.cookie }))).toEqual({ l: [], s: [], c: '' });
        await publicTab.close();
        const tokens = server.tokens;
        for (const t of tokens) expect(logs.join('\n')).not.toContain(t);
        expect(allDigitsFree(await page.locator('#app-root').evaluate((el) => [...el.querySelectorAll('h1,p,dt,dd,li,.chip')].map((n) => n.textContent).join(' ').replace(/private_portfolio_summary\.v1/g, '')))).toBe(true);

        // --- a second tab never inherits the session and never reaches private data ---------------------------------------------------------------------------------------------------
        const other = await context.newPage();
        await wire(other);
        await other.goto(OWNER_PAGE);
        await expect(other.locator('#view-signin')).toBeVisible();
        await expect(other.locator('#app-root')).toBeHidden();
        expect(await privateCalls()).toHaveLength(1);
        await other.close();

        // --- sign-out clears at once; a reload loses the memory-only session -----------------------------------------------------------------------------------------------------------
        await page.click('#btn-app-signout');
        await expect(page.locator('#view-signin')).toBeVisible();
        await expect(page.locator('#app-root')).toBeHidden();
        for (const id of ['live-reason', 'prov-version', 'prov-status', 'prov-reason-code', 'prov-generated']) await expect(page.locator(`#${id}`)).toHaveText('');
        await page.reload();
        await expect(page.locator('#view-signin')).toBeVisible();
        await expect(page.locator('#app-root')).toBeHidden();
        expect(await privateCalls()).toHaveLength(1);
    });

    test('an existing factor: password, wrong TOTP code (closed, no backend call), correct code (real backend 200)', async ({ page }) => {
        await openOwnerPage(page, { verifiedFactor: true, mint: minter() });
        await passwordSignIn(page);
        await expect(page.locator('#view-challenge')).toBeVisible();
        await page.fill('#challenge-code', '000000');
        await page.click('#challenge-form button[type=submit]');
        await expect(page.locator('#error')).toBeVisible();
        await expect(page.locator('#app-root')).toBeHidden();
        expect(await privateCalls()).toHaveLength(0);
        await page.fill('#challenge-code', CODE);
        await page.click('#challenge-form button[type=submit]');
        await expect(page.locator('#app-root')).toBeVisible();
        expect((await privateCalls()).map((e) => e.status)).toEqual([200]);
    });

    test('a foreign user with a valid AAL2+TOTP token is refused by the real backend (403) and never sees the application', async ({ page }) => {
        await openOwnerPage(page, { uid: OTHER_UID, mint: minter() });
        await passwordSignIn(page);
        await enrollToAal2(page);
        await expect(page.locator('#authz-status')).toHaveText(LOCKED.denied);
        await expect(page.locator('#app-root')).toBeHidden();
        const calls = await privateCalls();
        expect(calls.length).toBeGreaterThanOrEqual(1);
        expect(calls.every((e) => e.status === 403)).toBe(true);
        expect(await page.content()).not.toContain('private_portfolio_summary.v1</td>');
        await page.click('#btn-signout');
        await expect(page.locator('#view-signin')).toBeVisible();
    });

    for (const [variant, status] of [['wrong_signature', 401], ['wrong_issuer', 401], ['wrong_audience', 401], ['malformed_amr', 403], ['no_totp', 403]]) {
        test(`a ${variant} token for the owner is rejected by the real verifier (${status}) and the interface stays closed`, async ({ page }) => {
            await openOwnerPage(page, { mint: minter(variant) });
            await passwordSignIn(page);
            await enrollToAal2(page);
            await expect(page.locator('#authz-status')).toHaveText(LOCKED.denied);
            await expect(page.locator('#app-root')).toBeHidden();
            const calls = await privateCalls();
            expect(calls.length).toBeGreaterThanOrEqual(1);
            expect(calls.every((e) => e.status === status)).toBe(true);
        });
    }

    test('missing owner configuration makes the real backend answer 503 and the interface stays closed', async ({ page }) => {
        await setOwner(null);
        await openOwnerPage(page, { mint: minter() });
        await passwordSignIn(page);
        await enrollToAal2(page);
        await expect(page.locator('#authz-status')).toHaveText(LOCKED.unavailable);
        await expect(page.locator('#app-root')).toBeHidden();
        expect((await privateCalls()).every((e) => e.status === 503)).toBe(true);
    });

    test('a late backend answer after sign-out never repopulates the interface', async ({ page }) => {
        let release;
        const gate = new Promise((r) => { release = r; });
        await page.route(PRIVATE_URL, async (route) => { await gate; try { await route.continue(); } catch { /* aborted by the page */ } });
        await openOwnerPage(page, { mint: minter() });
        await passwordSignIn(page);
        await enrollToAal2(page);
        await expect(page.locator('#authz-status')).toHaveText('Sunucu sahip yetkilendirmesi doğrulanıyor…');
        await page.click('#btn-signout');
        await expect(page.locator('#view-signin')).toBeVisible();
        release();
        await page.waitForTimeout(600);
        await expect(page.locator('#app-root')).toBeHidden();
        await expect(page.locator('#live-reason')).toHaveText('');
        await expect(page.locator('#authz-status')).toHaveText('');
        await expect(page.locator('#view-signin')).toBeVisible();
    });

    test('with no destination configured the public link stays disabled and has no href', async ({ page }) => {
        await blockExternalFonts(page);
        await page.goto(`${PUBLIC_ORIGIN}/`);
        const entry = page.locator('#owner-entry');
        await expect(entry).toHaveAttribute('aria-disabled', 'true');
        await expect(entry).not.toHaveAttribute('href', /.*/);
        await entry.click({ force: true, noWaitAfter: true });
        expect(page.url()).toBe(`${PUBLIC_ORIGIN}/`);
        // the public header (title + three navigation items) must fit every viewport: no horizontal overflow, link fully inside the viewport
        for (const vp of [{ width: 1440, height: 900 }, { width: 820, height: 1000 }, { width: 390, height: 844 }, { width: 320, height: 640 }]) {
            await page.setViewportSize(vp);
            expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), `${vp.width}px`).toBeLessThanOrEqual(0);
            const box = await entry.boundingBox();
            expect(box.x).toBeGreaterThanOrEqual(0);
            expect(box.x + box.width).toBeLessThanOrEqual(vp.width);
        }
    });

    test('a hostile query string on the public page cannot redirect the link or add parameters', async ({ page }) => {
        await blockExternalFonts(page);
        await page.route('**/js/owner_entry_config.js', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: `window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: '${OWNER_ORIGIN}', path: '/owner/' });` }));
        await page.goto(`${PUBLIC_ORIGIN}/?owner=https://evil.example&next=//evil.example&access_token=abc#https://evil.example`);
        await expect(page.locator('#owner-entry')).toHaveAttribute('href', OWNER_PAGE);
    });

    test('an unapproved configured origin leaves the public link disabled', async ({ page }) => {
        await blockExternalFonts(page);
        await page.route('**/js/owner_entry_config.js', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: "window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: 'http://evil.example:3005', path: '/owner/' });" }));
        await page.goto(`${PUBLIC_ORIGIN}/`);
        await expect(page.locator('#owner-entry')).toHaveAttribute('aria-disabled', 'true');
        await expect(page.locator('#owner-entry')).not.toHaveAttribute('href', /.*/);
    });
});

test.describe('HTTP-level verdicts of the real FastAPI through the same-origin forwarder', () => {
    test('exact statuses for missing, expired, wrongly signed, AAL1, foreign and valid tokens; no cookies are ever set; writes do not exist', async () => {
        const http = await pwRequest.newContext({ baseURL: OWNER_ORIGIN });
        const bearer = (t) => ({ Authorization: `Bearer ${t}` });
        const get = (h) => http.get('/private/portfolio/summary', { headers: h, maxRedirects: 0 });
        expect((await get({})).status()).toBe(401);
        expect((await get(bearer(await sign({ variant: 'expired' })))).status()).toBe(401);
        expect((await get(bearer(await sign({ variant: 'wrong_signature' })))).status()).toBe(401);
        expect((await get(bearer(await sign({ variant: 'valid', aal: 'aal1' })))).status()).toBe(403);
        expect((await get(bearer(await sign({ variant: 'valid', sub: OTHER_UID })))).status()).toBe(403);
        const ok = await get(bearer(await sign({ variant: 'valid' })));
        expect(ok.status()).toBe(200);
        const body = await ok.json();
        expect(body).toMatchObject({ contract_version: 'private_portfolio_summary.v1', data_status: 'UNAVAILABLE', source: null, as_of: null, portfolios: [], positions: [] });
        expect(ok.headers()['set-cookie']).toBeUndefined();
        const direct = await pwRequest.newContext({ baseURL: API });
        const t = await sign({ variant: 'valid' });
        for (const method of ['post', 'put', 'patch', 'delete']) expect([404, 405]).toContain((await direct[method]('/private/portfolio/summary', { headers: bearer(t) })).status());
        expect((await log()).every((e) => !e.set_cookie)).toBe(true);
        expect((await http.post('/private/portfolio/summary', { headers: bearer(t) })).status()).toBe(405);
        await http.dispose();
        await direct.dispose();
    });
});
