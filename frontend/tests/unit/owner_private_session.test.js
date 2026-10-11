// @vitest-environment node
// Phase 25D1E3: lifecycle of the private application inside the owner document. The MFA state machine (createOwnerMfaFlow) stays the only AAL logic;
// this controller adds the SERVER authorization step (GET /private/portfolio/summary) and guarantees that nothing private survives a session change.
// All fixtures are synthetic: fake auth surface, fake fetch, fake timers. No network, no real token.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import path from 'path';
import { createOwnerMfaFlow, STATES } from '../../owner/js/mfa_flow.js';
import {
  AUTHZ, createPrivateSession, resolvePrivateApi, validateSummary, sessionKeyOf, PRIVATE_SUMMARY_PATH,
} from '../../owner/js/private_session.js';
import { PRIVATE_API_CONFIG } from '../../owner/js/private_api_config.js';

const repo = path.resolve(__dirname, '../../..');
const FIXTURE = JSON.parse(fs.readFileSync(path.join(repo, 'tests/fixtures/private_portfolio_summary_unavailable.json'), 'utf8'));
const API = { url: PRIVATE_SUMMARY_PATH };

const b64u = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
const token = (sessionId, extra = '') => `${b64u({ alg: 'ES256' })}.${b64u({ sub: 'u', session_id: sessionId })}.sig${extra}`;
const sess = (userId = 'user-a', sessionId = 'sid-1', extra = '') => ({ access_token: token(sessionId, extra), user: { id: userId } });

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); };
const jsonRes = (status, body, type = 'application/json') => ({ status, ok: status >= 200 && status < 300, headers: { get: (h) => (h.toLowerCase() === 'content-type' ? type : null) }, json: async () => { if (body === undefined) throw new Error('bad json'); return JSON.parse(JSON.stringify(body)); } });
const view = (state) => ({ state, verifiedFactorCount: 1, backupFactorReady: false, factors: [] });

