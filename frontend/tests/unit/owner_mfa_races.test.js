import { describe, it, expect, afterEach, vi } from 'vitest';
import { createOwnerMfaFlow, STATES } from '../../owner/js/mfa_flow.js';

const GOOD = '123456';

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

/**
 * Fake of the supabase-js auth surface with per-call gates. A response is computed when the call
 * is made and delivered when the gate opens (a slow network); `signInWithPassword` and `verify`
 * establish the session only on delivery, like the real SDK's _saveSession.
 */
function gatedAuth({ factors = [], signedIn = true, aal = 'aal1' } = {}) {
  const s = { session: signedIn ? { aal } : null, factors: factors.map((f) => ({ factor_type: 'totp', ...f })), calls: [], holds: {}, n: 0, inCallback: false };
  const hold = (name) => { const d = deferred(); (s.holds[name] ||= []).push(d); return d; };
  const gate = async (name) => { const q = s.holds[name]; if (q && q.length) await q.shift().promise; };
  const rec = (name, arg) => { if (s.inCallback) throw new Error(`reentrant SDK call ${name} inside onAuthStateChange`); s.calls.push([name, arg]); };
  const verified = () => s.factors.filter((f) => f.status === 'verified');
  const auth = {
    _s: s,
    hold,
    async signInWithPassword({ password }) {
      rec('signInWithPassword');
      await gate('signInWithPassword');
      if (password !== 'pw') return { data: { session: null }, error: { message: 'bad' } };
      s.session = { aal: 'aal1' };
      return { data: { session: s.session }, error: null };
    },
    async getSession() { rec('getSession'); const r = { data: { session: s.session }, error: null }; await gate('getSession'); return r; },
    async signOut(opts) { rec('signOut', opts); await gate('signOut'); s.session = null; return { error: null }; },
    mfa: {
      async getAuthenticatorAssuranceLevel() {
        rec('aal');
        const r = { data: { currentLevel: s.session ? s.session.aal : null, nextLevel: verified().length ? 'aal2' : 'aal1' }, error: null };
        await gate('aal');
        return r;
      },
      async listFactors() {
        rec('listFactors');
        if (s.listThrows) throw new Error('network');
        const r = { data: { all: s.factors.map((f) => ({ ...f })), totp: verified().map((f) => ({ ...f })) }, error: null };
        await gate('listFactors');
        return r;
      },
      async enroll({ friendlyName }) {
        rec('enroll', { friendlyName });
        const id = `f${++s.n}`;
        s.factors.push({ id, factor_type: 'totp', status: 'unverified', friendly_name: friendlyName });
        const r = { data: { id, type: 'totp', totp: { qr_code: 'data:image/svg+xml;utf-8,<svg/>', secret: 'SECRETSECRET', uri: 'otpauth://x' } }, error: null };
        await gate('enroll');
        return r;
      },
      async challenge({ factorId }) { rec('challenge', { factorId }); await gate('challenge'); return { data: { id: `ch-${factorId}` }, error: null }; },
      async verify({ factorId, code }) {
        rec('verify', { factorId });
        await gate('verify');
        if (!s.session) return { data: null, error: { message: 'session missing' } };
        if (code !== GOOD) return { data: null, error: { message: 'bad code' } };
        s.factors.find((f) => f.id === factorId).status = 'verified';
        s.session = { aal: 'aal2' };
        return { data: {}, error: null };
      },
      async unenroll({ factorId }) { rec('unenroll', { factorId }); s.factors = s.factors.filter((f) => f.id !== factorId); return { data: {}, error: null }; },
    },
  };
  return auth;
}

const A = { id: 'a', status: 'verified', friendly_name: 'one' };
const count = (auth, name) => auth._s.calls.filter((c) => c[0] === name).length;
afterEach(() => { vi.useRealTimers(); });

