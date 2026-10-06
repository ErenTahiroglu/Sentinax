# Phase 27 FIX D: Supabase Security Advisor closure

Migration `supabase/migrations/028_supabase_security_advisor_hardening.sql` plus the `analyze` Edge Function change. Repository tests do not prove the LIVE project: after the migration is applied
to the production project the Security Advisor must be re-run (see the last section). This checkpoint is therefore not closed until fresh live evidence exists.

## Live Advisor findings and adjudication

| Advisor item | Class | Adjudication |
|---|---|---|
| ERROR: RLS Disabled in Public, `public.rate_limits` | A genuine vulnerability | Migration 003 created the table without RLS and the platform default grants gave anon/authenticated direct table access. 028 enables RLS, adds NO policy and revokes every privilege from PUBLIC, anon, authenticated and service_role. |
| WARN: `consume_rate_limit` public/signed-in executable | A genuine vulnerability | The SECURITY DEFINER RPC took `p_identifier`, `p_capacity`, `p_refill_rate` from any caller: a caller could choose a huge capacity or refill rate, drain or refill another identifier's bucket (poisoning) or bypass its own limit. 028 drops the 3-argument function and creates `consume_rate_limit(p_identifier TEXT)` with the closed policy (capacity 15, refill 0.25 per second) fixed inside, EXECUTE revoked from PUBLIC/anon/authenticated and granted only to service_role. Bucket creation is now `INSERT ... ON CONFLICT DO NOTHING` before the row lock, so concurrent first calls cannot collide; the elapsed time is clamped at zero; empty, NULL or over-256-character identifiers are rejected. |
| WARN: `get_user_api_key` signed-in executable | A genuine hardening gap (decrypted Vault material) | Dropped. Replaced by the service-only `get_user_api_key_for_service(p_user_id UUID)` (SECURITY DEFINER, `search_path = pg_catalog, pg_temp`, qualified names). The Edge Function verifies the caller with the user JWT (`auth.getUser()`) and then asks for exactly that id through the privileged client; browsers can no longer fetch a decrypted key by RPC. |
| WARN: `upsert_user_api_key` | B hardening gap (unused) | No call site exists. Dormant: no API role (PUBLIC, anon, authenticated, service_role) may execute it; search_path pinned. Re-enabling a key-setting path is a separate product decision. |
| WARN: `check_user_has_api_key` | B hardening gap (unused) | No call site exists (no frontend or backend caller). EXECUTE revoked from every API role; search_path pinned to the strict path. It reads only the caller's own row, so re-granting `authenticated` later is safe if a product path needs it (and must then be added to the test allowlist with a rationale). |
| WARN: `get_pit_macro_observation` | B hardening gap | The macro tables already have public read policies, so SECURITY DEFINER added no authority. 028 re-flags the SAME Phase 17 body as SECURITY INVOKER with `search_path = public, pg_temp`; anon loses direct execution, authenticated and service_role keep it (needed by the INVOKER wrapper `get_pit_macro_state_input`). No second selection algorithm, no PIT change. |
| WARN: Function Search Path Mutable (the listed trigger / helper / PIT / identity / portfolio / import / SEC / macro functions) | B hardening gap | `ALTER FUNCTION ... SET search_path = public, pg_temp` for every remaining function without one (bodies untouched; the INVOKER functions legitimately use unqualified-safe names inside `public`). The real PostgreSQL inventory test fails if any public function lacks a `search_path` setting, so the audit is not limited to the screenshot names. |
| INFO: `private_scheduler_runs` / `private_scheduler_run_transitions` RLS Enabled No Policy | C intentional architecture | **ACCEPTED_INTENTIONAL_INFO.** Phase 24 migration 024 enables RLS, grants nothing to PUBLIC/anon/authenticated and exposes the scheduler RPCs only to service_role. No policy is added to silence the message; tests assert the tables stay policy-free and unprivileged for API roles. |
| WARN: Leaked Password Protection Disabled | D platform limitation | **ACCEPTED_PLATFORM_LIMITATION_FREE_PLAN.** Available on Pro and above; the project is on the Free plan. Enable it before any production user rollout if the project upgrades. No billing action is taken. |

## Final SECURITY DEFINER inventory and ACL matrix (all with `search_path = pg_catalog, pg_temp`)

| Function | PUBLIC | anon | authenticated | service_role |
|---|---|---|---|---|
| `consume_rate_limit(text)` | no | no | no | yes |
| `get_user_api_key_for_service(uuid)` | no | no | no | yes |
| `upsert_user_api_key(text)` (dormant) | no | no | no | no |
| `check_user_has_api_key()` (dormant) | no | no | no | no |
| `get_portfolio_transaction_history_coverage_snapshot(...)` (026) | no | no | no | yes |
| `lock_portfolio_transaction_reversal_target()`, `validate_fee_tax_attribution_event_integrity()` (027 triggers) | no | no | no | no |

No SECURITY DEFINER function remains authenticated-executable, so the allowlist in `test_supabase_security_advisor_postgres.py` is empty (an entry would need a rationale and an adversarial test). Therefore the Advisor warning
"Signed-In Users Can Execute SECURITY DEFINER Function" is expected to clear; if it remains for any function it must be justified individually.

## Search-path matrix