function harness(over = {}) {
  let t = 1_000_000;
  const h = {
    clock: { now: () => t, advance: (ms) => { t += ms; } },
    canRender: true,
    session: sess(),
    calls: [],
    fetches: [],
  };
  h.flow = { canRenderPrivate: () => h.canRender };
  h.getSession = vi.fn(async () => h.session);
  h.fetchImpl = vi.fn((url, init) => {
    const d = deferred();
    h.fetches.push({ url, init, ...d });
    init.signal?.addEventListener?.('abort', () => d.reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    return d.promise;
  });
  h.ps = createPrivateSession({ api: API, flow: h.flow, getSession: h.getSession, fetchImpl: h.fetchImpl, now: h.clock.now, timeoutMs: 10_000, revalidateMs: 60_000, retryMs: 15_000, ...over });
  h.states = [];
  h.ps.onChange((s) => h.states.push({ status: s.status, summary: s.summary }));
  return h;
}
const unlock = async (h) => { h.ps.onFlowChange(view(STATES.UNLOCKED)); await flush(); };
const answer = async (h, i, res) => { h.fetches[i].resolve(res); await flush(); };

beforeEach(() => { vi.useRealTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('private API configuration fails closed', () => {
  it('only the same-origin mode with the fixed read-only summary path resolves', () => {
    expect(PRIVATE_SUMMARY_PATH).toBe('/private/portfolio/summary');
    expect(Object.isFrozen(PRIVATE_API_CONFIG)).toBe(true);
    expect(resolvePrivateApi(PRIVATE_API_CONFIG)).toEqual({ url: '/private/portfolio/summary' });
    for (const bad of [undefined, null, {}, [], 'same-origin', 42, { mode: 'cross-origin' }, { mode: 'same-origin', extra: 1 }, { mode: 'same-origin', url: 'https://evil.example/x' },
      { baseUrl: 'https://api.example' }, { apiOrigin: 'https://api.example' }, { mode: 'Same-Origin' }]) {
      expect(resolvePrivateApi(bad), JSON.stringify(bad)).toBeNull();
    }
  });
  it('without a valid configuration an unlocked session is UNCONFIGURED and nothing is requested', async () => {
    const h = harness({ api: null });
    await unlock(h);
    expect(h.ps.snapshot()).toMatchObject({ status: AUTHZ.UNCONFIGURED, summary: null });
    expect(h.fetchImpl).not.toHaveBeenCalled();
    expect(h.getSession).not.toHaveBeenCalled();
  });
});

describe('the summary envelope is validated strictly (UNAVAILABLE is the only acceptable data status in this phase)', () => {
  it('accepts exactly the real contract fixture and normalises it', () => {
    const v = validateSummary(FIXTURE);
    expect(v).toMatchObject({ contractVersion: 'private_portfolio_summary.v1', dataStatus: 'UNAVAILABLE', reasonCode: 'NO_QUALIFIED_SOURCE_CONNECTED' });
    expect(v.warnings.length).toBeGreaterThan(0);
    expect(Object.isFrozen(v)).toBe(true);
  });
  it.each([
    ['EMPTY status', (b) => { b.data_status = 'EMPTY'; b.source = 'ledger'; b.as_of = '2026-01-01T00:00:00+00:00'; }],
    ['AVAILABLE status', (b) => { b.data_status = 'AVAILABLE'; b.source = 'ledger'; b.as_of = '2026-01-01T00:00:00+00:00'; }],
    ['unknown status', (b) => { b.data_status = 'OK'; }],
    ['extra key', (b) => { b.portfolio_value = 100; }],
    ['missing key', (b) => { delete b.warnings; }],
    ['wrong version', (b) => { b.contract_version = 'private_portfolio_summary.v2'; }],
    ['source set', (b) => { b.source = 'x'; }],
    ['as_of set', (b) => { b.as_of = '2026-01-01T00:00:00+00:00'; }],
    ['positions present', (b) => { b.positions = [{ x: 1 }]; }],
    ['portfolios present', (b) => { b.portfolios = [{ x: 1 }]; }],
    ['positions not an array', (b) => { b.positions = {}; }],
    ['warnings not strings', (b) => { b.warnings = [1]; }],
    ['too many warnings', (b) => { b.warnings = Array(50).fill('w'); }],
    ['bad generated_at', (b) => { b.generated_at = 'yesterday'; }],
    ['non-string reason', (b) => { b.reason = 5; }],
    ['lowercase reason code', (b) => { b.reason_code = 'ok; drop'; }],
  ])('rejects %s', (_n, mutate) => {
    const b = JSON.parse(JSON.stringify(FIXTURE));
    mutate(b);
    expect(validateSummary(b)).toBeNull();
  });
  it.each([null, undefined, 'x', 3, [], [FIXTURE], true])('rejects non-object input %#', (v) => { expect(validateSummary(v)).toBeNull(); });
});

describe('gate: nothing is requested or shown unless the MFA flow is UNLOCKED', () => {
  it.each([STATES.SIGNED_OUT, STATES.NEEDS_ENROLLMENT, STATES.NEEDS_CHALLENGE, STATES.ERROR_LOCKED])('%s never reaches the server', async (state) => {
    const h = harness();
    h.ps.onFlowChange(view(state));
    await flush();
    expect(h.fetchImpl).not.toHaveBeenCalled();
    expect(h.ps.snapshot()).toMatchObject({ status: AUTHZ.IDLE, summary: null });
  });
});

describe('the authenticated request', () => {
  it('is a credential-less, uncached, same-origin GET with the access token only in the Authorization header', async () => {
    const h = harness();
    await unlock(h);
    expect(h.fetchImpl).toHaveBeenCalledTimes(1);
    const { url, init } = h.fetches[0];
    expect(url).toBe('/private/portfolio/summary');
    expect(url).not.toMatch(/token|bearer|eyJ|\?|#/i);
    expect(init).toMatchObject({ method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', mode: 'same-origin' });
    expect(init.body).toBeUndefined();
    expect(Object.keys(init.headers).sort()).toEqual(['Accept', 'Authorization']);
    expect(init.headers.Authorization).toBe(`Bearer ${h.session.access_token}`);
    expect(init.signal).toBeDefined();
    expect(JSON.stringify(h.ps.snapshot())).not.toContain(h.session.access_token);
  });

  it('a valid UNAVAILABLE answer authorizes the private view and carries the real envelope fields', async () => {
    const h = harness();
    await unlock(h);
    expect(h.ps.snapshot().status).toBe(AUTHZ.AUTHORIZING);
    await answer(h, 0, jsonRes(200, FIXTURE));
    const s = h.ps.snapshot();
    expect(s.status).toBe(AUTHZ.AUTHORIZED);
    expect(s.summary).toMatchObject({ dataStatus: 'UNAVAILABLE', reasonCode: 'NO_QUALIFIED_SOURCE_CONNECTED' });
  });

  it.each([
    [401, AUTHZ.DENIED], [403, AUTHZ.DENIED], [503, AUTHZ.SERVER_UNAVAILABLE], [500, AUTHZ.ERROR], [404, AUTHZ.ERROR], [302, AUTHZ.ERROR], [204, AUTHZ.ERROR],
  ])('HTTP %i shows the locked state %s and no private view', async (status, expected) => {
    const h = harness();
    await unlock(h);
    await answer(h, 0, jsonRes(status, { error: true, message: 'detail-that-must-not-be-shown', token: 'x' }));
    expect(h.ps.snapshot()).toEqual({ status: expected, summary: null });
    expect(JSON.stringify(h.states)).not.toContain('detail-that-must-not-be-shown');
    expect(h.states.some((s) => s.status === AUTHZ.AUTHORIZED)).toBe(false);
  });

  it('a non-owner who reaches client-side AAL2 is rejected by the server and never sees the private view', async () => {
    const h = harness();
    h.session = sess('someone-else');
    await unlock(h);
    await answer(h, 0, jsonRes(403, { error: true, message: 'forbidden' }));
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.DENIED, summary: null });
  });

  it('network failure, abort and timeout fail closed as UNREACHABLE', async () => {
    const h = harness();
    await unlock(h);
    h.fetches[0].reject(new TypeError('Failed to fetch'));
    await flush();
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.UNREACHABLE, summary: null });

    vi.useFakeTimers();
    const g = harness();
    g.ps.onFlowChange(view(STATES.UNLOCKED));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(10_001);
    expect(g.ps.snapshot()).toEqual({ status: AUTHZ.UNREACHABLE, summary: null });
    expect(g.fetches[0].init.signal.aborted).toBe(true);
  });

  it.each([
    ['non-JSON content type', () => jsonRes(200, FIXTURE, 'text/html; charset=utf-8')],
    ['unparsable body', () => jsonRes(200, undefined)],
    ['EMPTY claimed by the server', () => jsonRes(200, { ...FIXTURE, data_status: 'EMPTY', source: 'ledger', as_of: '2026-01-01T00:00:00+00:00' })],
    ['extra financial field', () => jsonRes(200, { ...FIXTURE, portfolio_value: 123 })],
    ['SPA fallback HTML', () => jsonRes(200, { html: '<!doctype html>' }, 'text/html')],
  ])('malformed or unexpected 200 (%s) is MALFORMED, never evidence and never EMPTY', async (_n, make) => {
    const h = harness();
    await unlock(h);
    await answer(h, 0, make());
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.MALFORMED, summary: null });
  });

  it('a session that is missing or unreadable while the flow says UNLOCKED fails closed without a request', async () => {
    for (const bad of [null, {}, { access_token: 5, user: { id: 'x' } }, { access_token: 'a.b.c', user: {} }]) {
      const h = harness();
      h.session = bad;
      await unlock(h);
      expect(h.fetchImpl).not.toHaveBeenCalled();
      expect(h.ps.snapshot()).toEqual({ status: AUTHZ.ERROR, summary: null });
    }
    const h = harness();
    h.getSession.mockRejectedValue(new Error('boom'));
    await unlock(h);
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.ERROR, summary: null });
  });
});

