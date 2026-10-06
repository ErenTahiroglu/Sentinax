-- 028_supabase_security_advisor_hardening.sql
-- Sentinax: Phase 27 FIX D, live Supabase Security Advisor closure (RLS / SECURITY DEFINER / search_path / rate-limit RPC)
--
-- Findings closed (see docs/PHASE_27_SUPABASE_SECURITY_ADVISOR_CLOSURE.md):
--   1. public.rate_limits had no RLS and kept the legacy Data API table grants (Advisor ERROR).
--   2. public.consume_rate_limit(TEXT, INT, FLOAT) was SECURITY DEFINER, executable by PUBLIC/anon/authenticated and let the caller choose the
--      identifier, capacity and refill rate (bypass, bucket poisoning). The policy is now fixed inside a service-only function.
--   3. public.get_user_api_key() returned decrypted Vault material to any signed-in caller. Retrieval is now a service-only primitive taking the
--      caller id verified by the trusted Edge Function.
--   4. Dormant privileged helpers (upsert_user_api_key, check_user_has_api_key) lose all API-role EXECUTE.
--   5. get_pit_macro_observation was an unpinned SECURITY DEFINER over tables that already have public read policies: it becomes SECURITY INVOKER
--      (same body, same PIT algorithm) and is no longer executable by anon.
--   6. Every remaining public function without a pinned search_path gets one (Advisor "Function Search Path Mutable"), without touching any body.
--   7. Forward-looking default function privileges no longer hand EXECUTE to PUBLIC/anon/authenticated.
-- Not changed: scheduler tables/RPCs (RLS enabled without policy is the intentional Phase 24 service-role-only design), Phase 27 FIX A trigger
-- functions, any financial or PIT logic.

-- ============================================================================
-- 1. public.rate_limits: RLS on, no client table access at all
-- ============================================================================
ALTER TABLE public.rate_limits ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE public.rate_limits FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.rate_limits FROM anon;
REVOKE ALL PRIVILEGES ON TABLE public.rate_limits FROM authenticated;
-- The only writer is the SECURITY DEFINER function below (owner access); service_role needs no direct table access either.
REVOKE ALL PRIVILEGES ON TABLE public.rate_limits FROM service_role;

-- ============================================================================
-- 2. consume_rate_limit: fixed, non-caller-configurable policy; service-only
-- ============================================================================
DROP FUNCTION IF EXISTS public.consume_rate_limit(TEXT, INTEGER, DOUBLE PRECISION);

