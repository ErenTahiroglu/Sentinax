-- ============================================================================
-- Migration 012: Portfolio External Identity Lookup RPC
-- ============================================================================
-- Purpose:
--   Provides a deterministic, index-aligned lookup for external transactions
--   under the normalized composite unique key defined in Migration 011:
--   (portfolio_id, account_id, upper(trim(external_source)), trim(external_reference))
--
-- Security & Performance Invariants:
--   - SECURITY INVOKER (respects RLS and caller permissions)
--   - STABLE, deterministic explicit search_path (public, pg_temp)
--   - Scoped strictly by owner_id, portfolio_id, account_id
--   - Returns only the matching transaction UUID (or NULL), zero financial NUMERIC exposure
--   - REVOKE EXECUTE FROM PUBLIC; GRANT EXECUTE TO authenticated, service_role
-- ============================================================================

CREATE OR REPLACE FUNCTION public.lookup_portfolio_transaction_external_identity(
    p_owner_id UUID,
    p_portfolio_id UUID,
    p_account_id UUID,
    p_external_source TEXT,
    p_external_reference TEXT
)
RETURNS UUID
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT pt.id
    FROM public.portfolio_transactions pt
    WHERE pt.owner_id = p_owner_id
      AND pt.portfolio_id = p_portfolio_id
      AND pt.account_id = p_account_id
      AND pt.external_source IS NOT NULL
      AND pt.external_reference IS NOT NULL
      AND p_external_source IS NOT NULL
      AND p_external_reference IS NOT NULL
      AND upper(trim(pt.external_source)) = upper(trim(p_external_source))
      AND trim(pt.external_reference) = trim(p_external_reference)
    LIMIT 1;
$$;

-- Revoke default public execution rights
REVOKE EXECUTE ON FUNCTION public.lookup_portfolio_transaction_external_identity(UUID, UUID, UUID, TEXT, TEXT) FROM PUBLIC;

-- Grant execution to authenticated users and service_role
GRANT EXECUTE ON FUNCTION public.lookup_portfolio_transaction_external_identity(UUID, UUID, UUID, TEXT, TEXT) TO authenticated, service_role;
