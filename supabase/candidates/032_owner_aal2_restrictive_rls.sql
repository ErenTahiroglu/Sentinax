-- =====================================================================================================================================
-- Phase 25D1E0 — Migration 032 CANDIDATE: owner + AAL2 (TOTP) database boundary.            *** OFFLINE CANDIDATE — NOT APPLIED ***
--
-- Lives in supabase/candidates/ on purpose: supabase/migrations/ is picked up by the Supabase CLI / Preview / any GitHub integration and
-- a merge there could reach a connected project. Promotion into supabase/migrations/032_*.sql requires a separate, explicitly approved
-- checkpoint after the operator has run supabase/candidates/032_operator_live_inventory.sql and the Red Team has adjudicated its output.
--
-- What it does (and only this):
--   1. Refuses to apply (named blockers) when the live schema contains any public relation this contract has not classified
--      (legacy tables from infrastructure/database/*.sql, views, ...), when an owner table lacks RLS, or when auth.jwt() is absent.
--   2. Creates a NON-exposed schema private_config with a singleton owner_identity table (empty => everything below denies) and a
--      SECURITY DEFINER helper private_config.owner_aal2() with a pinned empty search_path. No API role can read the table, use the schema
--      or call the helper by name; authenticated can only reach it through the policy expressions below.
--   3. Adds ONE RESTRICTIVE policy (owner_aal2_required, FOR ALL, TO authenticated) to each owner-private table. A restrictive policy can
--      only narrow: it never grants anything the permissive policies and table privileges did not already grant.
--   4. Narrows (REVOKE only): anon loses every privilege on the owner tables and EXECUTE on the owner-ledger RPCs; authenticated loses
--      TRUNCATE / REFERENCES / TRIGGER on them. service_role, default privileges, permissive policies and the ledger writer model are untouched.
--
-- Out of scope (separate checkpoints): reference/PIT tables that still carry broad anon/authenticated table privileges guarded only by RLS,
-- user_profiles (legacy Vault pointer), service_role (BYPASSRLS — guarded by the FastAPI owner/AAL2 layer, not by RLS), signing keys.
--
-- Owner identity is configured OUT OF BAND by the operator after the real account exists (never by this file, never by the agent):
--     INSERT INTO private_config.owner_identity (owner_id) VALUES ('<OWNER_UUID_FROM_SUPABASE_AUTH>');
-- PostgREST limits: the policies see the claims PostgREST publishes in request.jwt.claims (sub, aal, amr). A direct SQL session could set
-- that setting itself; there is no network path to such a session. That the live project's tokens expose `amr` is documented Supabase
-- behaviour but UNVERIFIED here.
-- Pre-apply snapshot (operator, read-only): see 032_operator_live_inventory.sql; keep it, rollback never re-grants blindly.
-- =====================================================================================================================================

DO $blockers$
DECLARE
    unclassified text;
    missing_rls text;
BEGIN
    IF to_regprocedure('auth.jwt()') IS NULL OR to_regprocedure('auth.uid()') IS NULL THEN
        RAISE EXCEPTION 'owner_aal2_blocker: auth.uid() / auth.jwt() are not available';
    END IF;

    SELECT string_agg(c.relname::text || ' (' || c.relkind::text || ')', ', ' ORDER BY c.relname) INTO unclassified
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
      AND c.relname <> ALL (ARRAY[
          -- owner-private (restrictive owner+AAL2 policy)
          'portfolios', 'portfolio_accounts', 'cash_buckets', 'investment_goals', 'planned_contributions',
          'portfolio_transactions', 'portfolio_fee_tax_attribution_events', 'portfolio_import_claim_bindings',
          -- service-only (no API privileges expected)
          'private_scheduler_runs', 'private_scheduler_run_transitions', 'learning_evidence_bindings', 'rate_limits',
          -- reference / public-read
          'allowed_assets', 'macro_observations', 'macro_series',
          'instruments', 'corporate_actions', 'provider_aliases', 'normalized_observations', 'raw_provider_snapshots',
          'sec_filings', 'sec_raw_facts', 'sec_fact_filing_links',
          -- legacy user-scoped (Vault pointer; deliberately untouched here)
          'user_profiles'
      ]);
    IF unclassified IS NOT NULL THEN
        RAISE EXCEPTION 'owner_aal2_blocker: unclassified public relations (inventory them and classify before applying): %', unclassified;
    END IF;

    SELECT string_agg(t, ', ') INTO missing_rls
    FROM unnest(ARRAY[
        'portfolios', 'portfolio_accounts', 'cash_buckets', 'investment_goals', 'planned_contributions',
        'portfolio_transactions', 'portfolio_fee_tax_attribution_events', 'portfolio_import_claim_bindings'
    ]) AS t
    WHERE NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = to_regclass('public.' || quote_ident(t)) AND c.relrowsecurity);
    IF missing_rls IS NOT NULL THEN
        RAISE EXCEPTION 'owner_aal2_blocker: owner table missing or without row level security: %', missing_rls;
    END IF;
END
$blockers$;

CREATE SCHEMA IF NOT EXISTS private_config;
REVOKE ALL ON SCHEMA private_config FROM PUBLIC, anon, authenticated, service_role;

CREATE TABLE IF NOT EXISTS private_config.owner_identity (
    singleton    boolean     PRIMARY KEY DEFAULT true CHECK (singleton),
    owner_id     uuid        NOT NULL CHECK (owner_id::text !~ '^[0-]+$'),
    configured_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE private_config.owner_identity ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE private_config.owner_identity FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION private_config.owner_aal2()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $fn$
    SELECT COALESCE((
        SELECT (o.owner_id = auth.uid())
           AND (auth.jwt() ->> 'aal') = 'aal2'
           AND CASE WHEN jsonb_typeof(auth.jwt() -> 'amr') = 'array' THEN EXISTS (
                   SELECT 1 FROM jsonb_array_elements(auth.jwt() -> 'amr') AS e(m)
                   WHERE jsonb_typeof(e.m) = 'object' AND (e.m ->> 'method') = 'totp' AND jsonb_typeof(e.m -> 'timestamp') = 'number'
               ) ELSE false END
        FROM private_config.owner_identity AS o
        WHERE o.singleton
    ), false)
$fn$;
REVOKE ALL ON FUNCTION private_config.owner_aal2() FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION private_config.owner_aal2() TO authenticated;

DO $policies$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'portfolios', 'portfolio_accounts', 'cash_buckets', 'investment_goals', 'planned_contributions',
        'portfolio_transactions', 'portfolio_fee_tax_attribution_events', 'portfolio_import_claim_bindings'
    ] LOOP
        EXECUTE format('DROP POLICY IF EXISTS owner_aal2_required ON public.%I', t);
        EXECUTE format(
            'CREATE POLICY owner_aal2_required ON public.%I AS RESTRICTIVE FOR ALL TO authenticated'
            ' USING ((SELECT private_config.owner_aal2())) WITH CHECK ((SELECT private_config.owner_aal2()))', t);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon', t);
        EXECUTE format('REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE public.%I FROM authenticated', t);
    END LOOP;
END
$policies$;

REVOKE EXECUTE ON FUNCTION public.commit_portfolio_import_claim(jsonb, jsonb) FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.commit_portfolio_import_claim_batch(jsonb) FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.lookup_portfolio_transaction_external_identity(uuid, uuid, uuid, text, text) FROM PUBLIC, anon;
