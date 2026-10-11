// Real Chromium + real @supabase/supabase-js@2.117.1 code (bundled locally with esbuild and served in place of the CDN URL)
// against a mocked Supabase Auth origin. This proves browser behaviour (CSP, storage, module loading, DOM wiring);
// it does NOT prove anything about the real Supabase project, real TOTP codes or the production CDN bytes.
import { test, expect } from '@playwright/test';
import { build } from 'esbuild';

const SUPA = 'https://zlggrmsolklhfgijcjnz.supabase.co';
const CDN = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.1/+esm';
const CODE = '123456';
const SECRET = 'JBSWY3DPEHPK3PXP';
const b64u = (o) => Buffer.from(typeof o === 'string' ? o : JSON.stringify(o)).toString('base64url');

let sdkBundle;
test.beforeAll(async () => {
    // stdin re-export so esbuild applies the package's ESM ("import") entry and keeps named exports.
    const out = await build({
        stdin: { contents: "export * from '@supabase/supabase-js';", resolveDir: process.cwd(), loader: 'js' },
        bundle: true, format: 'esm', platform: 'browser', write: false, logLevel: 'silent',
    });
    sdkBundle = out.outputFiles[0].text;
});

function authServer() {
    const s = { factors: [], n: 0, tokens: [] };
    const uid = '11111111-2222-4333-8444-555555555555';
    const user = () => ({ id: uid, aud: 'authenticated', role: 'authenticated', email: 'owner@example.invalid', created_at: '2026-01-01T00:00:00Z', app_metadata: {}, user_metadata: {}, factors: s.factors.map((f) => ({ ...f })) });
    const jwt = (aal) => {
        const now = Math.floor(Date.now() / 1000);
        const amr = aal === 'aal2' ? [{ method: 'totp', timestamp: now }, { method: 'password', timestamp: now - 5 }] : [{ method: 'password', timestamp: now }];
        const t = `${b64u({ alg: 'ES256', typ: 'JWT', kid: 'k' })}.${b64u({ sub: uid, aud: 'authenticated', role: 'authenticated', aal, amr, session_id: '00000000-0000-4000-8000-000000000001', is_anonymous: false, iat: now, exp: now + 3600 })}.${b64u('sig')}`;
        s.tokens.push(t);
        return t;
    };
    const session = (aal) => ({ access_token: jwt(aal), token_type: 'bearer', expires_in: 3600, expires_at: Math.floor(Date.now() / 1000) + 3600, refresh_token: `r-${++s.n}`, user: user() });
    s.handle = (method, path, body) => {
        const p = path.replace('/auth/v1', '');
        if (p === '/token') return body.password === 'pw-correct' ? [200, session('aal1')] : [400, { code: 400, error_code: 'invalid_credentials', msg: 'Invalid login credentials' }];
        if (p === '/user') return [200, user()];
        if (p === '/logout') return [204, null];
        if (p === '/factors' && method === 'POST') {
            const id = `fac-${++s.n}`;
            s.factors.push({ id, friendly_name: body.friendly_name, factor_type: 'totp', status: 'unverified', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' });
            return [200, { id, type: 'totp', friendly_name: body.friendly_name, totp: { qr_code: '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>', secret: SECRET, uri: `otpauth://totp/Sentinax:o?secret=${SECRET}` } }];
        }
        let m = p.match(/^\/factors\/([^/]+)\/challenge$/);
        if (m) return [200, { id: `ch-${m[1]}`, type: 'totp', expires_at: Math.floor(Date.now() / 1000) + 300 }];
        m = p.match(/^\/factors\/([^/]+)\/verify$/);
        if (m) {
            if (body.code !== CODE) return [400, { code: 400, error_code: 'mfa_verification_failed', msg: 'Invalid TOTP code entered' }];
            s.factors.find((f) => f.id === m[1]).status = 'verified';
            return [200, session('aal2')];
        }
        return [404, { code: 404, msg: 'not found' }];
    };
    return s;
}

async function wire(page) {
    const server = authServer();
    const cors = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' };
    await page.route(CDN, (route) => route.fulfill({ status: 200, contentType: 'application/javascript', body: sdkBundle }));
    await page.route(`${SUPA}/**`, async (route) => {
        const req = route.request();
        if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
        const body = req.postData() ? JSON.parse(req.postData()) : {};
        const [status, payload] = server.handle(req.method(), new URL(req.url()).pathname, body);
        return route.fulfill({ status, headers: { ...cors, 'content-type': 'application/json' }, body: payload === null ? '' : JSON.stringify(payload) });
    });
    const logs = [];
    page.on('console', (m) => logs.push(m.text()));
    page.on('pageerror', (e) => logs.push(`pageerror: ${e.message}`));
    await page.addInitScript(() => {
        window.__csp = [];
        document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
    });
    return { server, logs };
}

const durableState = (page) => page.evaluate(async () => ({
    local: localStorage.length,
    session: sessionStorage.length,
    cookie: document.cookie,
    idb: indexedDB.databases ? (await indexedDB.databases()).length : 0,
    csp: window.__csp,
}));

test.describe('owner bootstrap page in a real browser', () => {
    test('sign-in -> TOTP enrolment -> AAL2, with no durable storage, no CSP violations and no secrets in the console', async ({ page }) => {
        const { server, logs } = await wire(page);
        await page.goto('/owner/index.html');
        await expect(page.locator('#view-signin')).toBeVisible();
        await expect(page.locator('#view-unlocked')).toBeHidden();

        await page.fill('#email', 'owner@example.invalid');
        await page.fill('#password', 'pw-correct');
        await page.click('#signin-form button[type=submit]');
        await expect(page.locator('#view-enroll')).toBeVisible();
        await expect(page.locator('#view-unlocked')).toBeHidden();

        await page.click('#btn-enroll');
        await expect(page.locator('#enroll-pending')).toBeVisible();
        expect(await page.getAttribute('#qr', 'src')).toMatch(/^data:image\/svg\+xml/);
        await expect(page.locator('#secret')).toHaveText(SECRET);

        await page.fill('#enroll-code', '000000');
        await page.click('#enroll-form button[type=submit]');
        await expect(page.locator('#error')).toBeVisible();
        await expect(page.locator('#view-unlocked')).toBeHidden();

        await page.fill('#enroll-code', CODE);
        await page.click('#enroll-form button[type=submit]');
        await expect(page.locator('#view-unlocked')).toBeVisible();
        await expect(page.locator('#factor-summary')).toContainText('1');
        await expect(page.locator('#secret')).toHaveText('');
        expect(await page.getAttribute('#qr', 'src')).toBeNull();
        await expect(page.locator('#view-unlocked')).toContainText('sunucu tarafı sahip yetkilendirmesi değildir');

        const state = await durableState(page);
        expect(state).toMatchObject({ local: 0, session: 0, cookie: '', idb: 0 });
        expect(state.csp).toEqual([]);
        const consoleText = logs.join('\n');
        for (const secret of [SECRET, CODE, 'pw-correct', ...server.tokens]) expect(consoleText).not.toContain(secret);
    });

    test('a reload drops the in-memory session: the page returns to sign-in and renders nothing unlocked', async ({ page }) => {
        await wire(page);
        await page.goto('/owner/index.html');
        await page.fill('#email', 'owner@example.invalid');
        await page.fill('#password', 'pw-correct');
        await page.click('#signin-form button[type=submit]');
        await expect(page.locator('#view-enroll')).toBeVisible();
        await page.reload();
        await expect(page.locator('#view-signin')).toBeVisible();
        await expect(page.locator('#view-enroll')).toBeHidden();
        await expect(page.locator('#view-unlocked')).toBeHidden();
    });

    test('wrong password shows only a generic message and never reveals provider text', async ({ page }) => {
        await wire(page);
        await page.goto('/owner/index.html');
        await page.fill('#email', 'owner@example.invalid');
        await page.fill('#password', 'nope');
        await page.click('#signin-form button[type=submit]');
        await expect(page.locator('#error')).toHaveText('Giriş başarısız. Bilgileri kontrol edin.');
        await expect(page.locator('#view-signin')).toBeVisible();
        expect(await page.content()).not.toContain('Invalid login credentials');
    });

    test('the legacy SPA never sees the bootstrap session (same origin, default Supabase storage)', async ({ page, context }) => {
        await wire(page);
        await page.goto('/owner/index.html');
        await page.fill('#email', 'owner@example.invalid');
        await page.fill('#password', 'pw-correct');
        await page.click('#signin-form button[type=submit]');
        await expect(page.locator('#view-enroll')).toBeVisible();
        const keys = await page.evaluate(() => Object.keys(localStorage));
        expect(keys.filter((k) => /^sb-.*-auth-token$/.test(k))).toEqual([]);
        const other = await context.newPage();
        await other.goto('/owner/index.html');
        await expect(other.locator('#view-signin')).toBeVisible(); // a second tab does not share the session
    });
});
