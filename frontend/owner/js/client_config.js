/**
 * Supabase client configuration for the owner bootstrap page (Phase 25D1C0-H1).
 *
 * persistSession:false => supabase-js keeps the session in memory only (verified in auth-js 2.117.1:
 * storage becomes an in-memory adapter and the cross-tab BroadcastChannel is not created), so the
 * bootstrap session is never written to persistent browser storage and the legacy SPA's default client (same origin,
 * same default storage key) cannot silently inherit it. A page reload signs the owner out by design.
 * detectSessionInUrl:false => the page never consumes tokens from the URL.
 *
 * This is session hygiene, NOT an origin boundary: same-origin script (e.g. an XSS in the legacy SPA)
 * remains a threat; see the runbook for the separate-origin plan.
 */
export const OWNER_AUTH_OPTIONS = Object.freeze({
    persistSession: false,
    autoRefreshToken: true,
    detectSessionInUrl: false,
});

// The Supabase SDK is a local, deterministic build of the version pinned in package-lock.json (scripts/owner_sdk_build.mjs;
// hash recorded in vendor/supabase-js.manifest.json). The page never fetches executable code from another origin.
export const SUPABASE_SDK_PATH = '../vendor/supabase-js.2.117.1.mjs';

const PROJECT_URL = /^https:\/\/[a-z0-9]{20}\.supabase\.co$/;
const PUBLISHABLE_KEY = /^sb_publishable_[A-Za-z0-9_-]{20,200}$/;

/**
 * Fail-closed validation of OWNER_PUBLIC_CONFIG: returns { url, key } only for an https project origin (no userinfo,
 * path, query or trailing slash) plus an sb_publishable_ key and nothing else; otherwise null and no client is created.
 */
export function resolveOwnerClientConfig(cfg) {
    if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) return null;
    const keys = Object.keys(cfg).sort();
    if (keys.length !== 2 || keys[0] !== 'publishableKey' || keys[1] !== 'supabaseUrl') return null;
    const { supabaseUrl, publishableKey } = cfg;
    if (typeof supabaseUrl !== 'string' || !PROJECT_URL.test(supabaseUrl)) return null;
    if (typeof publishableKey !== 'string' || !PUBLISHABLE_KEY.test(publishableKey)) return null;
    return { url: supabaseUrl, key: publishableKey };
}