describe('refresh ordering: an older async result never overrides a newer state', () => {
  it('logout during an in-flight refresh wins; the late refresh is discarded', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    const flow = createOwnerMfaFlow(a);
    const h = a.hold('getSession');
    const inflight = flow.refresh();
    await flow.signOut();
    h.resolve();
    const late = await inflight;
    expect(late.state).toBe(STATES.SIGNED_OUT);
    expect(flow.snapshot().state).toBe(STATES.SIGNED_OUT);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('a stale refresh that already read an aal2 session cannot unlock after logout (mid-pipeline)', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    const flow = createOwnerMfaFlow(a);
    const h = a.hold('listFactors');
    const inflight = flow.refresh();
    await vi.waitFor(() => expect(count(a, 'listFactors')).toBe(1));
    await flow.signOut();
    h.resolve();
    expect((await inflight).state).toBe(STATES.SIGNED_OUT);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('concurrent refresh and MFA verification: the newest result wins', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal1' });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    expect(flow.snapshot().state).toBe(STATES.NEEDS_CHALLENGE);
    const h = a.hold('listFactors');
    const stale = flow.refresh(); // reads the pre-verification (aal1) world, delivered late
    await vi.waitFor(() => expect(count(a, 'listFactors')).toBe(2));
    const r = await flow.challenge('a', GOOD);
    expect(r.state).toBe(STATES.UNLOCKED);
    h.resolve();
    expect((await stale).state).toBe(STATES.UNLOCKED);
    expect(flow.snapshot().state).toBe(STATES.UNLOCKED);
  });

  it('a newer failed refresh locks even if an older one succeeded', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    const flow = createOwnerMfaFlow(a);
    const h = a.hold('getSession');
    const older = flow.refresh();
    a._s.listThrows = true;
    const newer = flow.refresh();
    expect((await newer).state).toBe(STATES.ERROR_LOCKED);
    h.resolve();
    await older;
    expect(flow.snapshot().state).toBe(STATES.ERROR_LOCKED);
    expect(flow.canRenderPrivate()).toBe(false);
  });
});

describe('pending challenge versus session changes', () => {
  it('session expires while a challenge is pending: rejected and state reflects SIGNED_OUT', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal1' });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    const h = a.hold('verify');
    const p = flow.challenge('a', GOOD);
    await vi.waitFor(() => expect(count(a, 'verify')).toBe(1));
    a._s.session = null; // expiry / revocation elsewhere
    h.resolve();
    const r = await p;
    expect(r.ok).toBe(false);
    expect(r.state).toBe(STATES.SIGNED_OUT);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('logout while verification is in flight: late success is SUPERSEDED, never UNLOCKED, session re-closed', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal1' });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    const h = a.hold('verify');
    const p = flow.challenge('a', GOOD);
    await vi.waitFor(() => expect(count(a, 'verify')).toBe(1));
    await flow.signOut();
    a._s.session = { aal: 'aal1' }; // the SDK may still have a session at the moment verify is delivered
    h.resolve();
    const r = await p;
    expect(r).toMatchObject({ ok: false, code: 'SUPERSEDED', state: STATES.SIGNED_OUT });
    expect(flow.canRenderPrivate()).toBe(false);
    expect(a._s.session).toBeNull();
    expect(a._s.calls.filter((c) => c[0] === 'signOut').length).toBeGreaterThanOrEqual(2);
  });

  it('aal downgrade during a challenge (factor removed elsewhere) cannot unlock', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal1' });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    const h = a.hold('verify');
    const p = flow.challenge('a', '000000');
    await vi.waitFor(() => expect(count(a, 'verify')).toBe(1));
    a._s.factors = [];
    h.resolve();
    const r = await p;
    expect(r.ok).toBe(false);
    expect(r.state).toBe(STATES.NEEDS_ENROLLMENT);
    expect(flow.canRenderPrivate()).toBe(false);
  });
});

describe('factor removal and failing / slow SDK calls', () => {
  it('factor removed during refresh: aal2 token with zero verified factors fails closed', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    const flow = createOwnerMfaFlow(a);
    expect((await flow.refresh()).state).toBe(STATES.UNLOCKED);
    a._s.factors = [];
    expect((await flow.refresh()).state).toBe(STATES.ERROR_LOCKED);
  });

  it('a rejected SDK call locks', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    a._s.listThrows = true;
    expect((await createOwnerMfaFlow(a).refresh()).state).toBe(STATES.ERROR_LOCKED);
  });

  it('a never-resolving SDK call times out into ERROR_LOCKED and its late reply is ignored', async () => {
    vi.useFakeTimers();
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    const flow = createOwnerMfaFlow(a, { timeoutMs: 1000 });
    const h = a.hold('listFactors');
    const p = flow.refresh();
    await vi.advanceTimersByTimeAsync(1001);
    expect((await p).state).toBe(STATES.ERROR_LOCKED);
    h.resolve();
    await vi.advanceTimersByTimeAsync(10);
    expect(flow.snapshot().state).toBe(STATES.ERROR_LOCKED);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('UNLOCKED is not trusted forever: the freshness window expires', async () => {
    let t = 1000;
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    const flow = createOwnerMfaFlow(a, { now: () => t });
    await flow.refresh();
    expect(flow.canRenderPrivate()).toBe(true);
    t += 61000;
    expect(flow.canRenderPrivate()).toBe(false);
  });
});