CREATE OR REPLACE FUNCTION public.consume_rate_limit(p_identifier TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    -- Closed contract (15 requests per 60 seconds): never accepted from a caller.
    c_capacity    CONSTANT DOUBLE PRECISION := 15;
    c_refill_rate CONSTANT DOUBLE PRECISION := 0.25;
    v_now TIMESTAMP WITH TIME ZONE := pg_catalog.now();
    v_last_updated TIMESTAMP WITH TIME ZONE;
    v_tokens DOUBLE PRECISION;
    v_new_tokens DOUBLE PRECISION;
BEGIN
    IF p_identifier IS NULL OR pg_catalog.length(p_identifier) = 0 OR pg_catalog.length(p_identifier) > 256 THEN
        RAISE EXCEPTION 'Invalid rate-limit identifier' USING ERRCODE = '22023';
    END IF;

    -- Atomic bucket creation: two concurrent first calls cannot collide on the primary key.
    INSERT INTO public.rate_limits (identifier, tokens, last_updated)
    VALUES (p_identifier, c_capacity, v_now)
    ON CONFLICT (identifier) DO NOTHING;

    SELECT r.tokens, r.last_updated INTO v_tokens, v_last_updated
    FROM public.rate_limits r
    WHERE r.identifier = p_identifier
    FOR UPDATE;

    v_new_tokens := LEAST(
        c_capacity,
        v_tokens + (GREATEST(EXTRACT(EPOCH FROM (v_now - v_last_updated)), 0) * c_refill_rate)
    );

    IF v_new_tokens >= 1 THEN
        UPDATE public.rate_limits
        SET tokens = v_new_tokens - 1,
            last_updated = GREATEST(v_now, v_last_updated)
        WHERE identifier = p_identifier;
        RETURN TRUE;
    END IF;
    RETURN FALSE;
END;
$$;

REVOKE ALL ON FUNCTION public.consume_rate_limit(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.consume_rate_limit(TEXT) FROM anon;
REVOKE ALL ON FUNCTION public.consume_rate_limit(TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.consume_rate_limit(TEXT) TO service_role;

-- ============================================================================
-- 3. Vault key retrieval: service-only primitive (the verified caller id is supplied by the trusted Edge Function)
-- ============================================================================
DROP FUNCTION IF EXISTS public.get_user_api_key();

CREATE OR REPLACE FUNCTION public.get_user_api_key_for_service(p_user_id UUID)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_secret_id UUID;
    v_api_key TEXT;
BEGIN
    IF p_user_id IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT p.vault_secret_id INTO v_secret_id
    FROM public.user_profiles p
    WHERE p.user_id = p_user_id;

    IF v_secret_id IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT ds.decrypted_secret INTO v_api_key
    FROM vault.decrypted_secrets ds
    WHERE ds.id = v_secret_id;

    RETURN v_api_key;
END;
$$;

REVOKE ALL ON FUNCTION public.get_user_api_key_for_service(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_user_api_key_for_service(UUID) FROM anon;
REVOKE ALL ON FUNCTION public.get_user_api_key_for_service(UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_user_api_key_for_service(UUID) TO service_role;

-- Sections 4 to 6 act only on functions that exist in the target database (to_regprocedure guard): the live project's migration history may differ from a fresh replay of the repository
-- (Supabase Preview reported `function public.prevent_raw_snapshot_tamper() does not exist` for an unconditional ALTER). Signatures are constants; no caller-selected names, no data access.

-- ============================================================================
-- 4. Dormant privileged helpers (no active caller in the repository): no API-role EXECUTE, strict search_path
-- ============================================================================
DO $$
DECLARE
    v_signature TEXT;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY['public.upsert_user_api_key(text)', 'public.check_user_has_api_key()'] LOOP
        IF pg_catalog.to_regprocedure(v_signature) IS NOT NULL THEN
            EXECUTE pg_catalog.format('ALTER FUNCTION %s SET search_path = pg_catalog, pg_temp', v_signature);
            EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated, service_role', v_signature);
        END IF;
    END LOOP;
END
$$;

-- ============================================================================
-- 5. get_pit_macro_observation: same Phase 17 PIT body, no privileged execution (macro tables already have public read policies)
-- ============================================================================
DO $$
DECLARE
    v_signature CONSTANT TEXT := 'public.get_pit_macro_observation(character varying, date, timestamp with time zone, character varying)';
BEGIN
    IF pg_catalog.to_regprocedure(v_signature) IS NOT NULL THEN
        EXECUTE pg_catalog.format('ALTER FUNCTION %s SECURITY INVOKER SET search_path = public, pg_temp', v_signature);
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon', v_signature);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO authenticated, service_role', v_signature);
    END IF;
END
$$;

-- ============================================================================
-- 6. Pinned search_path for every remaining public function (bodies untouched)
-- ============================================================================
DO $$
DECLARE
    v_signature TEXT;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'public.update_updated_at_column()',
        'public.handle_record_supersession()',
        'public.prevent_raw_snapshot_tamper()',
        'public.prevent_observation_tamper()',
        'public.get_pit_observation(uuid, character varying, date, timestamp with time zone, character varying)',
        'public.resolve_provider_symbol_to_instrument(character varying, character varying, date)',
        'public.resolve_instrument_to_provider_symbol(uuid, character varying, date)',
        'public.prevent_macro_observation_tamper()',
        'public.handle_macro_observation_supersession()',
        'public.prevent_sec_immutability_violation()',
        'public.validate_sec_fact_filing_link_integrity()',
        'public.prevent_cash_bucket_identity_mutation()',
        'public.validate_portfolio_transaction_integrity()',
        'public.prevent_portfolio_transaction_tamper()',
        'public.prevent_import_claim_binding_tamper()',
        'public.prevent_fee_tax_attribution_event_tamper()'
    ] LOOP
        IF pg_catalog.to_regprocedure(v_signature) IS NOT NULL THEN
            EXECUTE pg_catalog.format('ALTER FUNCTION %s SET search_path = public, pg_temp', v_signature);
        END IF;
    END LOOP;
END
$$;

-- ============================================================================
-- 7. Forward-looking default function privileges (functions created later by this role are not executable by PUBLIC/anon/authenticated
--    unless a migration grants EXECUTE explicitly; service_role keeps its platform default)
-- ============================================================================
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon, authenticated;
