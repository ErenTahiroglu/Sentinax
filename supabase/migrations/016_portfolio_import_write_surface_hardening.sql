-- 016_portfolio_import_write_surface_hardening.sql
-- Sentinax Private Personal Investment Decision Engine
-- Phase 13Q.3: Import Atomic Write-Surface Exclusivity & Claim-Squatting Hardening
--
-- Security Boundary Hardening:
--   1. Claim-Squatting Prevention:
--      - Drop authenticated INSERT policy on public.portfolio_import_claim_bindings.
--      - Revoke direct INSERT table privilege from authenticated role.
--      - Raw import claim bindings can only be created via the backend atomic commit path.
--      - Authenticated SELECT policy ("Users can view own import claim bindings") is preserved.
--   2. Atomic Import Commit RPC Trust Boundary:
--      - Revoke EXECUTE privilege on public.commit_portfolio_import_claim(JSONB, JSONB) from authenticated.
--      - Revoke EXECUTE privilege from PUBLIC.
--      - Explicitly grant EXECUTE privilege strictly to service_role.
--      - Direct client/browser callers cannot bypass canonical Python domain serialization
--        or submit arbitrary economic fingerprints.
--   3. Preservation of Immutability:
--      - No authenticated UPDATE or DELETE policies.
--      - Immutability trigger (trg_prevent_import_claim_binding_tamper) remains intact.

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Hardening Table Permissions & RLS on portfolio_import_claim_bindings
-- ─────────────────────────────────────────────────────────────────────────────

-- Remove direct INSERT policy for authenticated users (prevents claim pre-binding / squatting)
DROP POLICY IF EXISTS "Users can insert own import claim bindings" ON public.portfolio_import_claim_bindings;

-- Defense-in-depth: revoke direct table INSERT privilege from authenticated role
REVOKE INSERT ON TABLE public.portfolio_import_claim_bindings FROM authenticated;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Hardening RPC Execution Privileges on commit_portfolio_import_claim
-- ─────────────────────────────────────────────────────────────────────────────

-- Revoke direct execution from authenticated users and public
REVOKE EXECUTE ON FUNCTION public.commit_portfolio_import_claim(JSONB, JSONB) FROM authenticated;
REVOKE EXECUTE ON FUNCTION public.commit_portfolio_import_claim(JSONB, JSONB) FROM PUBLIC;

-- Ensure execute authority is granted exclusively to service_role
GRANT EXECUTE ON FUNCTION public.commit_portfolio_import_claim(JSONB, JSONB) TO service_role;
