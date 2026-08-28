-- ============================================================================
-- Migration 013: Portfolio External Identity Cross-Language Normalization Parity
-- ============================================================================
-- Purpose:
--   Establishes exact cross-language, locale-independent normalization parity
--   between Python domain logic and PostgreSQL database unique index / RPC.
--
-- Exact Canonical Contract:
--   1. External Source:
--      - Remove ordinary ASCII U+0020 SPACE characters from boundaries ONLY (btrim(s, ' ')).
--      - Case-normalize ASCII lowercase letters a-z -> A-Z using explicit translation:
--        translate(..., 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
--      - Preserve every other character (including tabs, newlines, and non-ASCII characters).
--   2. External Reference:
--      - Remove ordinary ASCII U+0020 SPACE characters from boundaries ONLY (btrim(s, ' ')).
--      - Case-sensitive (preserve case).
--
-- Security & Performance Invariants:
--   - Replaces idx_portfolio_transactions_external_idempotency unique index.
--   - Replaces lookup_portfolio_transaction_external_identity RPC with exact matching logic.
--   - SECURITY INVOKER (respects RLS and caller permissions).
--   - STABLE, deterministic explicit search_path (public, pg_temp).
--   - Scoped strictly by owner_id, portfolio_id, account_id.
--   - Returns only matching transaction UUID (or NULL), zero financial NUMERIC exposure.
--   - REVOKE EXECUTE FROM PUBLIC; GRANT EXECUTE TO authenticated, service_role.
-- ============================================================================

-- 1. Recreate external idempotency unique index with canonical normalization
DROP INDEX IF EXISTS public.idx_portfolio_transactions_external_idempotency;

CREATE UNIQUE INDEX idx_portfolio_transactions_external_idempotency
    ON public.portfolio_transactions (
        portfolio_id,
        account_id,
        translate(btrim(external_source, ' '), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'),
        btrim(external_reference, ' ')
    )
    WHERE external_source IS NOT NULL AND external_reference IS NOT NULL;

-- 2. Update lookup RPC to use canonical normalization
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
      AND translate(btrim(pt.external_source, ' '), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') =
          translate(btrim(p_external_source, ' '), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
      AND btrim(pt.external_reference, ' ') = btrim(p_external_reference, ' ')
    LIMIT 1;
$$;

-- 3. Revoke default public execution rights
REVOKE EXECUTE ON FUNCTION public.lookup_portfolio_transaction_external_identity(UUID, UUID, UUID, TEXT, TEXT) FROM PUBLIC;

-- 4. Grant execution to authenticated users and service_role
GRANT EXECUTE ON FUNCTION public.lookup_portfolio_transaction_external_identity(UUID, UUID, UUID, TEXT, TEXT) TO authenticated, service_role;
