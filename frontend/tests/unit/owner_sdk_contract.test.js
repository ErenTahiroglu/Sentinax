// Real @supabase/supabase-js@2.117.1 against a stateful mock Auth server.
// The server responses are modelled on the SDK's own request/response handling and the GoTrue API
// shapes (token, user, factors, challenge, verify, logout); they are NOT captured from a live project.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createClient } from '@supabase/supabase-js';
import { createOwnerMfaFlow, STATES } from '../../owner/js/mfa_flow.js';
import { OWNER_AUTH_OPTIONS } from '../../owner/js/client_config.js';

const URL_ = 'https://abcdefghijklmnopqrst.supabase.co';
const KEY = 'sb_publishable_synthetic';
const CODE = '123456';
const b64u = (o) => Buffer.from(typeof o === 'string' ? o : JSON.stringify(o)).toString('base64url');

function makeServer() {
  const s = { factors: [], aal: 'aal1', revoked: false, requests: [], sessionId: '00000000-0000-4000-8000-000000000001', n: 0 };
  const user = () => ({ id: '11111111-2222-4333-8444-555555555555', aud: 'authenticated', role: 'authenticated', email: 'owner@example.invalid', created_at: '2026-01-01T00:00:00Z', app_metadata: {}, user_metadata: {}, factors: s.factors.map((f) => ({ ...f })) });
  const token = (aal) => {
    const now = Math.floor(Date.now() / 1000);
    const amr = aal === 'aal2' ? [{ method: 'totp', timestamp: now }, { method: 'password', timestamp: now - 5 }] : [{ method: 'password', timestamp: now }];
    return `${b64u({ alg: 'ES256', typ: 'JWT', kid: 'k' })}.${b64u({ sub: user().id, aud: 'authenticated', role: 'authenticated', aal, amr, session_id: s.sessionId, is_anonymous: false, iat: now, exp: now + 3600 })}.${b64u('sig')}`;
  };
  const session = (aal) => ({ access_token: token(aal), token_type: 'bearer', expires_in: 3600, expires_at: Math.floor(Date.now() / 1000) + 3600, refresh_token: `r-${++s.n}`, user: user() });
  const json = (status, body) => new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  s.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url);
    const method = (init.method || 'GET').toUpperCase();
    const body = init.body ? JSON.parse(init.body) : {};
    s.requests.push({ method, path: url.pathname, query: url.search, body });
    const p = url.pathname.replace('/auth/v1', '');
    if (p === '/token') {
      if (body.password !== 'pw-correct') return json(400, { code: 400, error_code: 'invalid_credentials', msg: 'Invalid login credentials' });
      s.aal = 'aal1';
      return json(200, session('aal1'));
    }
    if (p === '/user') return s.revoked ? json(403, { code: 403, error_code: 'session_not_found', msg: 'Session from session_id claim in JWT does not exist' }) : json(200, user());
    if (p === '/logout') return new Response(null, { status: 204 });
    if (p === '/factors' && method === 'POST') {
      const id = `fac-${++s.n}`;
      if (s.factors.some((f) => f.friendly_name === body.friendly_name)) return json(422, { code: 422, error_code: 'mfa_factor_name_conflict', msg: 'A factor with the friendly name exists' });
      s.factors.push({ id, friendly_name: body.friendly_name, factor_type: 'totp', status: 'unverified', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' });
      return json(200, { id, type: 'totp', friendly_name: body.friendly_name, totp: { qr_code: '<svg xmlns="http://www.w3.org/2000/svg"></svg>', secret: 'JBSWY3DPEHPK3PXP', uri: 'otpauth://totp/Sentinax:o?secret=JBSWY3DPEHPK3PXP' } });
    }
    let m = p.match(/^\/factors\/([^/]+)\/challenge$/);
    if (m) return json(200, { id: `ch-${m[1]}`, type: 'totp', expires_at: Math.floor(Date.now() / 1000) + 300 });
    m = p.match(/^\/factors\/([^/]+)\/verify$/);
    if (m) {
      if (body.code !== CODE) return json(400, { code: 400, error_code: 'mfa_verification_failed', msg: 'Invalid TOTP code entered' });
      s.factors.find((f) => f.id === m[1]).status = 'verified';
      s.aal = 'aal2';
      return json(200, session('aal2'));
    }
    m = p.match(/^\/factors\/([^/]+)$/);
    if (m && method === 'DELETE') { s.factors = s.factors.filter((f) => f.id !== m[1]); return json(200, { id: m[1] }); }
    return json(404, { code: 404, msg: 'not found' });
  };
  return s;
}

