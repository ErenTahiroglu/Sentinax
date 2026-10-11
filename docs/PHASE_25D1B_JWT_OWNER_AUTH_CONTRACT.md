# Phase 25D1B — JWT verification and single-owner backend authorization

Scope: `backend/infrastructure/auth.py` (authentication) and `backend/infrastructure/private_access.py` (owner authorization). No private endpoint is mounted; no schema, RLS, frontend or deployment change. **This does not make the private application or the database secure** (see "Not covered").

## Trust configuration (operator-set, never from token contents)

| Variable | Meaning | Failure |
|---|---|---|
| `SUPABASE_URL` | https project origin; issuer = `<origin>/auth/v1`, JWKS = `<origin>/auth/v1/.well-known/jwks.json` | not https / has userinfo, path, query / absent -> 503 |
| `SENTINAX_SUPABASE_JWT_MODE` | **required**: `jwks` or `legacy_hs256` | unset/unknown -> 503 (no guessing, no fallback) |
| `SUPABASE_JWT_SECRET` | read only in `legacy_hs256`, >= 32 bytes | missing/short -> 503 |
| `SENTINAX_OWNER_USER_ID` | the one approved owner UUID | absent/invalid/nil -> 503 on private routes |

The production signing mode is **unknown to this repository** and must be read from the Supabase dashboard before setting the mode variable.

## Signing-mode compatibility

| Mode | Accepted `alg` | Key source | Rejected |
|---|---|---|---|
| `jwks` | `ES256` (P-256), `RS256` | project JWKS by `kid`; key type must match header `alg` | `HS256`, `none`, missing/unknown `kid`, `jku/jwk/x5u/x5c/crit` headers, symmetric JWKs |
| `legacy_hs256` | `HS256` | `SUPABASE_JWT_SECRET` | every asymmetric alg, `none`; JWKS is never fetched |

Supabase's guidance: the legacy shared secret is "no longer recommended" and "not recommended for production"; JWT validation for it is expected to involve the Auth server, whereas asymmetric keys verify locally via the discovery endpoint. We therefore implement local HS256 verification only as an explicit opt-in and record server-side verification (`/auth/v1/user`) as a later dependency. Anyone holding the legacy secret can mint any claims.

## Claims required for a user token

Signature, `iss` == trusted issuer, `aud` == exactly `"authenticated"` (a list is rejected), `exp`/`iat`/`sub`/`iss`/`aud` present, `nbf` honored if present (10 s leeway), `sub` canonical lower-case UUID, `role == "authenticated"` (so `anon` and `service_role` keys never pass), `is_anonymous` is the boolean `false`, `session_id` canonical UUID. Anything else -> 401 with a fixed generic message.

## JWKS cache, rotation, revocation windows

* TTL 300 s; the whole key set is replaced on refresh (a key removed upstream stops being trusted at the next refresh).
* Unknown `kid`: one forced refresh, at most once per 30 s per project (attacker-chosen `kid`s cannot amplify into fetches).
* Fetch failure/malformed document: fail closed (503), 5 s failure backoff, no stale trust after TTL.
* Revoked-key window = our TTL (<= 5 min) + Supabase edge cache (documented 10 min) = **up to ~15 min**; Supabase documents a multi-level cache cleared every 20 min, so urgent revocation also needs a cache-busting restart.

## Revocation (no instantaneous guarantee)

An access token is valid until `exp` once issued. Sign-out/factor removal revokes the session/refresh token at Supabase, not tokens already issued; an unenrolled factor keeps `aal2` in an existing token until refresh. The former Redis `jwt_blacklist` check had **no writer anywhere in the repository**, failed open on any Redis error, and used the raw token as a key; it is removed. Immediate revocation needs a separate design (short JWT lifetime plus an Auth-server session check that fails closed) — dependency for 25D1D.

## Owner authorization (`require_private_owner`)

1. Authenticate (`verify_jwt`). 2. Read `SENTINAX_OWNER_USER_ID` (fail closed). 3. `sub` must equal it (constant-time compare, canonical form) else 403 `forbidden`. 4. `aal == "aal2"` and `amr` contains an entry with `method == "totp"` and integer `timestamp`, else 403 `mfa_required` (only a verified owner learns this). Owner identity is never read from query, body, headers, cookies, localStorage or `user_metadata`/`app_metadata`. `verify_jwt` itself stays owner-agnostic for public/other routes.

## Compatibility

`verify_token_string` and `verify_jwt` keep their names; `verify_jwt` still sets `request.state.user`. `/ws/prices` still calls `verify_token_string` (now in a threadpool) and closes with 1008 on any failure. Behavior changes that are intentional: mode must be configured, the missing-secret error is 503 not 500, error text is generic, and Redis is no longer consulted.

## Not covered (carried forward)

A. PostgREST/RLS: owner policies are not a single-owner allowlist and lack AAL2 (migration 032). B. Global idempotency cache (`idemp:{client key}` in `main.py`) can collide across users/endpoints — HIGH, fix before any private API. C. Service-role code in `dependencies.py` derives identity from claims, bypasses RLS and interpolates `sub` into URLs. D. Frontend `admin_bypass`, `mock_bypass_token`, `offline_portfolio_*`, service-worker caching. E. `/ws/prices`: token in query string, no Origin check, no expiry enforcement on an open socket. F. Buffett `AVAILABLE`-branch `innerHTML`.
