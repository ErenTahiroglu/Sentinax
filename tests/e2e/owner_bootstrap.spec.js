// Real Chromium + the vendored @supabase/supabase-js@2.117.1 bundle that the page itself ships (no CDN, no interception)
// against a mocked Supabase Auth origin. This proves browser behaviour (CSP, storage, module loading, DOM wiring);
// it does NOT prove anything about the real Supabase project, real TOTP codes or the production CDN bytes.
import { test, expect } from '@playwright/test';

import { SUPA, CODE, SECRET, wire, durableState } from './support/owner_mock_auth.js';

test.describe('owner bootstrap page in a real browser', () => {
    test('sign-in -> TOTP enrolment -> AAL2, with no durable storage, no CSP violations and no secrets in the console', async ({ page }) => {
        const { server, logs, origins } = await wire(page);
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
        // Self-contained: the only origins the page ever talks to are itself and the Supabase project.
        expect([...origins].sort()).toEqual(['http://127.0.0.1:3000', SUPA]);
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

    test('canonical owner URL forms all load the same working page (no relative-asset breakage)', async ({ page }) => {
        await wire(page);
        for (const url of ['/owner/', '/owner/index.html']) {
            await page.goto(url);
            await expect(page.locator('#view-signin')).toBeVisible();
        }
    });

    test('missing or invalid public configuration fails closed: locked page, no Supabase request, no sign-in form', async ({ page }) => {
        const { origins } = await wire(page);
        await page.route('**/owner/js/owner_public_config.js', (route) => route.fulfill({
            status: 200, contentType: 'application/javascript',
            body: "export const OWNER_PUBLIC_CONFIG = Object.freeze({ supabaseUrl: 'https://evil.example', publishableKey: 'sb_secret_xxxxxxxxxxxxxxxxxxxxxxxx' });",
        }));
        await page.goto('/owner/index.html');
        await expect(page.locator('#status')).toHaveText('Oturum durumu doğrulanamadı; erişim kapalı.');
        await expect(page.locator('#view-signin')).toBeHidden();
        expect([...origins]).not.toContain(SUPA);
        expect([...origins].some((o) => o.includes('evil.example'))).toBe(false);
    });
});