const ownerClient = (server) => createClient(URL_, KEY, { global: { fetch: server.fetch }, auth: { ...OWNER_AUTH_OPTIONS } });
const legacyClient = (server) => createClient(URL_, KEY, { global: { fetch: server.fetch } }); // default options, like the legacy SPA

// Node versions with a built-in `localStorage` getter leave it undefined under jsdom; use an explicit Storage double.
function memStorage() {
  const m = new Map();
  return {
    get length() { return m.size; },
    key: (i) => [...m.keys()][i] ?? null,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: vi.fn((k, v) => { m.set(k, String(v)); }),
    removeItem: (k) => { m.delete(k); },
    clear: () => m.clear(),
    keys: () => [...m.keys()],
  };
}

let server;
beforeEach(() => { vi.stubGlobal('localStorage', memStorage()); vi.stubGlobal('sessionStorage', memStorage()); server = makeServer(); });
afterEach(() => { vi.unstubAllGlobals(); });

async function fullEnrollment(flow, name = 'primary') {
  await flow.signIn('owner@example.invalid', 'pw-correct');
  const e = await flow.beginEnrollment(name);
  expect(e.ok).toBe(true);
  return { e, done: await flow.confirmEnrollment(e.factorId, CODE) };
}

describe('real SDK: response structures and state mapping', () => {
  it('password -> enroll -> verify walks NEEDS_ENROLLMENT -> UNLOCKED with the real response shapes', async () => {
    const client = ownerClient(server);
    const flow = createOwnerMfaFlow(client.auth);
    const r = await flow.signIn('owner@example.invalid', 'pw-correct');
    expect(r).toMatchObject({ ok: true, state: STATES.NEEDS_ENROLLMENT });
    const aal1 = await client.auth.mfa.getAuthenticatorAssuranceLevel();
    expect(aal1.data).toMatchObject({ currentLevel: 'aal1', nextLevel: 'aal1' });
    const e = await flow.beginEnrollment('primary');
    expect(e.qrDataUri.startsWith('data:image/svg+xml')).toBe(true);
    expect(e.secret).toBe('JBSWY3DPEHPK3PXP');
    const listed = await client.auth.mfa.listFactors();
    expect(Array.isArray(listed.data.all)).toBe(true);
    expect(Array.isArray(listed.data.totp)).toBe(true);
    expect(listed.data.totp).toHaveLength(0); // an unverified factor is not in the verified bucket
    expect(listed.data.all[0]).toMatchObject({ factor_type: 'totp', status: 'unverified', friendly_name: 'primary' });
    const done = await flow.confirmEnrollment(e.factorId, CODE);
    expect(done).toMatchObject({ ok: true, state: STATES.UNLOCKED, verifiedFactorCount: 1 });
    const aal2 = await client.auth.mfa.getAuthenticatorAssuranceLevel();
    expect(aal2.data.currentLevel).toBe('aal2');
    expect(aal2.data.currentAuthenticationMethods.some((x) => x.method === 'totp')).toBe(true);
  });

  it('wrong code is a provider error mapped to a fixed code; existing factor can be challenged after re-login', async () => {
    const flow = createOwnerMfaFlow(ownerClient(server).auth);
    await fullEnrollment(flow);
    await flow.signOut();
    const second = createOwnerMfaFlow(ownerClient(server).auth);
    expect((await second.signIn('owner@example.invalid', 'pw-correct')).state).toBe(STATES.NEEDS_CHALLENGE);
    const factor = second.snapshot().factors[0].id;
    const bad = await second.challenge(factor, '000000');
    expect(bad).toMatchObject({ ok: false, code: 'CODE_REJECTED', state: STATES.NEEDS_CHALLENGE });
    expect(JSON.stringify(bad)).not.toContain('Invalid TOTP');
    expect(await second.challenge(factor, CODE)).toMatchObject({ ok: true, state: STATES.UNLOCKED });
  });

  it('duplicate factor names are rejected by the server and surface as ENROLL_FAILED', async () => {
    const flow = createOwnerMfaFlow(ownerClient(server).auth);
    await fullEnrollment(flow);
    expect(await flow.beginEnrollment('primary')).toMatchObject({ ok: false, code: 'ENROLL_FAILED' });
  });

  it('signOut scope is forwarded to the Auth server', async () => {
    const flow = createOwnerMfaFlow(ownerClient(server).auth);
    await flow.signIn('owner@example.invalid', 'pw-correct');
    await flow.signOut('local');
    await flow.signIn('owner@example.invalid', 'pw-correct');
    await flow.signOut('global');
    const logouts = server.requests.filter((r) => r.path.endsWith('/logout')).map((r) => r.query);
    expect(logouts).toEqual(['?scope=local', '?scope=global']);
  });
});

