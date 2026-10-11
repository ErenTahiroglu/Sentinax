/**
 * Owner-only TOTP enrolment / challenge state machine (Phase 25D1C0).
 * Pure logic over the supabase-js `auth` surface: no DOM, no storage, no logging.
 * Secrets (TOTP secret, QR) are handed to the caller once and never retained here.
 * Provider error text is never surfaced; callers only see fixed codes.
 * UX gate only: the backend owner dependency and RLS are the authority.
 */
export const STATES = Object.freeze({
    SIGNED_OUT: 'SIGNED_OUT',
    NEEDS_ENROLLMENT: 'NEEDS_ENROLLMENT',
    NEEDS_CHALLENGE: 'NEEDS_CHALLENGE',
    UNLOCKED: 'UNLOCKED',
    ERROR_LOCKED: 'ERROR_LOCKED',
});

const CODE_RE = /^\d{6}$/;
const NAME_RE = /^[A-Za-z0-9 _-]{1,32}$/;
const MAX_ENROLL_ATTEMPTS = 5;
const MAX_FRESH_MS = 60000;

const validCode = (c) => typeof c === 'string' && CODE_RE.test(c);
const validName = (n) => typeof n === 'string' && NAME_RE.test(n) && n.trim() === n;
const nonEmpty = (v, max) => typeof v === 'string' && v.length > 0 && v.length <= max;

export function createOwnerMfaFlow(auth, now = () => Date.now()) {
    let current = { state: STATES.SIGNED_OUT, factors: [], checkedAt: 0 };
    let pending = null; // { factorId, attempts }
    const listeners = [];

    const publish = (state, factors = []) => {
        current = { state, factors, checkedAt: now() };
        if (state === STATES.SIGNED_OUT || state === STATES.ERROR_LOCKED) pending = null;
        listeners.forEach((fn) => fn(view()));
        return view();
    };

    const view = () => ({
        state: current.state,
        verifiedFactorCount: current.factors.length,
        backupFactorReady: current.state === STATES.UNLOCKED && current.factors.length >= 2,
        factors: current.factors.map((f) => ({ id: f.id, friendlyName: f.friendly_name || '' })),
    });

    async function refresh() {
        try {
            const s = await auth.getSession();
            if (s.error) return publish(STATES.ERROR_LOCKED);
            if (!s.data?.session) return publish(STATES.SIGNED_OUT);
            const a = await auth.mfa.getAuthenticatorAssuranceLevel();
            if (a.error || !a.data) return publish(STATES.ERROR_LOCKED);
            const f = await auth.mfa.listFactors();
            if (f.error || !f.data) return publish(STATES.ERROR_LOCKED);
            const verified = (f.data.all ?? f.data.totp ?? []).filter(
                (x) => x.factor_type === 'totp' && x.status === 'verified',
            );
            const level = a.data.currentLevel;
            if (level === 'aal2') {
                return verified.length > 0 ? publish(STATES.UNLOCKED, verified) : publish(STATES.ERROR_LOCKED);
            }
            if (level === 'aal1') {
                return publish(verified.length > 0 ? STATES.NEEDS_CHALLENGE : STATES.NEEDS_ENROLLMENT, verified);
            }
            return publish(STATES.ERROR_LOCKED);
        } catch {
            return publish(STATES.ERROR_LOCKED);
        }
    }

    const fail = (code) => ({ ok: false, code, state: current.state });

    async function challengeAndVerify(factorId, code) {
        try {
            const c = await auth.mfa.challenge({ factorId });
            if (c.error || !c.data?.id) return false;
            const v = await auth.mfa.verify({ factorId, challengeId: c.data.id, code });
            return !v.error;
        } catch {
            return false;
        }
    }

    async function signIn(email, password) {
        if (!nonEmpty(email, 320) || !nonEmpty(password, 1024)) return fail('INVALID_INPUT');
        try {
            const r = await auth.signInWithPassword({ email, password });
            if (r.error || !r.data?.session) return { ok: false, code: 'SIGN_IN_FAILED', state: (await refresh()).state };
        } catch {
            return { ok: false, code: 'SIGN_IN_FAILED', state: (await refresh()).state };
        }
        return { ok: true, ...(await refresh()) };
    }

    async function dropPending() {
        if (!pending) return;
        const { factorId } = pending;
        pending = null;
        try { await auth.mfa.unenroll({ factorId }); } catch { /* best effort: an unverified factor grants nothing */ }
    }

    async function beginEnrollment(friendlyName) {
        if (current.state !== STATES.NEEDS_ENROLLMENT && current.state !== STATES.UNLOCKED) return fail('NOT_ALLOWED');
        if (!validName(friendlyName)) return fail('INVALID_INPUT');
        await dropPending();
        try {
            const r = await auth.mfa.enroll({ factorType: 'totp', friendlyName, issuer: 'Sentinax' });
            if (r.error || !r.data?.id || !r.data.totp) return fail('ENROLL_FAILED');
            pending = { factorId: r.data.id, attempts: 0 };
            const qr = r.data.totp.qr_code;
            return {
                ok: true,
                factorId: r.data.id,
                secret: r.data.totp.secret,
                qrDataUri: typeof qr === 'string' && qr.startsWith('data:image/svg+xml') ? qr : null,
            };
        } catch {
            return fail('ENROLL_FAILED');
        }
    }

    async function confirmEnrollment(factorId, code) {
        if (!pending || pending.factorId !== factorId) return fail('NOT_ALLOWED');
        if (!validCode(code)) return fail('INVALID_INPUT');
        if (await challengeAndVerify(factorId, code)) {
            pending = null;
            return { ok: true, ...(await refresh()) };
        }
        pending.attempts += 1;
        if (pending.attempts >= MAX_ENROLL_ATTEMPTS) {
            await dropPending();
            return fail('ENROLLMENT_CANCELLED');
        }
        return fail('CODE_REJECTED');
    }

    async function cancelEnrollment() {
        await dropPending();
        return { ok: true, state: current.state };
    }

    async function challenge(factorId, code) {
        if (current.state !== STATES.NEEDS_CHALLENGE) return fail('NOT_ALLOWED');
        if (typeof factorId !== 'string' || !current.factors.some((f) => f.id === factorId)) return fail('NOT_ALLOWED');
        if (!validCode(code)) return fail('INVALID_INPUT');
        if (await challengeAndVerify(factorId, code)) return { ok: true, ...(await refresh()) };
        return fail('CODE_REJECTED');
    }

    async function signOut(scope = 'local') {
        const safeScope = scope === 'global' ? 'global' : 'local';
        let ok = true;
        try {
            const r = await auth.signOut({ scope: safeScope });
            ok = !r?.error;
        } catch {
            ok = false;
        }
        publish(STATES.SIGNED_OUT);
        return { ok, state: STATES.SIGNED_OUT };
    }

    return {
        refresh,
        signIn,
        beginEnrollment,
        confirmEnrollment,
        cancelEnrollment,
        challenge,
        signOut,
        snapshot: () => ({ ...view(), enrollmentPending: pending !== null }),
        canRenderPrivate: (t = now()) => current.state === STATES.UNLOCKED && t - current.checkedAt <= MAX_FRESH_MS,
        onChange: (fn) => { listeners.push(fn); },
    };
}
