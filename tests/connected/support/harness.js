// Client of the TEST-ONLY harness (backend/tests/e2e_harness/real_api_server.py). Used from Node test code only.
export const API = 'http://127.0.0.1:8765';
export const OWNER_ORIGIN = 'http://127.0.0.1:3005';
export const PUBLIC_ORIGIN = 'http://127.0.0.1:3006';
const headers = () => ({ 'x-harness-secret': process.env.SENTINAX_HARNESS_SECRET, 'content-type': 'application/json' });

export async function sign(body) {
    const r = await fetch(`${API}/__harness/sign`, { method: 'POST', headers: headers(), body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`harness sign failed: ${r.status}`);
    return (await r.json()).token;
}
export async function log({ clear = false } = {}) {
    const r = await fetch(`${API}/__harness/log${clear ? '?clear=1' : ''}`, { headers: headers() });
    return (await r.json()).log;
}
export async function setOwner(owner) {
    await fetch(`${API}/__harness/owner`, { method: 'POST', headers: headers(), body: JSON.stringify({ owner }) });
}
/** Tokens for the mocked Supabase Auth: valid ones unless `variant` is set for the aal2 token. */
export const minter = (variant = 'valid') => (aal, uid) => sign({ variant: aal === 'aal2' ? variant : 'valid', aal, sub: uid });