describe('real SDK: why client-side AAL alone is not enough', () => {
  it('after the factor is removed server-side the local JWT still says aal2, but the flow fails closed', async () => {
    const client = ownerClient(server);
    const flow = createOwnerMfaFlow(client.auth);
    expect((await fullEnrollment(flow)).done.state).toBe(STATES.UNLOCKED);
    server.factors = [];
    const local = await client.auth.mfa.getAuthenticatorAssuranceLevel();
    expect(local.data.currentLevel).toBe('aal2'); // stale token claim
    expect((await flow.refresh()).state).toBe(STATES.ERROR_LOCKED);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('a revoked server session is detected through listFactors (GET /user), not the local token', async () => {
    const client = ownerClient(server);
    const flow = createOwnerMfaFlow(client.auth);
    expect((await fullEnrollment(flow)).done.state).toBe(STATES.UNLOCKED);
    server.revoked = true;
    const r = await flow.refresh();
    expect(r.state).not.toBe(STATES.UNLOCKED);
    expect(flow.canRenderPrivate()).toBe(false);
  });
});

describe('real SDK: session isolation between the owner page and the legacy client', () => {
  it('the bootstrap configuration writes nothing durable (localStorage, sessionStorage, cookies)', async () => {
    const flow = createOwnerMfaFlow(ownerClient(server).auth);
    expect((await fullEnrollment(flow)).done.state).toBe(STATES.UNLOCKED);
    expect(localStorage.setItem).not.toHaveBeenCalled();
    expect(sessionStorage.setItem).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe('');
  });

  it('a legacy client using default options cannot inherit the bootstrap session', async () => {
    const flow = createOwnerMfaFlow(ownerClient(server).auth);
    expect((await fullEnrollment(flow)).done.state).toBe(STATES.UNLOCKED);
    const legacy = legacyClient(server);
    expect((await legacy.auth.getSession()).data.session).toBeNull();
  });

  it('negative control: with persistSession:true the same flow IS inherited by a default client (why the setting matters)', async () => {
    const persistent = createClient(URL_, KEY, { global: { fetch: server.fetch }, auth: { persistSession: true, autoRefreshToken: false, detectSessionInUrl: false } });
    await persistent.auth.signInWithPassword({ email: 'owner@example.invalid', password: 'pw-correct' });
    expect(localStorage.keys().some((k) => /^sb-.*-auth-token$/.test(k))).toBe(true);
    const inheriting = legacyClient(server);
    expect((await inheriting.auth.getSession()).data.session).not.toBeNull();
  });

  it('the owner page does not adopt a pre-existing stored session either', async () => {
    const persistent = createClient(URL_, KEY, { global: { fetch: server.fetch }, auth: { persistSession: true, autoRefreshToken: false, detectSessionInUrl: false } });
    await persistent.auth.signInWithPassword({ email: 'owner@example.invalid', password: 'pw-correct' });
    const flow = createOwnerMfaFlow(ownerClient(server).auth);
    expect((await flow.refresh()).state).toBe(STATES.SIGNED_OUT);
  });
});

describe('real SDK: onAuthStateChange integration (no deadlock, no reentrancy)', () => {
  it('a synchronous callback that only notifies the flow completes sign-in, enrolment and sign-out', async () => {
    const client = ownerClient(server);
    const flow = createOwnerMfaFlow(client.auth);
    const events = [];
    client.auth.onAuthStateChange((event) => { events.push(event); flow.notifyAuthEvent(event); });
    const { e } = await fullEnrollment(flow);
    expect(e.ok).toBe(true);
    await vi.waitFor(() => expect(flow.snapshot().state).toBe(STATES.UNLOCKED));
    expect(events).toEqual(expect.arrayContaining(['SIGNED_IN', 'MFA_CHALLENGE_VERIFIED']));
    await flow.signOut();
    await vi.waitFor(() => expect(events).toContain('SIGNED_OUT'));
    expect(flow.snapshot().state).toBe(STATES.SIGNED_OUT);
  });
});
