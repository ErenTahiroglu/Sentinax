-- ============================================================================
-- Migration 026: atomic portfolio transaction history coverage snapshot (Phase 26C2B2B1)
-- ============================================================================
-- One service-role-only RPC that answers, atomically: which complete set of
-- persisted public.portfolio_transactions rows has recorded_at <= a replay cutoff
-- for one explicit owner and portfolio, as observed after a SHARE lock.
--
-- Why not the offset/range pagination of the repository list read: it issues many
-- statements and can observe different database states across pages. Here one
-- statement-consistent snapshot is taken after LOCK TABLE ... IN SHARE MODE (it
-- conflicts with the ROW EXCLUSIVE lock of every writer, so earlier writers finish
-- and no new row can appear while the manifest is selected). Advisory or
-- application locks are not used: existing writers do not participate in them.
--
-- SECURITY DEFINER is an explicitly authorized exception, used only so the owner
-- can take the SHARE lock; migration 025 deliberately keeps service_role at
-- SELECT and INSERT. EXECUTE is service-role-only and the search_path is pinned.
--
-- The manifest binds each row by physical id, recorded_at (epoch microseconds) and
-- economic fingerprint, in one canonical order. observed_at is the database proof
-- observation time (never the cutoff and never an economic date). The function
-- writes nothing and defines no digest: the ordered tuple is the evidence.
-- Limit: this proves the committed rows visible after the lock through the cutoff,
-- relying on the trusted service-role write boundary (migration 025); it does not
-- prevent a trusted writer from inserting a backdated row after the proof ends.
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_portfolio_transactions_history_coverage
    ON public.portfolio_transactions (owner_id, portfolio_id, recorded_at, id);

CREATE OR REPLACE FUNCTION public.get_portfolio_transaction_history_coverage_snapshot(
    p_owner_id UUID,
    p_portfolio_id UUID,
    p_as_of_recorded_at TIMESTAMPTZ
)
RETURNS TABLE (
    owner_id UUID,
    portfolio_id UUID,
    as_of_recorded_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ,
    transaction_count BIGINT,
    transaction_ids UUID[],
    recorded_at_epoch_micros BIGINT[],
    economic_fingerprints TEXT[]
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_observed_at TIMESTAMPTZ;
    v_invalid BIGINT;
    v_count BIGINT;
    v_ids UUID[];
    v_micros BIGINT[];
    v_fingerprints TEXT[];
BEGIN
    IF p_owner_id IS NULL OR p_portfolio_id IS NULL OR p_as_of_recorded_at IS NULL THEN
        RAISE EXCEPTION 'coverage snapshot requires non-null owner, portfolio and cutoff';
    END IF;

    LOCK TABLE public.portfolio_transactions IN SHARE MODE;

    v_observed_at := pg_catalog.clock_timestamp();

    IF p_as_of_recorded_at > v_observed_at THEN
        RAISE EXCEPTION 'coverage cutoff is later than the database observation time';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.portfolios p
        WHERE p.id = p_portfolio_id AND p.owner_id = p_owner_id
    ) THEN
        RAISE EXCEPTION 'portfolio does not exist for the given owner';
    END IF;

    SELECT count(*) INTO v_invalid
    FROM public.portfolio_transactions t
    WHERE t.owner_id = p_owner_id AND t.portfolio_id = p_portfolio_id AND t.recorded_at <= p_as_of_recorded_at
      AND t.economic_fingerprint !~ '^[0-9a-f]{64}$';
    IF v_invalid > 0 THEN
        RAISE EXCEPTION 'persisted economic fingerprint is not 64 lowercase hexadecimal characters';
    END IF;

    SELECT
        count(*)::bigint,
        COALESCE(array_agg(t.id ORDER BY t.recorded_at ASC, t.id ASC), ARRAY[]::uuid[]),
        COALESCE(array_agg((pg_catalog.extract(epoch FROM t.recorded_at) * 1000000)::bigint ORDER BY t.recorded_at ASC, t.id ASC), ARRAY[]::bigint[]),
        COALESCE(array_agg(t.economic_fingerprint::text ORDER BY t.recorded_at ASC, t.id ASC), ARRAY[]::text[])
    INTO v_count, v_ids, v_micros, v_fingerprints
    FROM public.portfolio_transactions t
    WHERE t.owner_id = p_owner_id AND t.portfolio_id = p_portfolio_id AND t.recorded_at <= p_as_of_recorded_at;

    RETURN QUERY SELECT p_owner_id, p_portfolio_id, p_as_of_recorded_at, v_observed_at, v_count, v_ids, v_micros, v_fingerprints;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_portfolio_transaction_history_coverage_snapshot(UUID, UUID, TIMESTAMPTZ) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_portfolio_transaction_history_coverage_snapshot(UUID, UUID, TIMESTAMPTZ) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_portfolio_transaction_history_coverage_snapshot(UUID, UUID, TIMESTAMPTZ) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_portfolio_transaction_history_coverage_snapshot(UUID, UUID, TIMESTAMPTZ) TO service_role;
