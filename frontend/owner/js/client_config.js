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

// Exact version pin; the library handles credentials, so it must never float.
export const SUPABASE_JS_URL = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.1/+esm';