describe('overlapping sign-in, sign-out and duplicate submits', () => {
  it('sign-out during an in-flight sign-in supersedes it and closes the session it creates', async () => {
    const a = gatedAuth({ signedIn: false });
    const flow = createOwnerMfaFlow(a);
    const h = a.hold('signInWithPassword');
    const p = flow.signIn('o@x.invalid', 'pw');
    await vi.waitFor(() => expect(count(a, 'signInWithPassword')).toBe(1));
    await flow.signOut();
    h.resolve();
    const r = await p;
    expect(r).toMatchObject({ ok: false, code: 'SUPERSEDED', state: STATES.SIGNED_OUT });
    expect(a._s.session).toBeNull();
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('double-submitted sign-in is refused while one is in flight', async () => {
    const a = gatedAuth({ signedIn: false });
    const flow = createOwnerMfaFlow(a);
    const h = a.hold('signInWithPassword');
    const first = flow.signIn('o@x.invalid', 'pw');
    await vi.waitFor(() => expect(count(a, 'signInWithPassword')).toBe(1));
    expect((await flow.signIn('o@x.invalid', 'pw')).code).toBe('BUSY');
    h.resolve();
    expect((await first).ok).toBe(true);
    expect(count(a, 'signInWithPassword')).toBe(1);
  });

  it('double-submitted enrollment code verifies once and is not double counted', async () => {
    const a = gatedAuth({ signedIn: true });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    const { factorId } = await flow.beginEnrollment('primary');
    const h = a.hold('challenge');
    const first = flow.confirmEnrollment(factorId, GOOD);
    await vi.waitFor(() => expect(count(a, 'challenge')).toBe(1));
    expect((await flow.confirmEnrollment(factorId, GOOD)).code).toBe('BUSY');
    h.resolve();
    expect((await first).state).toBe(STATES.UNLOCKED);
    expect(count(a, 'challenge')).toBe(1);
  });

  it('sign-out while enrollment is starting: no secret is returned and nothing stays pending', async () => {
    const a = gatedAuth({ signedIn: true });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    const h = a.hold('enroll');
    const p = flow.beginEnrollment('primary');
    await vi.waitFor(() => expect(count(a, 'enroll')).toBe(1));
    await flow.signOut();
    h.resolve();
    const r = await p;
    expect(r).toMatchObject({ ok: false, code: 'SUPERSEDED' });
    expect(r.secret).toBeUndefined();
    expect(r.qrDataUri).toBeUndefined();
    expect(flow.snapshot().enrollmentPending).toBe(false);
  });
});

describe('onAuthStateChange handling: no reentrant SDK calls, coalesced, SIGNED_OUT is immediate', () => {
  function withScheduler(a) {
    const queue = [];
    const flow = createOwnerMfaFlow(a, { schedule: (fn) => queue.push(fn) });
    return { flow, queue, flush: async () => { while (queue.length) await queue.shift()(); } };
  }

  it('never calls the SDK synchronously from the callback and coalesces a burst into one refresh', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    const { flow, queue, flush } = withScheduler(a);
    a._s.inCallback = true; // any SDK call made synchronously inside the callback throws
    flow.notifyAuthEvent('SIGNED_IN');
    flow.notifyAuthEvent('TOKEN_REFRESHED');
    flow.notifyAuthEvent('MFA_CHALLENGE_VERIFIED');
    a._s.inCallback = false;
    expect(a._s.calls).toEqual([]);
    expect(queue).toHaveLength(1);
    await flush();
    expect(count(a, 'getSession')).toBe(1);
    expect(flow.snapshot().state).toBe(STATES.UNLOCKED);
  });

  it('SIGNED_OUT locks immediately and discards an in-flight refresh', async () => {
    const a = gatedAuth({ factors: [A], aal: 'aal2' });
    const { flow } = withScheduler(a);
    const h = a.hold('listFactors');
    const inflight = flow.refresh();
    await vi.waitFor(() => expect(count(a, 'listFactors')).toBe(1));
    flow.notifyAuthEvent('SIGNED_OUT');
    expect(flow.snapshot().state).toBe(STATES.SIGNED_OUT);
    expect(flow.canRenderPrivate()).toBe(false);
    h.resolve();
    await inflight;
    expect(flow.snapshot().state).toBe(STATES.SIGNED_OUT);
  });
});
