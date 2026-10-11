/**
 * Owner-only TOTP enrolment / challenge state machine (Phase 25D1C0, race-hardened in H1).
 * Pure logic over the supabase-js `auth` surface: no DOM, no storage, no logging.
 * Secrets (TOTP secret, QR) are handed to the caller once and never retained here.
 * Provider error text is never surfaced; callers only see fixed codes.
 * UX gate only: the backend owner dependency and RLS are the authority.
 *
 * Concurrency rules (an older async result must never override a newer locked/signed-out state):
 *  - `refreshSeq`: every refresh and every mutating operation invalidates older in-flight refreshes.
 *  - `epoch`: bumped whenever the flow locks (SIGNED_OUT / ERROR_LOCKED). Operations compare the epoch
 *    captured at their start and report SUPERSEDED instead of applying a late result.
 *  - A late success after a lock re-closes the session defensively (scope: local).
 *  - Mutating operations are exclusive (BUSY); sign-out is never blocked and always wins.
 *  - Every SDK call has a timeout; a hung call locks the flow.
 *  - `notifyAuthEvent` is synchronous and never calls the SDK; it schedules one coalesced refresh.
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
const DEFAULT_TIMEOUT_MS = 15000;

const validCode = (c) => typeof c === 'string' && CODE_RE.test(c);
const validName = (n) => typeof n === 'string' && NAME_RE.test(n) && n.trim() === n;
const nonEmpty = (v, max) => typeof v === 'string' && v.length > 0 && v.length <= max;

export function createOwnerMfaFlow(auth, options = {}) {
    const now = options.now ?? (() => Date.now());
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const schedule = options.schedule ?? ((fn) => setTimeout(fn, 0));

    let current = { state: STATES.SIGNED_OUT, factors: [], checkedAt: 0 };
    let pending = null; // { factorId, attempts }
    let epoch = 0;
    let refreshSeq = 0;
    let busy = false;
    let signingOut = false;
    let refreshScheduled = false;
    const listeners = [];

    const view = () => ({
        state: current.state,
        verifiedFactorCount: current.factors.length,
        backupFactorReady: current.state === STATES.UNLOCKED && current.factors.length >= 2,
        factors: current.factors.map((f) => ({ id: f.id, friendlyName: f.friendly_name || '' })),
    });

    const publish = (state, factors = []) => {
        if (state === STATES.SIGNED_OUT || state === STATES.ERROR_LOCKED) {
            pending = null;
            epoch += 1;
        }
        current = { state, factors, checkedAt: now() };
        const v = view();
        listeners.forEach((fn) => fn(v));
        return v;
    };

    // Bounded SDK call: a hung or failing call rejects and the caller fails closed.
    const call = (fn) => new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('timeout')), timeoutMs);
        Promise.resolve().then(fn).then(
            (v) => { clearTimeout(timer); resolve(v); },
            (e) => { clearTimeout(timer); reject(e); },
        );
    });

    const fail = (code) => ({ ok: false, code, state: current.state });

    async function refresh() {
        if (signingOut) return view();
        const seq = ++refreshSeq;
        const ep = epoch;
        const stale = () => seq !== refreshSeq || ep !== epoch || signingOut;
        try {
            const s = await call(() => auth.getSession());
            if (stale()) return view();
            if (s.error) return publish(STATES.ERROR_LOCKED);
            if (!s.data?.session) return publish(STATES.SIGNED_OUT);
            const a = await call(() => auth.mfa.getAuthenticatorAssuranceLevel());
            if (stale()) return view();
            if (a.error || !a.data) return publish(STATES.ERROR_LOCKED);
            const f = await call(() => auth.mfa.listFactors()); // server round-trip (GET /user): authoritative for factors and session validity
            if (stale()) return view();
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
            return stale() ? view() : publish(STATES.ERROR_LOCKED);
        }
    }

    async function challengeAndVerify(factorId, code) {
        try {
            const c = await call(() => auth.mfa.challenge({ factorId }));
            if (c.error || !c.data?.id) return false;
            const v = await call(() => auth.mfa.verify({ factorId, challengeId: c.data.id, code }));
            return !v.error;
        } catch {
            return false;
        }
    }

    // A late success after the flow locked would leave a live SDK session behind: close it.
    async function supersededCleanup() {
        if (current.state !== STATES.SIGNED_OUT) return;
        try { await call(() => auth.signOut({ scope: 'local' })); } catch { /* best effort */ }
    }

    async function exclusive(fn) {
        if (busy) return fail('BUSY');
        busy = true;
        refreshSeq += 1; // results of refreshes that started before this operation are stale
        try { return await fn(); } finally { busy = false; }
    }

    async function dropPending() {
        if (!pending) return;
        const { factorId } = pending;
        pending = null;
        try { await call(() => auth.mfa.unenroll({ factorId })); } catch { /* best effort: an unverified factor grants nothing */ }
    }

    function signIn(email, password) {
        if (!nonEmpty(email, 320) || !nonEmpty(password, 1024)) return Promise.resolve(fail('INVALID_INPUT'));
        return exclusive(async () => {
            const ep = epoch;
            let ok = false;
            try {
                const r = await call(() => auth.signInWithPassword({ email, password }));
                ok = !r.error && !!r.data?.session;
            } catch { ok = false; }
            if (ep !== epoch) {
                if (ok) await supersededCleanup();
                return fail('SUPERSEDED');
            }
            if (!ok) return { ok: false, code: 'SIGN_IN_FAILED', state: (await refresh()).state };
            return { ok: true, ...(await refresh()) };
        });
    }

    function beginEnrollment(friendlyName) {
        if (current.state !== STATES.NEEDS_ENROLLMENT && current.state !== STATES.UNLOCKED) return Promise.resolve(fail('NOT_ALLOWED'));
        if (!validName(friendlyName)) return Promise.resolve(fail('INVALID_INPUT'));
        return exclusive(async () => {
            const ep = epoch;
            await dropPending();
            let r;
            try {
                r = await call(() => auth.mfa.enroll({ factorType: 'totp', friendlyName, issuer: 'Sentinax' }));
            } catch { return fail('ENROLL_FAILED'); }
            if (ep !== epoch) return fail('SUPERSEDED'); // never hand a secret to a locked UI
            if (r.error || !r.data?.id || !r.data.totp) return fail('ENROLL_FAILED');
            pending = { factorId: r.data.id, attempts: 0 };
            const qr = r.data.totp.qr_code;
            return {
                ok: true,
                factorId: r.data.id,
                secret: r.data.totp.secret,
                qrDataUri: typeof qr === 'string' && qr.startsWith('data:image/svg+xml') ? qr : null,
            };
        });
    }

    function confirmEnrollment(factorId, code) {
        if (!pending || pending.factorId !== factorId) return Promise.resolve(fail('NOT_ALLOWED'));
        if (!validCode(code)) return Promise.resolve(fail('INVALID_INPUT'));
        return exclusive(async () => {
            const ep = epoch;
            const mine = pending;
            const ok = await challengeAndVerify(factorId, code);
            if (ep !== epoch) {
                if (ok) await supersededCleanup();
                return fail('SUPERSEDED');
            }
            if (ok) {
                pending = null;
                return { ok: true, ...(await refresh()) };
            }
            mine.attempts += 1;
            if (mine.attempts >= MAX_ENROLL_ATTEMPTS) {
                await dropPending();
                await refresh();
                return fail('ENROLLMENT_CANCELLED');
            }
            const v = await refresh(); // report the truth after a failure (session may have expired)
            return { ok: false, code: 'CODE_REJECTED', state: v.state };
        });
    }

    async function cancelEnrollment() {
        await dropPending();
        return { ok: true, state: current.state };
    }

    function challenge(factorId, code) {
        if (current.state !== STATES.NEEDS_CHALLENGE) return Promise.resolve(fail('NOT_ALLOWED'));
        if (typeof factorId !== 'string' || !current.factors.some((f) => f.id === factorId)) return Promise.resolve(fail('NOT_ALLOWED'));
        if (!validCode(code)) return Promise.resolve(fail('INVALID_INPUT'));
        return exclusive(async () => {
            const ep = epoch;
            const ok = await challengeAndVerify(factorId, code);
            if (ep !== epoch) {
                if (ok) await supersededCleanup();
                return fail('SUPERSEDED');
            }
            if (ok) return { ok: true, ...(await refresh()) };
            const v = await refresh();
            return { ok: false, code: 'CODE_REJECTED', state: v.state };
        });
    }

    async function signOut(scope = 'local') {
        const safeScope = scope === 'global' ? 'global' : 'local';
        signingOut = true;
        refreshSeq += 1;
        publish(STATES.SIGNED_OUT); // lock the UI first, then tell the provider
        let ok = true;
        try {
            const r = await call(() => auth.signOut({ scope: safeScope }));
            ok = !r?.error;
        } catch { ok = false; }
        signingOut = false;
        publish(STATES.SIGNED_OUT);
        return { ok, state: STATES.SIGNED_OUT };
    }

    // Called synchronously from supabase-js onAuthStateChange: must not call the SDK (reentrancy/deadlock risk).
    function notifyAuthEvent(event) {
        if (event === 'SIGNED_OUT') {
            refreshSeq += 1;
            if (!signingOut) publish(STATES.SIGNED_OUT);
            return;
        }
        if (refreshScheduled || signingOut) return;
        refreshScheduled = true;
        schedule(() => { refreshScheduled = false; return refresh(); }); // refresh() never rejects
    }

    return {
        refresh,
        signIn,
        beginEnrollment,
        confirmEnrollment,
        cancelEnrollment,
        challenge,
        signOut,
        notifyAuthEvent,
        snapshot: () => ({ ...view(), enrollmentPending: pending !== null }),
        canRenderPrivate: (t = now()) => current.state === STATES.UNLOCKED && t - current.checkedAt <= MAX_FRESH_MS,
        onChange: (fn) => { listeners.push(fn); },
    };
}
