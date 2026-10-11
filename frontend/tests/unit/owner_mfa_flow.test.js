import { describe, it, expect, beforeEach, vi } from 'vitest';
import fs from 'fs';
import path from 'path';
import { createOwnerMfaFlow, STATES } from '../../owner/js/mfa_flow.js';
import { isAal2Session } from '../../js/network/aalGate.js';

const root = path.resolve(__dirname, '../..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');
const GOOD_CODE = '123456';
const SECRET = 'JBSWY3DPEHPK3PXP';

/** Stateful fake of the supabase-js auth surface; any account-creation entry point explodes. */
function fakeAuth({ factors = [], password = 'pw-correct', signedIn = false, aal = 'aal1' } = {}) {
  const s = { session: signedIn ? { aal } : null, factors: factors.map((f) => ({ factor_type: 'totp', ...f })), calls: [], n: 0 };
  const log = (name, arg) => s.calls.push([name, arg]);
  const verified = () => s.factors.filter((f) => f.status === 'verified');
  const forbidden = (name) => () => { throw new Error(`${name} must never be called`); };
  const auth = {
    _s: s,
    signUp: forbidden('signUp'),
    signInWithOAuth: forbidden('signInWithOAuth'),
    signInWithOtp: forbidden('signInWithOtp'),
    signInAnonymously: forbidden('signInAnonymously'),
    async signInWithPassword({ email, password: pw }) {
      log('signInWithPassword', { email });
      if (pw !== password) return { data: { session: null }, error: { message: 'Invalid login credentials: SECRET-DETAIL' } };
      s.session = { aal: 'aal1' };
      return { data: { session: s.session }, error: null };
    },
    async getSession() { return { data: { session: s.session }, error: null }; },
    async signOut(opts) { log('signOut', opts); s.session = null; return { error: null }; },
    mfa: {
      async getAuthenticatorAssuranceLevel() {
        if (s.aalError) return s.aalError();
        return { data: { currentLevel: s.session ? s.session.aal : null, nextLevel: verified().length ? 'aal2' : 'aal1' }, error: null };
      },
      async listFactors() {
        if (s.listError) return { data: null, error: { message: 'x' } };
        return { data: { all: s.factors, totp: verified() }, error: null };
      },
      async enroll({ factorType, friendlyName, issuer }) {
        log('enroll', { factorType, friendlyName, issuer });
        if (s.factors.some((f) => f.friendly_name === friendlyName)) return { data: null, error: { message: 'friendly name exists' } };
        const id = `f${++s.n}`;
        s.factors.push({ id, factor_type: 'totp', status: 'unverified', friendly_name: friendlyName });
        return { data: { id, type: 'totp', totp: { qr_code: 'data:image/svg+xml;utf-8,<svg xmlns="http://www.w3.org/2000/svg"/>', secret: SECRET, uri: `otpauth://totp/Sentinax?secret=${SECRET}` } }, error: null };
      },
      async challenge({ factorId }) { log('challenge', { factorId }); return { data: { id: `ch-${factorId}` }, error: null }; },
      async verify({ factorId, challengeId, code }) {
        log('verify', { factorId, challengeId });
        if (code !== GOOD_CODE) return { data: null, error: { message: 'Invalid TOTP code entered: SECRET-DETAIL' } };
        s.factors.find((f) => f.id === factorId).status = 'verified';
        s.session = { aal: 'aal2' };
        return { data: {}, error: null };
      },
      async unenroll({ factorId }) { log('unenroll', { factorId }); s.factors = s.factors.filter((f) => f.id !== factorId); return { data: {}, error: null }; },
    },
  };
  return auth;
}

const names = (auth) => auth._s.calls.map((c) => c[0]);

