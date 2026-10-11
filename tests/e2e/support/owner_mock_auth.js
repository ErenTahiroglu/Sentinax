// Shared SYNTHETIC test helpers: the real bundled Supabase SDK runs in Chromium against a mocked Supabase Auth origin. Not production code.
export const OWNER_UID = '11111111-2222-4333-8444-555555555555'; // synthetic test identity
export const OTHER_UID = '99999999-8888-4777-8666-555555555555';
export const SUPA = 'https://zlggrmsolklhfgijcjnz.supabase.co';
export const CODE = '123456';
export const SECRET = 'JBSWY3DPEHPK3PXP';
const b64u = (o) => Buffer.from(typeof o === 'string' ? o : JSON.stringify(o)).toString('base64url');

export function authServer({ uid = OWNER_UID, verifiedFactor = false } = {}) {
    const s = { factors: verifiedFactor ? [{ id: 'fac-pre', friendly_name: 'birincil', factor_type: 'totp', status: 'verified', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' }] : [], n: 0, tokens: [] };
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

export async function wire(page, opts) {
    const server = authServer(opts);
    const cors = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' };
    await page.route(`${SUPA}/**`, async (route) => {
        const req = route.request();
        if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
        const body = req.postData() ? JSON.parse(req.postData()) : {};
        const [status, payload] = server.handle(req.method(), new URL(req.url()).pathname, body);
        return route.fulfill({ status, headers: { ...cors, 'content-type': 'application/json' }, body: payload === null ? '' : JSON.stringify(payload) });
    });
    const logs = [];
    const origins = new Set();
    page.on('request', (r) => origins.add(new URL(r.url()).origin));
    page.on('console', (m) => logs.push(m.text()));
    page.on('pageerror', (e) => logs.push(`pageerror: ${e.message}`));
    await page.addInitScript(() => {
        window.__csp = [];
        document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
    });
    return { server, logs, origins };
}

export const durableState = (page) => page.evaluate(async () => ({
    local: localStorage.length,
    session: sessionStorage.length,
    cookie: document.cookie,
    idb: indexedDB.databases ? (await indexedDB.databases()).length : 0,
    csp: window.__csp,
}));

