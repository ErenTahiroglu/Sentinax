// Phase 25D1E3: the integrated owner document (sign-in -> MFA -> server owner authorization -> Deep Ocean application -> sign-out) in real Chromium and Mobile Chrome,
// served from the BUILT Cloudflare artifact with its header CSP (local model, port 3002; not Cloudflare). The real bundled Supabase SDK runs against a mocked Auth origin and
// the private API is a TEST-ONLY mock of the server's rules (owner + aal2 + totp) replaying the contract fixture. That mock is NOT the FastAPI application: the backend's own
// authorization is proven by backend/tests/test_private_portfolio_summary_api.py; a live end-to-end flow through the real backend remains unverified.
import fs from 'fs';
import { test, expect } from '@playwright/test';
import { OWNER_UID, OTHER_UID, SUPA, CODE, wire, durableState } from './support/owner_mock_auth.js';

const BASE = 'http://127.0.0.1:3002';
const PAGE = '/owner/';
const API = `${BASE}/private/portfolio/summary`;
test.use({ baseURL: BASE });
const FIXTURE = JSON.parse(fs.readFileSync('tests/fixtures/private_portfolio_summary_unavailable.json', 'utf8'));
const SECTIONS = [['overview', 'Genel Bakış'], ['opportunities', 'Yatırım Fırsatları'], ['portfolio', 'Portföy'], ['allocation', 'Dağılım ve Senaryolar'], ['research', 'Piyasa Araştırması'], ['risk', 'Risk ve Kanıt']];

const claimsOf = (auth) => { try { return JSON.parse(Buffer.from(auth.replace('Bearer ', '').split('.')[1], 'base64url').toString()); } catch { return {}; } };

/** Test-only stand-in for the server's owner rule. Records what the browser sent. */
async function privateApi(page, { mode = 'rules', owner = OWNER_UID, gate = null } = {}) {
    const calls = [];
    await page.route(API, async (route) => {
        const req = route.request();
        const headers = req.headers();
        calls.push({ url: req.url(), method: req.method(), headers });
        if (gate) await gate;
        const c = claimsOf(headers.authorization || '');
        const isOwner = c.sub === owner && c.aal === 'aal2' && Array.isArray(c.amr) && c.amr.some((e) => e.method === 'totp');
        const body = JSON.stringify({ ...FIXTURE, generated_at: new Date().toISOString() });
        const json = { 'content-type': 'application/json', 'cache-control': 'private, no-store' };
        try {
            if (mode === 'rules') {
                if (!headers.authorization) return await route.fulfill({ status: 401, headers: json, body: '{"error":true,"message":"detail-not-shown"}' });
                return isOwner ? await route.fulfill({ status: 200, headers: json, body }) : await route.fulfill({ status: 403, headers: json, body: '{"error":true,"message":"forbidden"}' });
            }
            if (mode === 'abort') return await route.abort('connectionfailed');
            if (mode === 'html') return await route.fulfill({ status: 200, headers: { 'content-type': 'text/html' }, body: '<!doctype html><title>owner</title>' });
            if (mode === 'empty') return await route.fulfill({ status: 200, headers: json, body: JSON.stringify({ ...FIXTURE, data_status: 'EMPTY', source: 'ledger', as_of: '2026-01-01T00:00:00+00:00' }) });
            return await route.fulfill({ status: Number(mode), headers: json, body: '{"error":true,"message":"detail-not-shown"}' });
        } catch { /* the page aborted the request (sign-out): nothing to deliver */ }
    });
    return calls;
}