describe('invalidation and stale results (no private response may outlive or cross a session)', () => {
  it('sign-out while the request is pending clears at once and the late success is ignored', async () => {
    const h = harness();
    await unlock(h);
    h.ps.onFlowChange(view(STATES.SIGNED_OUT));                          // synchronous: before any SDK promise settles
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.IDLE, summary: null });
    expect(h.fetches[0].init.signal.aborted).toBe(true);
    await answer(h, 0, jsonRes(200, FIXTURE));                           // arrives late (a server that ignored the abort)
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.IDLE, summary: null });
    expect(h.states.some((s) => s.status === AUTHZ.AUTHORIZED)).toBe(false);
  });

  it.each([STATES.NEEDS_CHALLENGE, STATES.NEEDS_ENROLLMENT, STATES.ERROR_LOCKED])('MFA downgrade / lock (%s) while pending hides everything and suppresses the late success', async (state) => {
    const h = harness();
    await unlock(h);
    h.ps.onFlowChange(view(state));
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.IDLE, summary: null });
    await answer(h, 0, jsonRes(200, FIXTURE));
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.IDLE, summary: null });
  });

  it('an authorized view is hidden synchronously on sign-out, downgrade and lock', async () => {
    for (const state of [STATES.SIGNED_OUT, STATES.NEEDS_CHALLENGE, STATES.ERROR_LOCKED]) {
      const h = harness();
      await unlock(h);
      await answer(h, 0, jsonRes(200, FIXTURE));
      expect(h.ps.snapshot().status).toBe(AUTHZ.AUTHORIZED);
      h.ps.onFlowChange(view(state));
      expect(h.ps.snapshot()).toEqual({ status: AUTHZ.IDLE, summary: null });
    }
  });

  it('the flow freshness window is part of the gate: a stale UNLOCKED cannot authorize', async () => {
    const h = harness();
    await unlock(h);
    h.canRender = false;                                                  // flow.canRenderPrivate() turned false before the answer arrived
    await answer(h, 0, jsonRes(200, FIXTURE));
    expect(h.ps.snapshot().status).not.toBe(AUTHZ.AUTHORIZED);
    expect(h.ps.snapshot().summary).toBeNull();
  });

  it('a different account signing in after the first request started never receives the earlier response', async () => {
    const h = harness();
    await unlock(h);                                                      // request A pending
    h.ps.onFlowChange(view(STATES.SIGNED_OUT));
    h.session = sess('user-b', 'sid-b');
    await unlock(h);                                                      // request B
    expect(h.fetches).toHaveLength(2);
    await answer(h, 0, jsonRes(200, { ...FIXTURE, reason: 'RESPONSE-FOR-A' }));
    expect(h.ps.snapshot().summary).toBeNull();
    await answer(h, 1, jsonRes(200, { ...FIXTURE, reason: 'RESPONSE-FOR-B' }));
    expect(h.ps.snapshot().summary.reason).toBe('RESPONSE-FOR-B');
    expect(JSON.stringify(h.states)).not.toContain('RESPONSE-FOR-A');
  });

  it('an identity change that the flow does not announce is caught when the response arrives', async () => {
    const h = harness();
    await unlock(h);
    h.session = sess('user-b', 'sid-b');                                  // the SDK now holds another session, no SIGNED_OUT in between
    await answer(h, 0, jsonRes(200, { ...FIXTURE, reason: 'RESPONSE-FOR-A' }));
    expect(JSON.stringify(h.states)).not.toContain('RESPONSE-FOR-A');
    expect(h.fetches).toHaveLength(2);                                    // re-authorized for the new identity
    expect(h.fetches[1].init.headers.Authorization).toBe(`Bearer ${h.session.access_token}`);
  });

  it('a token refresh of the SAME session during the request is accepted (same identity, same session id)', async () => {
    const h = harness();
    await unlock(h);
    h.session = sess('user-a', 'sid-1', '-refreshed');
    await answer(h, 0, jsonRes(200, FIXTURE));
    expect(h.ps.snapshot().status).toBe(AUTHZ.AUTHORIZED);
    expect(h.fetches).toHaveLength(1);
  });

  it('a switch to another account while already authorized clears the view before the new account is authorized', async () => {
    const h = harness();
    await unlock(h);
    await answer(h, 0, jsonRes(200, { ...FIXTURE, reason: 'RESPONSE-FOR-A' }));
    h.clock.advance(61_000);
    h.session = sess('user-b', 'sid-b');
    await unlock(h);
    expect(h.ps.snapshot().summary).toBeNull();
    expect(h.ps.snapshot().status).toBe(AUTHZ.AUTHORIZING);
    await answer(h, 1, jsonRes(200, { ...FIXTURE, reason: 'RESPONSE-FOR-B' }));
    expect(h.ps.snapshot().summary.reason).toBe('RESPONSE-FOR-B');
  });

  it('repeated AUTH-driven UNLOCKED publications coalesce into one request', async () => {
    const h = harness();
    for (let i = 0; i < 6; i++) h.ps.onFlowChange(view(STATES.UNLOCKED));
    await flush();
    expect(h.fetchImpl).toHaveBeenCalledTimes(1);
    await answer(h, 0, jsonRes(200, FIXTURE));
    for (let i = 0; i < 6; i++) h.ps.onFlowChange(view(STATES.UNLOCKED));
    await flush();
    expect(h.fetchImpl).toHaveBeenCalledTimes(1);                         // still fresh: no revalidation inside the window
  });

  it('simultaneous refresh and sign-out: the interleaving sign-out -> unlock leaves only the second request alive', async () => {
    const h = harness();
    h.ps.onFlowChange(view(STATES.UNLOCKED));
    h.ps.onFlowChange(view(STATES.SIGNED_OUT));
    h.ps.onFlowChange(view(STATES.UNLOCKED));
    await flush();
    const live = h.fetches.filter((f) => !f.init.signal.aborted);
    expect(live).toHaveLength(1);
    for (const f of h.fetches) f.resolve(jsonRes(200, { ...FIXTURE, reason: 'R' + h.fetches.indexOf(f) }));
    await flush();
    expect(h.ps.snapshot().status).toBe(AUTHZ.AUTHORIZED);
    expect(h.ps.snapshot().summary.reason).toBe('R' + (h.fetches.length - 1));
  });

  it('listeners never observe private data from an earlier session once a new one starts', async () => {
    const h = harness();
    await unlock(h);
    await answer(h, 0, jsonRes(200, { ...FIXTURE, reason: 'SESSION-ONE-DATA' }));
    h.ps.onFlowChange(view(STATES.SIGNED_OUT));
    h.session = sess('user-a', 'sid-2');
    await unlock(h);
    const afterSignOut = h.states.slice(h.states.findIndex((s) => s.status === AUTHZ.IDLE && h.states.indexOf(s) > 0));
    expect(JSON.stringify(afterSignOut)).not.toContain('SESSION-ONE-DATA');
  });
});

