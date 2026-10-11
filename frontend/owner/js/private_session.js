/**
 * Session lifecycle of the private application inside the owner document (Phase 25D1E3). Pure logic: no DOM, no storage, no logging, no SDK import.
 *
 * The MFA state machine (mfa_flow.js) stays the ONLY AAL logic. This controller adds the step the client cannot decide for itself: the server's
 * owner authorization (GET /private/portfolio/summary behind require_private_owner). The private view may exist only while
 *   the flow is UNLOCKED  AND  the server accepted a fresh memory-only access token  AND  nothing about the session changed since.
 *
 * Stale-result rules (an older async result must never populate the interface):
 *  - `gen` is bumped by every invalidation and every new authorization attempt; a completion handler applies its result only if its own `gen` is still current,
 *    the flow is still UNLOCKED and flow.canRenderPrivate() still holds (AbortController is only a courtesy: it cannot be relied on).
 *  - Leaving UNLOCKED invalidates synchronously (the UI is cleared before any SDK promise settles).
 *  - After the response the session identity (user id + session id claim, change detection only) is re-read; a different identity discards the response.
 *  - Anything other than a well-formed UNAVAILABLE envelope from a 200 hides the private view. Failures are never reported as EMPTY.
 * The access token is only ever placed in the Authorization header of a credential-less, uncached, same-origin GET.
 */
export const PRIVATE_SUMMARY_PATH = '/private/portfolio/summary';

export const AUTHZ = Object.freeze({
    IDLE: 'IDLE',
    AUTHORIZING: 'AUTHORIZING',
    AUTHORIZED: 'AUTHORIZED',
    DENIED: 'DENIED',
    SERVER_UNAVAILABLE: 'SERVER_UNAVAILABLE',
    UNREACHABLE: 'UNREACHABLE',
    UNCONFIGURED: 'UNCONFIGURED',
    MALFORMED: 'MALFORMED',
    ERROR: 'ERROR',
});

const UNLOCKED = 'UNLOCKED';
const CONTRACT_KEYS = ['as_of', 'contract_version', 'data_status', 'generated_at', 'portfolios', 'positions', 'reason', 'reason_code', 'source', 'warnings'];
const REASON_CODE = /^[A-Z][A-Z0-9_]{2,63}$/;
const MAX_RESTARTS = 3;

export function resolvePrivateApi(cfg) {
    if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) return null;
    const keys = Object.keys(cfg);
    if (keys.length !== 1 || keys[0] !== 'mode' || cfg.mode !== 'same-origin') return null;
    return { url: PRIVATE_SUMMARY_PATH };
}

const isStr = (v, max) => typeof v === 'string' && v.length > 0 && v.length <= max;

/** Returns a frozen normalised summary, or null for anything that is not exactly the UNAVAILABLE contract (EMPTY/AVAILABLE are not acceptable in this phase). */
export function validateSummary(body) {
    if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
    const keys = Object.keys(body).sort();
    if (keys.length !== CONTRACT_KEYS.length || keys.some((k, i) => k !== CONTRACT_KEYS[i])) return null;
    if (body.contract_version !== 'private_portfolio_summary.v1') return null;
    if (body.data_status !== 'UNAVAILABLE') return null;
    if (typeof body.reason_code !== 'string' || !REASON_CODE.test(body.reason_code)) return null;
    if (!isStr(body.reason, 500)) return null;
    if (body.source !== null || body.as_of !== null) return null;
    if (!Array.isArray(body.portfolios) || body.portfolios.length !== 0 || !Array.isArray(body.positions) || body.positions.length !== 0) return null;
    if (!Array.isArray(body.warnings) || body.warnings.length > 10 || body.warnings.some((w) => !isStr(w, 300))) return null;
    if (typeof body.generated_at !== 'string' || body.generated_at.length > 64 || !Number.isFinite(Date.parse(body.generated_at))) return null;
    return Object.freeze({
        contractVersion: body.contract_version,
        dataStatus: body.data_status,
        reasonCode: body.reason_code,
        reason: body.reason,
        generatedAt: body.generated_at,
        warnings: Object.freeze([...body.warnings]),
    });
}

function sessionIdClaim(token) {
    try {
        const part = token.split('.')[1];
        const json = JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/')));
        return typeof json.session_id === 'string' ? json.session_id : '';
    } catch {
        return '';
    }
}

/** Identity key for CHANGE DETECTION only (the payload is not verified here and never decides access). */
export function sessionKeyOf(session) {
    if (!session || typeof session.access_token !== 'string' || !session.access_token) return null;
    const id = session.user?.id;
    if (typeof id !== 'string' || !id) return null;
    return `${id}:${sessionIdClaim(session.access_token)}`;
}

