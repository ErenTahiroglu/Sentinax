/**
 * Public configuration of the owner bootstrap page (Phase 25D1C2).
 *
 * Only the Supabase project URL and the PUBLISHABLE key. Both values are already public (they ship to every browser
 * of the legacy SPA). Never put a secret, service-role or JWT-style key here: client_config.js rejects anything that
 * is not an sb_publishable_ key and the page then fails closed. This file is the owner page's only configuration;
 * it must not depend on the legacy SPA's globals or files.
 */
export const OWNER_PUBLIC_CONFIG = Object.freeze({
    supabaseUrl: 'https://zlggrmsolklhfgijcjnz.supabase.co',
    publishableKey: 'sb_publishable_G67qSQ6JxmUYy3fuQAHb_Q_myhSebvW', // gitleaks:allow (publishable browser key, public by design; same value the legacy SPA ships)
});
