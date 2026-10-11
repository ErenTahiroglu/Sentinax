// Pinned destination of the public "Sahip Girişi" link (Phase 25D1E4). Reviewed in git; never read from the URL, storage or the visitor.
// origin: null = the owner login host is not configured yet, so the link stays disabled. Set it to the exact https origin of the isolated owner
// project (scheme + host only, no port/path) when that host exists. path must stay the single approved login path.
window.OWNER_ENTRY_CONFIG = Object.freeze({ origin: null, path: '/owner/' });
