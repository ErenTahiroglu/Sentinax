// Phase 25D1C3: real Chromium against the BUILT owner deployment artifact (dist/) served by scripts/owner_deploy_serve.mjs with the
// headers from deploy/owner-bootstrap/vercel.json. A local static smoke test: it is not a Vercel deployment and proves nothing
// about headers delivered in production. Auth is never contacted (no credentials are entered).
import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:3001';
test.use({ baseURL: BASE });

const REQUIRED = [
    ['/owner', 'text/html'], ['/owner/css/owner.css', 'text/css'], ['/owner/js/owner_app.js', 'javascript'],
    ['/owner/js/client_config.js', 'javascript'], ['/owner/js/owner_public_config.js', 'javascript'],
    ['/owner/js/mfa_flow.js', 'javascript'], ['/owner/vendor/supabase-js.2.117.1.mjs', 'javascript'],
];
const HDR = {
    'cache-control': 'no-store', 'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer',
    'cross-origin-opener-policy': 'same-origin', 'cross-origin-resource-policy': 'same-origin',
};

test.describe('owner deployment artifact (local static HTTP smoke)', () => {
    test('every owner resource is served 200 with the security headers and a strict header CSP', async ({ request }) => {
        for (const [url, type] of REQUIRED) {
            const r = await request.get(url, { maxRedirects: 0 });
            expect(r.status(), url).toBe(200);
            expect(r.headers()['content-type'], url).toContain(type);
            for (const [k, v] of Object.entries(HDR)) expect(r.headers()[k], `${url} ${k}`).toBe(v);
            const csp = r.headers()['content-security-policy'];
            expect(csp, url).toContain("script-src 'self'");
            expect(csp, url).toContain("frame-ancestors 'none'");
            expect(csp, url).not.toMatch(/unsafe-inline|unsafe-eval|\*/);
        }
    });

    test('/owner/ and /owner/index.html canonicalise to /owner', async ({ request }) => {
        for (const u of ['/owner/', '/owner/index.html']) {
            const r = await request.get(u, { maxRedirects: 0 });
            expect(r.status(), u).toBe(308);
            expect(r.headers().location, u).toBe('/owner');
            const f = await request.get(u);
            expect(f.status(), u).toBe(200);
            expect(await f.text(), u).toContain('Sentinax');
        }
    });

    test('root, legacy, API, worker and unknown paths are 404 and never owner HTML', async ({ request }) => {
        for (const u of ['/', '/index.html', '/sw.js', '/manifest.json', '/js/core/config.js', '/frontend/index.html', '/buffett/index.html', '/api/v1/portfolio', '/owner/js/missing.js', '/owner/vendor/supabase-js.manifest.json']) {
            const r = await request.get(u); // follows the cleanUrls redirect (e.g. /index.html -> /) to the final answer
            expect(r.status(), u).toBe(404);
            expect(await r.text(), u).not.toContain('Sentinax Sahip');
            expect(r.headers()['cache-control'], u).toBe('no-store');
            expect(r.headers()['x-content-type-options'], u).toBe('nosniff');
        }
    });

    test('the page boots from the artifact: sign-in view, only same-origin traffic, no CSP violations, no worker, no durable storage', async ({ page }) => {
        const origins = new Set();
        const logs = [];
        page.on('request', (r) => origins.add(new URL(r.url()).origin));
        page.on('console', (m) => logs.push(`${m.type()}: ${m.text()}`));
        page.on('pageerror', (e) => logs.push(`pageerror: ${e.message}`));
        await page.addInitScript(() => {
            window.__csp = [];
            document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
        });
        await page.goto('/owner');
        await expect(page.locator('#view-signin')).toBeVisible();
        const state = await page.evaluate(async () => ({
            csp: window.__csp,
            workers: (await navigator.serviceWorker.getRegistrations()).length,
            local: localStorage.length, session: sessionStorage.length, cookie: document.cookie,
        }));
        expect(state).toEqual({ csp: [], workers: 0, local: 0, session: 0, cookie: '' });
        expect([...origins]).toEqual([BASE]);
        expect(logs.filter((l) => /error|pageerror|refused|violat/i.test(l))).toEqual([]);
    });

    test('the header CSP blocks an injected inline script and a foreign script even if the page were compromised', async ({ page }) => {
        await page.goto('/owner');
        await expect(page.locator('#view-signin')).toBeVisible();
        const res = await page.evaluate(async () => {
            const out = { inlineBlocked: false, foreignBlocked: false };
            const inline = document.createElement('script');
            inline.textContent = 'window.__inlineRan = true';
            document.head.appendChild(inline);
            out.inlineBlocked = window.__inlineRan !== true;
            await new Promise((resolve) => {
                const s = document.createElement('script');
                s.src = 'https://cdn.jsdelivr.net/npm/x/x.js';
                s.onerror = () => { out.foreignBlocked = true; resolve(); };
                s.onload = resolve;
                document.head.appendChild(s);
                setTimeout(resolve, 3000);
            });
            return out;
        });
        expect(res).toEqual({ inlineBlocked: true, foreignBlocked: true });
    });
});