describe('owner MFA flow: state derivation (fail closed)', () => {
  it('starts signed out and renders nothing private', async () => {
    const flow = createOwnerMfaFlow(fakeAuth());
    expect((await flow.refresh()).state).toBe(STATES.SIGNED_OUT);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('aal1 session with no factor needs enrollment and cannot render private content', async () => {
    const flow = createOwnerMfaFlow(fakeAuth({ signedIn: true }));
    expect((await flow.refresh()).state).toBe(STATES.NEEDS_ENROLLMENT);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('aal1 session with a verified factor needs a challenge, not private rendering', async () => {
    const flow = createOwnerMfaFlow(fakeAuth({ signedIn: true, factors: [{ id: 'a', status: 'verified', friendly_name: 'one' }] }));
    const r = await flow.refresh();
    expect(r.state).toBe(STATES.NEEDS_CHALLENGE);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('only aal2 with a verified factor unlocks', async () => {
    const flow = createOwnerMfaFlow(fakeAuth({ signedIn: true, aal: 'aal2', factors: [{ id: 'a', status: 'verified', friendly_name: 'one' }] }));
    expect((await flow.refresh()).state).toBe(STATES.UNLOCKED);
    expect(flow.canRenderPrivate()).toBe(true);
  });

  it.each([
    ['aal lookup errors', (a) => { a._s.aalError = () => ({ data: null, error: { message: 'x' } }); }],
    ['aal lookup throws', (a) => { a._s.aalError = () => { throw new Error('boom'); }; }],
    ['factor listing fails', (a) => { a._s.listError = true; }],
  ])('%s => ERROR_LOCKED', async (_n, tweak) => {
    const a = fakeAuth({ signedIn: true, aal: 'aal2', factors: [{ id: 'a', status: 'verified', friendly_name: 'one' }] });
    tweak(a);
    const flow = createOwnerMfaFlow(a);
    expect((await flow.refresh()).state).toBe(STATES.ERROR_LOCKED);
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('aal2 token but zero verified factors (stale after unenroll) fails closed', async () => {
    const flow = createOwnerMfaFlow(fakeAuth({ signedIn: true, aal: 'aal2', factors: [] }));
    expect((await flow.refresh()).state).toBe(STATES.ERROR_LOCKED);
  });

  it('unverified factors never count as verified', async () => {
    const flow = createOwnerMfaFlow(fakeAuth({ signedIn: true, factors: [{ id: 'a', status: 'unverified', friendly_name: 'one' }] }));
    expect((await flow.refresh()).state).toBe(STATES.NEEDS_ENROLLMENT);
  });

  it('session expiry after unlock returns to SIGNED_OUT; aal downgrade returns to NEEDS_CHALLENGE', async () => {
    const a = fakeAuth({ signedIn: true, aal: 'aal2', factors: [{ id: 'a', status: 'verified', friendly_name: 'one' }] });
    const flow = createOwnerMfaFlow(a);
    expect((await flow.refresh()).state).toBe(STATES.UNLOCKED);
    a._s.session = { aal: 'aal1' };
    expect((await flow.refresh()).state).toBe(STATES.NEEDS_CHALLENGE);
    expect(flow.canRenderPrivate()).toBe(false);
    a._s.session = null;
    expect((await flow.refresh()).state).toBe(STATES.SIGNED_OUT);
  });
});

describe('owner MFA flow: sign-in, no account creation', () => {
  it('wrong password: generic failure, no provider detail, no account creation attempt', async () => {
    const a = fakeAuth();
    const flow = createOwnerMfaFlow(a);
    const r = await flow.signIn('owner@example.invalid', 'nope');
    expect(r).toEqual({ ok: false, code: 'SIGN_IN_FAILED', state: STATES.SIGNED_OUT });
    expect(JSON.stringify(r)).not.toContain('SECRET-DETAIL');
    expect(names(a)).toEqual(['signInWithPassword']);
  });

  it('empty or non-string credentials are rejected locally', async () => {
    const a = fakeAuth();
    const flow = createOwnerMfaFlow(a);
    for (const [e, p] of [['', 'x'], ['a@b.c', ''], [null, 'x'], ['a@b.c', undefined], [{}, []]]) {
      expect((await flow.signIn(e, p)).code).toBe('INVALID_INPUT');
    }
    expect(a._s.calls).toEqual([]);
  });

  it('successful password sign-in alone yields aal1 and a locked UI', async () => {
    const flow = createOwnerMfaFlow(fakeAuth());
    const r = await flow.signIn('owner@example.invalid', 'pw-correct');
    expect(r.ok).toBe(true);
    expect(r.state).toBe(STATES.NEEDS_ENROLLMENT);
    expect(flow.canRenderPrivate()).toBe(false);
  });
});

describe('owner MFA flow: TOTP enrollment', () => {
  let a, flow;
  beforeEach(() => { a = fakeAuth({ signedIn: true }); flow = createOwnerMfaFlow(a); });

  it('returns secret + QR once and does not retain them', async () => {
    await flow.refresh();
    const r = await flow.beginEnrollment('primary');
    expect(r.ok).toBe(true);
    expect(r.secret).toBe(SECRET);
    expect(r.qrDataUri.startsWith('data:image/svg+xml')).toBe(true);
    const snap = JSON.stringify(flow.snapshot());
    expect(snap).not.toContain(SECRET);
    expect(snap).not.toContain('otpauth');
    expect(snap).not.toContain('svg');
  });

  it('rejects unsafe friendly names without calling the provider', async () => {
    await flow.refresh();
    for (const n of ['', ' ', 'a'.repeat(33), '<img src=x>', null, 'x;y']) {
      expect((await flow.beginEnrollment(n)).code).toBe('INVALID_INPUT');
    }
    expect(names(a)).not.toContain('enroll');
  });

  it('refuses to start enrollment when signed out', async () => {
    const out = createOwnerMfaFlow(fakeAuth());
    await out.refresh();
    expect((await out.beginEnrollment('primary')).code).toBe('NOT_ALLOWED');
  });

  it.each(['12345', 'abcdef', '1234567', ' 123456', '123456 ', '', null, undefined, 123456])('malformed code %j is rejected locally', async (code) => {
    await flow.refresh();
    const { factorId } = await flow.beginEnrollment('primary');
    const before = names(a).length;
    expect((await flow.confirmEnrollment(factorId, code)).code).toBe('INVALID_INPUT');
    expect(names(a).length).toBe(before);
  });

  it('wrong code keeps the user locked; right code unlocks at aal2', async () => {
    await flow.refresh();
    const { factorId } = await flow.beginEnrollment('primary');
    const bad = await flow.confirmEnrollment(factorId, '000000');
    expect(bad).toMatchObject({ ok: false, code: 'CODE_REJECTED', state: STATES.NEEDS_ENROLLMENT });
    expect(JSON.stringify(bad)).not.toContain('SECRET-DETAIL');
    expect(flow.canRenderPrivate()).toBe(false);
    const good = await flow.confirmEnrollment(factorId, GOOD_CODE);
    expect(good).toMatchObject({ ok: true, state: STATES.UNLOCKED, verifiedFactorCount: 1, backupFactorReady: false });
    expect(flow.canRenderPrivate()).toBe(true);
  });

  it('five wrong codes cancel the pending factor and unenroll it', async () => {
    await flow.refresh();
    const { factorId } = await flow.beginEnrollment('primary');
    let last;
    for (let i = 0; i < 5; i++) last = await flow.confirmEnrollment(factorId, '000000');
    expect(last.code).toBe('ENROLLMENT_CANCELLED');
    expect(a._s.calls.filter((c) => c[0] === 'unenroll')).toHaveLength(1);
    expect((await flow.confirmEnrollment(factorId, GOOD_CODE)).code).toBe('NOT_ALLOWED');
  });

  it('cannot confirm a factor id that was not started by this flow', async () => {
    await flow.refresh();
    await flow.beginEnrollment('primary');
    const before = names(a).length;
    expect((await flow.confirmEnrollment('someone-elses', GOOD_CODE)).code).toBe('NOT_ALLOWED');
    expect(names(a).length).toBe(before);
  });

  it('cancel removes the unverified factor', async () => {
    await flow.refresh();
    const { factorId } = await flow.beginEnrollment('primary');
    await flow.cancelEnrollment();
    expect(a._s.calls.find((c) => c[0] === 'unenroll')[1]).toEqual({ factorId });
  });

  it('two independent verified factors are required for backupFactorReady', async () => {
    await flow.refresh();
    const f1 = await flow.beginEnrollment('primary');
    await flow.confirmEnrollment(f1.factorId, GOOD_CODE);
    const dup = await flow.beginEnrollment('primary');
    expect(dup).toMatchObject({ ok: false, code: 'ENROLL_FAILED' });
    const f2 = await flow.beginEnrollment('backup');
    expect(f2.factorId).not.toBe(f1.factorId);
    const done = await flow.confirmEnrollment(f2.factorId, GOOD_CODE);
    expect(done).toMatchObject({ state: STATES.UNLOCKED, verifiedFactorCount: 2, backupFactorReady: true });
  });
});

describe('owner MFA flow: challenge with an existing factor', () => {
  const existing = [{ id: 'a', status: 'verified', friendly_name: 'one' }];

  it('wrong code stays aal1/locked; right code unlocks', async () => {
    const flow = createOwnerMfaFlow(fakeAuth({ signedIn: true, factors: existing }));
    await flow.refresh();
    expect(await flow.challenge('a', '000000')).toMatchObject({ ok: false, code: 'CODE_REJECTED', state: STATES.NEEDS_CHALLENGE });
    expect(flow.canRenderPrivate()).toBe(false);
    expect(await flow.challenge('a', GOOD_CODE)).toMatchObject({ ok: true, state: STATES.UNLOCKED });
  });

  it('unknown or unverified factor ids never reach the provider', async () => {
    const a = fakeAuth({ signedIn: true, factors: [...existing, { id: 'u', status: 'unverified', friendly_name: 'x' }] });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    for (const id of ['zzz', 'u', '', null]) expect((await flow.challenge(id, GOOD_CODE)).code).toBe('NOT_ALLOWED');
    expect(names(a)).not.toContain('challenge');
  });
});

describe('owner MFA flow: sign-out', () => {
  it('clears state, drops pending enrollment and requests global scope when asked', async () => {
    const a = fakeAuth({ signedIn: true });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    const { factorId } = await flow.beginEnrollment('primary');
    const r = await flow.signOut('global');
    expect(r.state).toBe(STATES.SIGNED_OUT);
    expect(a._s.calls.find((c) => c[0] === 'signOut')[1]).toEqual({ scope: 'global' });
    expect((await flow.confirmEnrollment(factorId, GOOD_CODE)).code).toBe('NOT_ALLOWED');
    expect(flow.canRenderPrivate()).toBe(false);
  });

  it('a provider sign-out error still leaves the UI locked', async () => {
    const a = fakeAuth({ signedIn: true, aal: 'aal2', factors: [{ id: 'a', status: 'verified', friendly_name: 'one' }] });
    a.signOut = async () => ({ error: { message: 'network' } });
    const flow = createOwnerMfaFlow(a);
    await flow.refresh();
    const r = await flow.signOut();
    expect(r.state).toBe(STATES.SIGNED_OUT);
    expect(flow.canRenderPrivate()).toBe(false);
  });
});

describe('aal gate used by the legacy client', () => {
  it('true only for a currentLevel of aal2', async () => {
    const mk = (r) => ({ mfa: { getAuthenticatorAssuranceLevel: async () => r } });
    expect(await isAal2Session(mk({ data: { currentLevel: 'aal2' }, error: null }))).toBe(true);
    expect(await isAal2Session(mk({ data: { currentLevel: 'aal1' }, error: null }))).toBe(false);
    expect(await isAal2Session(mk({ data: { currentLevel: 'AAL2' }, error: null }))).toBe(false);
    expect(await isAal2Session(mk({ data: null, error: { message: 'x' } }))).toBe(false);
    expect(await isAal2Session({ mfa: { getAuthenticatorAssuranceLevel: async () => { throw new Error('x'); } } })).toBe(false);
    expect(await isAal2Session({})).toBe(false);
    expect(await isAal2Session(null)).toBe(false);
  });
});