const failureStatus = (code) => (code === 401 || code === 403 ? AUTHZ.DENIED : code === 503 ? AUTHZ.SERVER_UNAVAILABLE : AUTHZ.ERROR);

export function createPrivateSession({ api, flow, getSession, fetchImpl, now = () => Date.now(), timeoutMs = 10000, revalidateMs = 60000, retryMs = 15000 }) {
    let gen = 0;
    let unlocked = false;
    let inflight = null; // AbortController of the current attempt
    let key = null;
    let lastOk = 0;
    let lastAttempt = 0;
    let state = { status: AUTHZ.IDLE, summary: null };
    const listeners = [];

    const publish = (status, summary = null) => {
        state = { status, summary };
        const snap = { status, summary };
        listeners.forEach((fn) => fn(snap));
    };

    function invalidate(status = AUTHZ.IDLE) {
        gen += 1;
        if (inflight) { try { inflight.abort(); } catch { /* ignore */ } }
        inflight = null;
        key = null;
        lastOk = 0;
        lastAttempt = 0;
        if (state.status !== status || state.summary !== null) publish(status, null);
    }

    async function readSession() {
        try {
            const s = await getSession();
            const k = sessionKeyOf(s);
            return k ? { session: s, key: k } : null;
        } catch {
            return null;
        }
    }

    async function attempt(restarts = 0) {
        const mine = ++gen;
        const stale = () => mine !== gen || !unlocked;
        const ac = new AbortController();
        inflight = ac;
        lastAttempt = now();
        const fail = (status) => {
            if (stale()) return;
            inflight = null;
            lastOk = 0;
            key = null;
            publish(status, null);
        };

        const first = await readSession();
        if (stale()) return;
        if (!first) { fail(AUTHZ.ERROR); return; }
        if (key !== null && key !== first.key) publish(AUTHZ.IDLE, null); // another identity: clear the previous view before anything else
        key = first.key;
        if (state.status !== AUTHZ.AUTHORIZED) publish(AUTHZ.AUTHORIZING, null);

        const timer = setTimeout(() => ac.abort(), timeoutMs);
        let res;
        try {
            res = await fetchImpl(api.url, {
                method: 'GET',
                headers: { Accept: 'application/json', Authorization: `Bearer ${first.session.access_token}` },
                credentials: 'omit',
                cache: 'no-store',
                redirect: 'error',
                referrerPolicy: 'no-referrer',
                mode: 'same-origin',
                signal: ac.signal,
            });
        } catch {
            clearTimeout(timer);
            fail(AUTHZ.UNREACHABLE);
            return;
        }
        clearTimeout(timer);
        if (stale()) return;
        if (res.status !== 200) { fail(failureStatus(res.status)); return; }
        const type = String(res.headers?.get?.('content-type') ?? '').toLowerCase();
        if (!type.startsWith('application/json')) { fail(AUTHZ.MALFORMED); return; }
        let body;
        try { body = await res.json(); } catch { body = undefined; }
        if (stale()) return;
        const summary = validateSummary(body);
        if (!summary) { fail(AUTHZ.MALFORMED); return; }

        const second = await readSession();
        if (stale()) return;
        if (!second) { fail(AUTHZ.ERROR); return; }
        if (second.key !== first.key) { // the session changed while the request was in flight: this response belongs to someone else
            inflight = null;
            if (restarts >= MAX_RESTARTS) { fail(AUTHZ.ERROR); return; }
            publish(AUTHZ.IDLE, null);
            await attempt(restarts + 1);
            return;
        }
        if (!flow.canRenderPrivate()) { fail(AUTHZ.ERROR); return; }
        inflight = null;
        lastOk = now();
        publish(AUTHZ.AUTHORIZED, summary);
    }

    function onFlowChange(view) {
        if (!view || view.state !== UNLOCKED) {
            unlocked = false;
            if (state.status !== AUTHZ.IDLE || inflight || key !== null) invalidate(AUTHZ.IDLE);
            return;
        }
        unlocked = true;
        if (!api) { if (state.status !== AUTHZ.UNCONFIGURED) publish(AUTHZ.UNCONFIGURED, null); return; }
        if (inflight) return; // coalesce repeated AUTH events
        const t = now();
        if (state.status === AUTHZ.AUTHORIZED && t - lastOk < revalidateMs) return;
        if (state.status !== AUTHZ.AUTHORIZED && state.status !== AUTHZ.IDLE && state.status !== AUTHZ.AUTHORIZING && t - lastAttempt < retryMs) return;
        attempt();
    }

    return {
        onFlowChange,
        invalidate: () => invalidate(AUTHZ.IDLE),
        snapshot: () => ({ status: state.status, summary: state.summary }),
        onChange: (fn) => { listeners.push(fn); },
    };
}
