/**
 * Private API target of the owner application (Phase 25D1E3).
 *
 * Only the SAME-ORIGIN mode exists: the private application calls the fixed read-only path below on its own origin. In production a Cloudflare-controlled route is
 * expected to forward that path to the (not yet deployed) Oracle backend; until then the path does not exist on the static origin and the request fails closed.
 * There is deliberately no hostname here and no cross-origin mode: a cross-origin API would require a separately reviewed exact origin in BOTH the CSP connect-src
 * and the backend CORS allow-list, never a wildcard or a permissive proxy.
 */
export const PRIVATE_API_CONFIG = Object.freeze({ mode: 'same-origin' });