async function signIn(page) {
    await page.goto(PAGE);
    await expect(page.locator('#view-signin')).toBeVisible();
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
async function challengeToAal2(page) {
    await expect(page.locator('#view-challenge')).toBeVisible();
    await page.fill('#challenge-code', CODE);
    await page.click('#challenge-form button[type=submit]');
}

test.describe('integrated owner application (artifact, local model)', () => {
    test('SIGNED_OUT -> password -> AAL1 -> TOTP enrolment -> AAL2 -> server authorization -> Deep Ocean -> sign-out -> SIGNED_OUT', async ({ page }) => {
        const { server, logs, origins } = await wire(page);
        const calls = await privateApi(page);
        await signIn(page);
        await expect(page.locator('#view-enroll')).toBeVisible();
        expect(calls).toHaveLength(0);                                        // AAL1: the server is never asked
        await enrollToAal2(page);

        await expect(page.locator('#app-root')).toBeVisible();
        await expect(page.locator('.owner-shell')).toBeHidden();             // never two contradictory active views
        await expect(page.locator('#view-signin')).toBeHidden();
        expect(calls).toHaveLength(1);
        for (const [id, label] of SECTIONS) {
            await page.locator(`.nav-btn[data-target="${id}"]`).click();
            await expect(page.locator(`[data-panel="${id}"]`)).toBeVisible();
            await expect(page.locator('[data-panel]:visible')).toHaveCount(1);
            await expect(page.locator(`[data-panel="${id}"] h1`)).toHaveText(label);
            await expect(page.locator(`[data-panel="${id}"] .state-card .chip-unavailable`)).toHaveText('UNAVAILABLE');
        }
        await page.locator('.nav-btn[data-target="overview"]').click();
        await expect(page.locator('#live-reason')).toContainText('doğrulanmış bir kaynak henüz bağlı değil');
        await page.locator('[data-mode-set="advanced"]').click();
        await expect(page.locator('#prov-version')).toHaveText('private_portfolio_summary.v1');
        await expect(page.locator('#prov-status')).toHaveText('UNAVAILABLE');
        await expect(page.locator('#prov-reason-code')).toHaveText('NO_QUALIFIED_SOURCE_CONNECTED');
        await expect(page.locator('#prov-warnings li')).not.toHaveCount(0);
        await expect(page.locator('#prov-generated')).not.toHaveText('');

        // the request itself
        const c = calls[0];
        expect(c.method).toBe('GET');
        expect(c.url).toBe(API);
        expect(c.headers.authorization).toMatch(/^Bearer [\w-]+\.[\w-]+\.[\w-]+$/);
        expect(c.headers.cookie).toBeUndefined();
        expect(c.headers.referer).toBeUndefined();
        // (the cache:'no-store' / credentials:'omit' / redirect:'error' flags are asserted at the source level and in the controller unit tests)
        expect(new URL(c.url).search + new URL(c.url).hash).toBe('');

        // nothing durable, nothing leaked
        const state = await durableState(page);
        expect(state).toMatchObject({ local: 0, session: 0, cookie: '', idb: 0 });
        expect(state.csp).toEqual([]);
        expect([...origins].sort()).toEqual([BASE, SUPA].sort());
        expect(page.url()).toBe(`${BASE}${PAGE}`);
        const consoleText = logs.join('\n');
        for (const secret of [CODE, 'pw-correct', ...server.tokens]) expect(consoleText).not.toContain(secret);

        // sign-out clears everything session-derived
        await page.click('#btn-app-signout');
        await expect(page.locator('#view-signin')).toBeVisible();
        await expect(page.locator('#app-root')).toBeHidden();
        for (const id of ['live-reason', 'prov-version', 'prov-status', 'prov-reason-code', 'prov-generated']) await expect(page.locator(`#${id}`)).toHaveText('', { useInnerText: false });
        await expect(page.locator('#prov-warnings li')).toHaveCount(0);
        await expect(page.locator('[data-mode-set="simple"]')).toHaveAttribute('aria-pressed', 'true');
        expect(await page.locator('#app-root').evaluate((el) => el.textContent.includes('private_portfolio_summary.v1'))).toBe(false);
    });

    test('an existing verified factor: password -> AAL1 -> TOTP challenge -> AAL2 -> authorized; the server is not asked before AAL2', async ({ page }) => {
        await wire(page, { verifiedFactor: true });
        const calls = await privateApi(page);
        await signIn(page);
        await challengeToAal2(page);
        await expect(page.locator('#app-root')).toBeVisible();
        expect(calls).toHaveLength(1);
        expect(claimsOf(calls[0].headers.authorization).aal).toBe('aal2');
    });

    test('wrong TOTP code keeps everything closed and never contacts the server', async ({ page }) => {
        await wire(page, { verifiedFactor: true });
        const calls = await privateApi(page);
        await signIn(page);
        await expect(page.locator('#view-challenge')).toBeVisible();
        await page.fill('#challenge-code', '000000');
        await page.click('#challenge-form button[type=submit]');
        await expect(page.locator('#error')).toBeVisible();
        await expect(page.locator('#app-root')).toBeHidden();
        expect(calls).toHaveLength(0);
    });

    test('a non-owner who reaches client-side AAL2 is refused by the server: no private view, only the locked message', async ({ page }) => {
        await wire(page, { uid: OTHER_UID });
        const calls = await privateApi(page);
        await signIn(page);
        await enrollToAal2(page);
        await expect(page.locator('#view-unlocked')).toBeVisible();
        await expect(page.locator('#authz-status')).toHaveText('Sunucu sahip yetkisini onaylamadı; özel alan kapalı.');
        await expect(page.locator('#app-root')).toBeHidden();
        expect(calls.length).toBeGreaterThanOrEqual(1);
        expect(await page.content()).not.toContain('private_portfolio_summary.v1</td>');
        await page.click('#btn-signout');
        await expect(page.locator('#view-signin')).toBeVisible();
        await expect(page.locator('#authz-status')).toHaveText('');
    });

    for (const [mode, message] of [
        ['401', 'Sunucu sahip yetkisini onaylamadı; özel alan kapalı.'],
        ['503', 'Sunucu sahip yapılandırması yok veya hizmet kullanılamıyor; özel alan kapalı.'],
        ['500', 'Sunucu yetkilendirmesi tamamlanamadı; özel alan kapalı.'],
        ['abort', 'Özel API’ye ulaşılamadı; özel alan kapalı.'],
        ['html', 'Sunucu yanıtı doğrulanamadı; özel alan kapalı.'],
        ['empty', 'Sunucu yanıtı doğrulanamadı; özel alan kapalı.'],
    ]) {
        test(`server answer "${mode}" fails closed with a fixed message, no private view and no echo of server text`, async ({ page }) => {
            await wire(page);
            await privateApi(page, { mode });
            await signIn(page);
            await enrollToAal2(page);
            await expect(page.locator('#authz-status')).toHaveText(message);
            await expect(page.locator('#app-root')).toBeHidden();
            expect(await page.content()).not.toContain('detail-not-shown');
            expect(await page.locator('#prov-version').textContent()).toBe('');
        });
    }

    test('a late server answer after sign-out never populates the interface', async ({ page }) => {
        await wire(page);
        let release;
        const gate = new Promise((r) => { release = r; });
        const calls = await privateApi(page, { gate });
        await signIn(page);
        await enrollToAal2(page);
        await expect(page.locator('#authz-status')).toHaveText('Sunucu sahip yetkilendirmesi doğrulanıyor…');
        await expect.poll(() => calls.length).toBe(1);
        await page.click('#btn-signout');                                      // sign out while the request is still pending
        await expect(page.locator('#view-signin')).toBeVisible();
        release();
        await page.waitForTimeout(400);
        await expect(page.locator('#app-root')).toBeHidden();
        await expect(page.locator('#view-signin')).toBeVisible();
        await expect(page.locator('#live-reason')).toHaveText('');
        await expect(page.locator('#authz-status')).toHaveText('');
    });

    test('a reload drops the session; a second tab does not inherit it and never calls the server', async ({ page, context }) => {
        await wire(page);
        const calls = await privateApi(page);
        await signIn(page);
        await enrollToAal2(page);
        await expect(page.locator('#app-root')).toBeVisible();
        const other = await context.newPage();
        await wire(other);
        const otherCalls = await privateApi(other);
        await other.goto(PAGE);
        await expect(other.locator('#view-signin')).toBeVisible();
        await expect(other.locator('#app-root')).toBeHidden();
        expect(otherCalls).toHaveLength(0);
        await page.reload();
        await expect(page.locator('#view-signin')).toBeVisible();
        await expect(page.locator('#app-root')).toBeHidden();
        expect(calls.length).toBe(1);
    });

    test('the page carries no legacy frontend, no worker, no foreign script and enforces its header CSP', async ({ page }) => {
        const { origins } = await wire(page);
        const urls = [];
        page.on('request', (r) => urls.push(r.url()));
        await privateApi(page);
        await signIn(page);
        await enrollToAal2(page);
        await expect(page.locator('#app-root')).toBeVisible();
        expect(urls.filter((u) => u.startsWith(BASE)).map((u) => new URL(u).pathname).sort()).toEqual(
            ['/owner/', '/owner/css/deep_ocean.css', '/owner/css/owner.css', '/owner/js/client_config.js', '/owner/js/mfa_flow.js', '/owner/js/owner_app.js', '/owner/js/owner_public_config.js',
             '/owner/js/private_api_config.js', '/owner/js/private_session.js', '/owner/js/private_shell.js', '/owner/vendor/supabase-js.2.117.1.mjs', '/private/portfolio/summary'].sort());
        expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length)).toBe(0);
        const seenBeforeProbe = [...origins].sort();
        expect(seenBeforeProbe).toEqual([BASE, SUPA].sort());
        const res = await page.evaluate(async () => {
            const out = { inlineBlocked: false, foreignBlocked: false, foreignConnectBlocked: false };
            const inline = document.createElement('script');
            inline.textContent = 'window.__inlineRan = true';
            document.head.appendChild(inline);
            out.inlineBlocked = window.__inlineRan !== true;
            try { await fetch('https://evil.example/x', { mode: 'no-cors' }); } catch { out.foreignConnectBlocked = true; }
            await new Promise((resolve) => {
                const s = document.createElement('script');
                s.src = 'https://cdn.jsdelivr.net/npm/x/x.js';
                s.onerror = () => { out.foreignBlocked = true; resolve(); };
                s.onload = resolve;
                document.head.appendChild(s);
                setTimeout(resolve, 2000);
            });
            return out;
        });
        expect(res).toEqual({ inlineBlocked: true, foreignBlocked: true, foreignConnectBlocked: true });
    });

    test('mobile and desktop layouts have no horizontal overflow in the authorized application', async ({ page }) => {
        await wire(page);
        await privateApi(page);
        await signIn(page);
        await enrollToAal2(page);
        await expect(page.locator('#app-root')).toBeVisible();
        await page.locator('[data-mode-set="advanced"]').click();
        for (const vp of [{ width: 1440, height: 900 }, { width: 820, height: 1000 }, { width: 390, height: 844 }]) {
            await page.setViewportSize(vp);
            for (const [id] of SECTIONS) {
                await page.locator(`.nav-btn[data-target="${id}"]`).click();
                const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
                expect(overflow, `${vp.width}px ${id}`).toBeLessThanOrEqual(0);
            }
        }
    });
});