describe('revalidation keeps the server authoritative over time', () => {
  it('revalidates after the window and hides the view if the server no longer authorizes', async () => {
    const h = harness();
    await unlock(h);
    await answer(h, 0, jsonRes(200, FIXTURE));
    h.clock.advance(61_000);
    await unlock(h);
    expect(h.fetchImpl).toHaveBeenCalledTimes(2);
    expect(h.ps.snapshot().status).toBe(AUTHZ.AUTHORIZED);                // keeps showing while the check runs
    await answer(h, 1, jsonRes(403, { error: true }));
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.DENIED, summary: null });
  });

  it('a network failure during revalidation also hides the private view (fail closed)', async () => {
    const h = harness();
    await unlock(h);
    await answer(h, 0, jsonRes(200, FIXTURE));
    h.clock.advance(61_000);
    await unlock(h);
    h.fetches[1].reject(new TypeError('offline'));
    await flush();
    expect(h.ps.snapshot()).toEqual({ status: AUTHZ.UNREACHABLE, summary: null });
  });

  it('failed attempts are retried only after the retry interval (no request storm)', async () => {
    const h = harness();
    await unlock(h);
    await answer(h, 0, jsonRes(403, {}));
    await unlock(h);
    expect(h.fetchImpl).toHaveBeenCalledTimes(1);
    h.clock.advance(15_001);
    await unlock(h);
    expect(h.fetchImpl).toHaveBeenCalledTimes(2);
  });
});