SECURITY DEFINER: `pg_catalog, pg_temp`, every relation schema-qualified (`public.`, `vault.`, `auth.`). SECURITY INVOKER / trigger functions without a setting before 028: `public, pg_temp` (unchanged bodies). Functions that
already had a pinned path (012, 013, 015, 017, 022, 023, 024, 026) are unchanged.

## Edge Function boundary

Two logically separate clients: a user-scoped client (anon key + the caller's JWT; `auth.getUser()` and RLS-dependent reads) and a server-side privileged client built from the platform-injected `SUPABASE_SERVICE_ROLE_KEY`,
used only for `consume_rate_limit` and `get_user_api_key_for_service`. The credential is never hard-coded, logged, returned or read from the request. The rate-limit identity is derived server-side (`user:<verified id>` or
`ip:<first forwarded address>`), never from the body, and no capacity or refill value is passed. BYOK behavior (x-gemini-key header, Vault fallback, 401/429 responses) is unchanged. Deploy the migration BEFORE the
Edge Function (the new function calls the new RPCs; the old RPCs are dropped by 028).

## Default function privileges

028 revokes EXECUTE on future functions from PUBLIC (for the migration role) and from anon/authenticated in schema public. service_role keeps its platform default. Existing functions are governed by their explicit ACLs; a
real PostgreSQL test proves a newly created function is not executable by PUBLIC/anon/authenticated. Supabase may hold additional default ACLs for other owner roles (for example supabase_admin) that a migration run as `postgres`
cannot change; explicit REVOKE statements in each migration plus the ACL inventory test remain the actual guard.

## Live-history drift and the Preview failure

The first FIX_D commit (`326ba78c`) failed Supabase Preview: `function public.prevent_raw_snapshot_tamper() does not exist` for an unconditional `ALTER FUNCTION`. A fresh replay of the repository
creates that function (migration 004), so the deployment target's history differs from the repository. Sections 4 to 6 of 028 therefore act only on functions that exist (a constant-signature
`to_regprocedure` guard, no dynamic names) and the migration is re-runnable; a real PostgreSQL test drops legacy functions and runs 028 twice. This also means the live database may hold functions that are
absent from the repository: the Security Advisor re-run, not the repository, is the authority for those.

## Fixture limits of the real PostgreSQL test

The test runs migrations 001 to 028 against a plain PostgreSQL 16 database, not a Supabase project: roles, an `auth` stand-in with `auth.uid()`, a `vault` stand-in (the `supabase_vault` extension line is removed), and Supabase-style
default grants. Its behavior against live Supabase (platform-owned grants and default ACLs) is verified only by the live Advisor re-run below.

## Live Security Advisor re-verification (required for closure)

After applying 028 to the production project and deploying the Edge Function: Security Advisor, Refresh / Rerun linter, then provide fresh screenshots of Errors, Warnings and Info or (preferably) the Security Advisor Export.
Expected: ERRORS 0 (no `RLS Disabled in Public`); the targeted `Function Search Path Mutable` and `Public Can Execute SECURITY DEFINER Function` warnings gone; Scheduler `RLS Enabled No Policy` INFO and Leaked Password Protection
on the Free plan remain accepted. This document does not prove the LIVE project is repaired.

## Phase 27 FIX D-R1: live function-signature drift (migration 029)

Fresh live Advisor after 028 was applied: **ERRORS 0**. Only two search-path warnings remained, `public.resolve_instrument_to_provider_symbol` and `public.get_pit_observation`, plus the accepted Free-plan
Leaked Password Protection warning and three INFO findings (scheduler runs, scheduler run transitions, `rate_limits`: RLS enabled, no policy).

**Cause.** The live database has historical function-signature drift. 028 pinned those two functions through fixed repository signatures behind `to_regprocedure` guards (added after Supabase Preview rejected unconditional
ALTERs), so on the live database the signatures did not match, the guard skipped them and the functions stayed mutable.

**Repair (migration 029).** Catalog identity instead of signature assumptions: the two functions are found in `pg_catalog.pg_proc` / `pg_namespace` (schema `public`, exact name); exactly ONE match is required per name
(zero or several raises, so there is no silent skip, no first-match guess and no overload sweep); the ALTER uses the catalog-derived `regprocedure` and changes only `proconfig` to
`search_path = pg_catalog, public, pg_temp` (public is kept because the historical live bodies may use unqualified public relations). Bodies, arguments, return types, volatility, security mode (still INVOKER) and ACLs are unchanged;
the real PostgreSQL test proves this on both a fresh replay of 001 to 029 and a simulated drift (alternative signatures, 028 skips them, 029 alters the very same objects).

**Accepted live findings (unchanged).** WARN Leaked Password Protection Disabled: `ACCEPTED_PLATFORM_LIMITATION_FREE_PLAN`. INFO `private_scheduler_runs`, `private_scheduler_run_transitions` and `rate_limits`
"RLS Enabled No Policy": `ACCEPTED_INTENTIONAL_INFO` (direct-client inaccessible by design; no permissive policy is added to silence them).

**Closure condition.** After applying 029 to production, re-run the Security Advisor and return the export: expected WARNINGS are only Leaked Password Protection; no Function Search Path Mutable finding may remain.
Repository tests do not prove the LIVE project.