describe('session identity key (change detection only, never an authorization decision)', () => {
  it('combines the user id and the session id claim and tolerates garbage', () => {
    expect(sessionKeyOf(sess('u1', 's1'))).toBe('u1:s1');
    expect(sessionKeyOf(sess('u1', 's1', '-refreshed'))).toBe('u1:s1');
    expect(sessionKeyOf({ access_token: 'not-a-jwt', user: { id: 'u1' } })).toBe('u1:');
    for (const bad of [null, undefined, {}, { access_token: 'x' }, { user: { id: 'u' } }]) expect(sessionKeyOf(bad)).toBeNull();
  });
});

describe('integration with the real MFA state machine', () => {
  function fakeAuth() {
    const a = {
      session: { access_token: token('sid-1'), user: { id: 'user-a' } },
      level: 'aal2',
      signOutGate: deferred(),
    };
    a.api = {
      getSession: async () => ({ data: { session: a.session }, error: null }),
      signOut: vi.fn(() => a.signOutGate.promise),
      mfa: {
        getAuthenticatorAssuranceLevel: async () => ({ data: { currentLevel: a.level, nextLevel: 'aal2' }, error: null }),
        listFactors: async () => ({ data: { all: [{ id: 'f1', factor_type: 'totp', status: 'verified', friendly_name: 'k' }] }, error: null }),
      },
    };
    return a;
  }

  it('UNLOCKED -> server authorization -> view; sign-out clears the view BEFORE the SDK sign-out settles', async () => {
    const auth = fakeAuth();
    const flow = createOwnerMfaFlow(auth.api);
    const fetches = [];
    const ps = createPrivateSession({
      api: API, flow, getSession: async () => auth.session, now: () => Date.now(),
      fetchImpl: (url, init) => { const d = deferred(); fetches.push(d); return d.promise; },
    });
    flow.onChange((v) => ps.onFlowChange(v));
    await flow.refresh();
    await flush();
    expect(flow.snapshot().state).toBe(STATES.UNLOCKED);
    expect(fetches).toHaveLength(1);
    fetches[0].resolve(jsonRes(200, FIXTURE));
    await flush();
    expect(ps.snapshot().status).toBe(AUTHZ.AUTHORIZED);

    const out = flow.signOut('local');                                      // SDK promise is still pending here
    expect(ps.snapshot()).toEqual({ status: AUTHZ.IDLE, summary: null });
    auth.signOutGate.resolve({ error: null });
    await out;
    expect(ps.snapshot()).toEqual({ status: AUTHZ.IDLE, summary: null });
  });

  it('an AAL1 session (password only) is never sent to the server at all', async () => {
    const auth = fakeAuth();
    auth.level = 'aal1';
    const flow = createOwnerMfaFlow(auth.api);
    const fetchImpl = vi.fn();
    const ps = createPrivateSession({ api: API, flow, getSession: async () => auth.session, fetchImpl });
    flow.onChange((v) => ps.onFlowChange(v));
    await flow.refresh();
    await flush();
    expect(flow.snapshot().state).toBe(STATES.NEEDS_CHALLENGE);
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(ps.snapshot().status).toBe(AUTHZ.IDLE);
  });
});
